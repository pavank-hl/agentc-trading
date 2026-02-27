"""Strategy engine: builds multi-symbol prompts, calls LLM, parses and validates.

This is the central orchestrator that connects indicators, LLM, and risk manager.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .indicators import IndicatorReport, compute_indicators
from .models.config import TradingConfig
from .sentiment import (
    FundingHistory,
    LiquidationTracker,
    fetch_fear_greed,
)
from .taapi import TaapiClient
from .models.decision import (
    AnalysisCycle,
    MultiSymbolDecision,
    TradeDecision,
    ValidatedDecision,
)
from .models.market import MarketSnapshot
from .models.position import PortfolioState
from .risk_manager import RiskManager

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE_PATH = Path(__file__).parent.parent / "prompt_template.md"

# All markets support 100x max leverage on Orderly Network.
# leverage_pct config (from LEVERAGE_PCT env var) defines what % to use.
MARKET_MAX_LEVERAGE = 100

ANALYSIS_PROMPT = """You are an expert perpetual futures analyst on Orderly Network. You receive pre-computed technical indicators for multiple symbols and output a directional call for each.

## Your Job
- You ALWAYS output LONG or SHORT for every symbol. Never HOLD, never CLOSE.
- You are a pure market analyst. You have no awareness of existing positions — that is handled separately.
- Your job: determine the most likely direction for each symbol and rate your confidence 0-100.

## Decision Framework — Two Layers

### Layer 1: Foundational Edge (70% of decision weight) — DETERMINES DIRECTION
These are LEADING signals. They tell you which way to trade BEFORE price confirms.

**Funding + OI (rules of thumb):**
- Funding rate direction, magnitude, and 24h trend
- Price up + OI stable = healthy trend continuation
- Price up + OI surging + high funding = crowded/fragile — fade or wait
- Price up + OI dropping = short squeeze / forced covering — continuation likely
- OI flush (sharp OI drop + price move) = forced liquidation cascade — trade the continuation
- Funding flipping from positive to negative (or vice versa) = regime change — high signal
- Extreme funding (>0.01% per 8h) = crowded side, mean-reversion risk

**Liquidations:**
- Long squeeze (longs getting wiped) = bearish cascade risk
- Short squeeze (shorts getting wiped) = bullish cascade risk
- Large liquidation clusters = fuel for momentum in that direction

**Taker Flow + Volume Delta:**
- Taker buy >60% = aggressive buying demand → bullish
- Taker sell >60% = aggressive selling pressure → bearish
- Volume delta direction confirms aggressor side

**Orderbook:**
- Imbalance >0.2 = directional pressure
- Bid-heavy book = buy wall support; ask-heavy = sell wall resistance
- Absorption: large resting orders being eaten = breakout imminent in that direction
- Est. slippage: >5bps = thin book (reduce size); <2bps = deep book (full size OK)

**Vol/OI Participation Gate:**
- Vol/OI ratio < 0.5 = stale positioning, low participation — subtract 5 from score
- Vol/OI ratio > 2.0 = very active turnover — signals are fresher, more reliable

**Sentiment:**
- Fear & Greed: <25 = extreme fear (contrarian buy), >75 = extreme greed (contrarian sell)
- Spot-Futures Basis: >0.1% = futures premium (bullish), <-0.1% = discount (bearish)
- L/S ratio: >1.49 = crowded longs (contrarian short risk), <0.67 = crowded shorts (contrarian long risk)

### Layer 2: Technical Execution (30% of decision weight) — REFINES ENTRY TIMING
These are LAGGING signals. Use them to time entries and place SL/TP, NOT to determine direction.

**Trend (15m and 1h):**
- EMA alignment: 9 > 21 > 50 = bullish, reverse = bearish
- Price vs VWAP: above = bullish, below = bearish
- MACD direction and histogram
- ADX: >25 = strong trend (trust signals), <18 = choppy (filter noise)

**Momentum (5m and 15m):**
- RSI: <40 favors long, >60 favors short. Extremes (<30, >70) strong.
- StochRSI: <20 = oversold (long timing), >80 = overbought (short timing)
- CCI: <-100 = oversold, >+100 = overbought
- Bollinger %B: <0.3 = long zone, >0.7 = short zone
- MACD histogram building = momentum, fading = weakening
- Recent candle trend and % change: detect sharp moves lagging indicators miss

## Setup Quality Score (0-100)
For EVERY symbol, compute a quality score. This score determines confidence and leverage — but you ALWAYS output a direction.

**Foundational sub-score (0-50):**
- Funding alignment: +10 (funding favors your direction or is neutral)
- OI health: +10 (not crowded — no surging OI + extreme funding combo)
- Taker flow: +10 (>55% in your direction)
- Orderbook: +10 (imbalance favors your direction)
- Liquidation fuel: +10 (liquidations on opposite side, or no liquidation pressure against you)

**Sentiment sub-score (0-20):**
- Fear & Greed alignment: +10 (contrarian or confirming)
- L/S ratio + basis alignment: +10

**Technical sub-score (0-30):**
- EMA alignment on 1h: +10 (aligned with your direction)
- RSI/StochRSI timing: +10 (not overbought for long, not oversold for short)
- ADX trend strength: +10 (>20)

Include "Score: XX/100" in your reasoning.

## Quality-Gated Leverage (as % of Effective Max)

Each symbol has an "effective max leverage" shown in the market data. Use this table:

| Score | Leverage Range (% of effective max) | Margin % of Wallet |
|-------|-------------------------------------|-------------------|
| 75-100 | 80-100% of effective max | 60-80% |
| 55-74 | 50-75% of effective max | 35-55% |
| 40-54 | 20-45% of effective max | 15-30% |
| <40 | Minimum viable (enough for $10.50 notional) | 10-15% |

**Score <40 still gets a direction + trade params** — the position manager will decide whether to act.

## 7 Filters (check every setup)

### 1. Range Position
- Don't SHORT if price is in bottom 20% of 24h range (Range Position <20%) unless strong continuation evidence (taker sell >65% + falling OI)
- Don't LONG if price is in top 20% of 24h range (Range Position >80%) unless strong continuation evidence (taker buy >65% + stable/rising OI)
- If filter triggers, note it in reasoning and subtract 10 from score

### 2. Structural Contradiction
- Taker selling + bid-heavy orderbook = contradiction → subtract 15 from score
- Taker buying + ask-heavy orderbook = contradiction → subtract 15 from score
- If detected, note it in reasoning

### 3. Choppiness Compound Filter
- ADX < 18 + neutral funding + flat OI = choppy environment → subtract 15 from score
- All three conditions must be true simultaneously
- Still output a direction — just with reduced score

### 4. Counter-Trend Constraints
- Trading against 1h EMA alignment → cap leverage at 60% of effective max, tighten TP to 1.5x ATR
- e.g., shorting when 1h EMA alignment = bullish, or longing when bearish

### 5. ATR-Derived Target Realism
- TP distance must not exceed 2.5x the 1h ATR
- If your TP is farther than 2.5 × ATR(14) on 1h, bring it closer

### 6. Fee Awareness + Break-Even Distance
- TP distance must be ≥ 0.18% from entry (3x round-trip taker fees at 0.03% each side)
- If TP is closer than 0.18%, widen it or subtract 10 from score
- Check: at your chosen leverage, is the liquidation price farther than the SL? If not, reduce leverage.

### 7. Duration-Leverage Coherence
- If TP is >1.5x ATR away (meaning it will take time to hit) AND leverage >60% of effective max → cap leverage at 60%
- Distant targets + high leverage = liquidation risk during normal volatility

## Net Expectancy Check
Before finalizing, estimate: (win% × avg_win) - (loss% × avg_loss). If negative at your SL/TP levels, widen TP or tighten SL until positive. Note the estimate in reasoning.

## CRITICAL: Minimum Order Value — amount × leverage ≥ $10.50
**Every trade MUST satisfy: amount × leverage ≥ $10.50.** Orders below this are REJECTED by the exchange.

## Position Sizing

Given your wallet balance (from market data), use the Quality-Gated Leverage table above. Then:

amount = wallet_balance × margin_pct
notional = amount × leverage

CHECK 1: notional ≥ $10.50? If not, increase leverage.
CHECK 2: amount ≤ wallet_balance? Must be true.

### SL/TP Rules
- Set stop-loss 1-2 ATR from entry at a technical level (EMA, BB band, recent swing)
- Set take-profit at 2:1 or better risk:reward, capped at 2.5x 1h ATR
- TP must be ≥ 0.18% from entry (fee awareness)
- ALWAYS verify: amount × leverage ≥ $10.50

## Cross-Symbol
- BTC often leads ETH and SOL
- Correlated moves = stronger signal
- If all 3 trend the same way, that confirms direction

## Output Format
Output ONLY valid JSON (no markdown fences):
{
  "decisions": [
    {
      "symbol": "PERP_ETH_USDC",
      "direction": "LONG|SHORT",
      "confidence": 72,
      "summary": "Score: 72/100. Funding bullish +10, OI healthy +10, taker 62% buy +10, ...",
      "leverage": 50,
      "positionSize": 0.05,
      "stopLoss": 1960.0,
      "takeProfit": 2060.0,
      "entryPrice": 2000.0,
      "riskLevel": "MEDIUM"
    }
  ]
}

Rules:
- One decision per symbol. Always include ALL symbols.
- direction is ALWAYS "LONG" or "SHORT". Never "HOLD" or "CLOSE".
- confidence: 0-100 integer (maps to quality score). Even low-confidence calls get a direction.
- riskLevel: "LOW" (score <40), "MEDIUM" (40-74), "HIGH" (75-100).
- entryPrice: current mark price or your target entry level.
- leverage: gated by quality score as % of effective max shown in market data.
- FINAL CHECK: positionSize × leverage × entryPrice ≥ $10.50. If not, increase leverage."""


POSITION_PROMPT = """You are a position management analyst. You receive market analysis results (with directional calls and confidence scores) and current position data. Your job is to compare each symbol's analysis against its existing position and decide what to do.

## What You Receive

For each symbol you get:
- **Analysis result**: direction (LONG/SHORT), confidence (0-100), suggested trade params (leverage, size, SL, TP, entry)
- **Position data** (if exists): side, size, entry price, mark price, PnL, leverage, liquidation price, and active TP/SL algo orders

## Reading Position Data

- **PnL**: Unrealized profit/loss. Negative = underwater. But a small loss is NOT a reason to close — the exchange TP/SL handles that.
- **Liquidation price**: If mark price is approaching this, the position is at risk. Factor this into urgency.
- **TAKE_PROFIT / STOP_LOSS orders**: The exchange will auto-exit at these levels. If they're well-placed, HOLD is safer. If the analysis suggests the TP won't be reached, consider CLOSE.

## Decision Matrix — When a Position EXISTS

| Analysis Direction | Confidence | Your Output | Rationale |
|---|---|---|---|
| Same as position | >= 50 | HOLD | Thesis confirmed, let it run |
| Same as position | < 50 | CLOSE | Thesis weakening, exit before SL |
| Opposite of position | < 50 | HOLD | Weak opposing signal, not enough to reverse |
| Opposite of position | >= 50 | Output the NEW direction (LONG/SHORT with trade params from analysis) | Strong opposing signal — reverse the position |

## Decision Matrix — When NO Position Exists

| Confidence | Your Output | Rationale |
|---|---|---|
| >= 40 | Output direction (LONG/SHORT with trade params from analysis) | Sufficient quality to enter |
| < 40 | HOLD | Signal too weak to open a new position |

## For Reversals
When you output a new direction that opposes an existing position, the agent will:
1. Close the existing position first
2. Then open the new position with your suggested trade params

Output the NEW direction with the trade params from the analysis. The agent handles the close.

## Output Format
Output ONLY valid JSON (no markdown fences). Same format as analysis — one decision per symbol, ALL symbols included:
{
  "decisions": [
    {
      "symbol": "PERP_ETH_USDC",
      "direction": "LONG|SHORT|HOLD|CLOSE",
      "confidence": 72,
      "summary": "Position: LONG. Analysis: LONG @ 72/100. Same direction, confidence >= 50 → HOLD.",
      "leverage": 50,
      "positionSize": 0.05,
      "stopLoss": 1960.0,
      "takeProfit": 2060.0,
      "entryPrice": 2000.0,
      "riskLevel": "MEDIUM"
    }
  ]
}

Rules:
- HOLD: leverage=1, positionSize=0, stopLoss=0, takeProfit=0, entryPrice=0
- CLOSE: leverage=1, positionSize=0, stopLoss=0, takeProfit=0, entryPrice=0
- For LONG/SHORT: use the trade params from the analysis result
- Always explain: what the position is, what the analysis says, and why you chose this direction."""


class StrategyEngine:
    """Orchestrates the analysis cycle: snapshot → indicators → LLM → validate."""

    def __init__(
        self,
        config: TradingConfig,
        portfolio: PortfolioState,
        taapi_client: TaapiClient = None,
        liquidation_tracker: LiquidationTracker | None = None,
        funding_history: FundingHistory | None = None,
    ) -> None:
        self.config = config
        self.portfolio = portfolio
        self.risk_manager = RiskManager(config)
        self.taapi_client = taapi_client
        self.liquidation_tracker = liquidation_tracker
        self.funding_history = funding_history
        self.cycles: list[AnalysisCycle] = []
        # Intermediate state between prepare_analysis and process_response
        self._pending_reports: dict[str, IndicatorReport] = {}
        self._pending_prices: dict[str, float] = {}

    def prepare_analysis(
        self,
        snapshots: dict[str, MarketSnapshot],
        prices: dict[str, float],
    ) -> tuple[str, str]:
        """Phase 1: Compute indicators and build prompts.

        Returns (system_prompt, user_prompt) for the LLM to analyze.
        Call process_response() with the LLM's JSON output afterwards.
        """
        # Compute indicators for each symbol
        reports: dict[str, IndicatorReport] = {}
        for symbol, snap in snapshots.items():
            reports[symbol] = compute_indicators(snap)

        # Enrich with TAAPI indicators (if available)
        self._enrich_taapi(reports)

        # Enrich with Fear & Greed Index
        fear_greed = fetch_fear_greed()
        for report in reports.values():
            report.fear_greed_index = fear_greed

        # Enrich with liquidation data
        if self.liquidation_tracker:
            for symbol, report in reports.items():
                liq = self.liquidation_tracker.get_summary(symbol)
                report.derivatives.long_liq_volume = liq.long_liq_volume
                report.derivatives.short_liq_volume = liq.short_liq_volume
                report.derivatives.liq_bias = liq.bias

        # Enrich with funding history
        if self.funding_history:
            for symbol, report in reports.items():
                stats = self.funding_history.get_stats(symbol)
                report.derivatives.funding_avg_24h = stats.avg_24h
                report.derivatives.funding_trend = stats.trend

        # Build prompt
        user_prompt = self._build_user_prompt(reports, prices)
        logger.debug("User prompt (%d chars):\n%s", len(user_prompt), user_prompt)

        # Stash state for process_response
        self._pending_reports = reports
        self._pending_prices = prices

        return ANALYSIS_PROMPT, user_prompt

    def _enrich_taapi(self, reports: dict[str, IndicatorReport]) -> None:
        """Merge TAAPI indicators into existing reports. No-op if client is None."""
        if not self.taapi_client:
            return

        import asyncio

        try:
            # Run the async fetch in a new event loop if needed
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                # We're inside an async context — use a thread to avoid blocking
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    taapi_data = pool.submit(
                        lambda: asyncio.run(
                            self.taapi_client.fetch_indicators(list(reports.keys()))
                        )
                    ).result(timeout=20)
            else:
                taapi_data = asyncio.run(
                    self.taapi_client.fetch_indicators(list(reports.keys()))
                )
        except Exception:
            logger.warning("TAAPI enrichment failed, continuing with existing indicators", exc_info=True)
            return

        for symbol, tf_data in taapi_data.items():
            report = reports.get(symbol)
            if not report:
                continue
            for tf_name, taapi_result in tf_data.items():
                tf_indicators = report.timeframes.get(tf_name)
                if not tf_indicators:
                    continue
                tf_indicators.stoch_rsi_k = taapi_result.stoch_rsi_k
                tf_indicators.stoch_rsi_d = taapi_result.stoch_rsi_d
                tf_indicators.adx = taapi_result.adx
                tf_indicators.cci = taapi_result.cci
                tf_indicators.obv = taapi_result.obv
                tf_indicators.taker_buy_pct = taapi_result.taker_buy_pct
                tf_indicators.taker_sell_pct = taapi_result.taker_sell_pct

    def process_response(
        self,
        response_text: str,
    ) -> list[ValidatedDecision]:
        """Phase 2: Parse JSON response and validate through risk manager.

        Does NOT create in-memory positions. The agent must execute approved
        trades via the x402 VoltPerps API itself.

        Args:
            response_text: Raw JSON string with trading decisions.

        Returns:
            List of validated decisions for the agent to execute.
        """
        reports = self._pending_reports
        prices = self._pending_prices

        cycle = AnalysisCycle(
            portfolio_state_before=self.portfolio.to_summary_dict(prices),
        )

        try:
            # Parse response
            multi_decision = self._parse_response(response_text)
            cycle.llm_output = multi_decision

            # Validate each decision through risk manager
            validated: list[ValidatedDecision] = []
            for decision in multi_decision.decisions:
                price = prices.get(decision.symbol, 0)
                report = reports.get(decision.symbol)
                if not report or price <= 0:
                    v = ValidatedDecision(
                        original=decision,
                        approved=False,
                        rejection_reasons=["No price/indicator data"],
                    )
                else:
                    v = self.risk_manager.validate_decision(
                        decision, self.portfolio, report, price
                    )
                validated.append(v)
                logger.info(
                    "%s %s: %s (approved=%s, lev=%.1f, size=%.4f) %s",
                    decision.symbol,
                    decision.direction.value,
                    decision.summary[:80],
                    v.approved,
                    v.final_leverage,
                    v.final_position_size,
                    v.rejection_reasons if not v.approved else "",
                )

            cycle.validated_decisions = validated
            cycle.portfolio_state_after = self.portfolio.to_summary_dict(prices)

        except Exception as e:
            logger.exception("Decision processing failed")
            cycle.error = str(e)
            validated = []

        self.cycles.append(cycle)
        if self.config.store_reasoning:
            self.portfolio.analysis_cycles.append(cycle)

        # Clean up pending state
        self._pending_reports = {}
        self._pending_prices = {}

        return validated

    def _build_user_prompt(
        self,
        reports: dict[str, IndicatorReport],
        prices: dict[str, float],
    ) -> str:
        """Build the user prompt containing all symbols' data."""
        parts = [f"## Current Market Data — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}\n"]

        for symbol, report in reports.items():
            parts.append(f"### {symbol}")
            parts.append(f"Mark Price: {report.mark_price:.2f}")
            parts.append(f"Index Price: {report.index_price:.2f}")
            parts.append(f"24h Change: {report.ticker_change_24h:.2f}%")
            parts.append(f"24h Volume: {report.ticker_volume_24h:.0f}")
            parts.append(f"24h High: {report.ticker_high_24h:.2f}")
            parts.append(f"24h Low: {report.ticker_low_24h:.2f}")
            parts.append(f"Range Position: {report.range_percentile:.0f}% (0%=at 24h low, 100%=at 24h high)")
            parts.append(f"Vol/OI Ratio: {report.vol_oi_ratio:.2f}")
            effective_max = int(MARKET_MAX_LEVERAGE * self.config.leverage_pct / 100)
            parts.append(f"Effective Max Leverage: {effective_max}x ({self.config.leverage_pct}% of {MARKET_MAX_LEVERAGE}x)")
            parts.append("")

            for tf_name, ti in report.timeframes.items():
                parts.append(f"**{tf_name} Timeframe:**")
                parts.append(f"  Last Close: {ti.last_close:.2f}")
                parts.append(f"  RSI(14): {ti.rsi_14:.1f}")
                parts.append(f"  MACD: line={ti.macd_line:.4f} signal={ti.macd_signal:.4f} hist={ti.macd_histogram:.4f}")
                parts.append(f"  Bollinger: upper={ti.bb_upper:.2f} mid={ti.bb_middle:.2f} lower={ti.bb_lower:.2f} %B={ti.bb_pct_b:.3f}")
                parts.append(f"  EMA: 9={ti.ema_9:.2f} 21={ti.ema_21:.2f} 50={ti.ema_50:.2f} alignment={ti.ema_alignment}")
                parts.append(f"  VWAP: {ti.vwap_value:.2f} (price {ti.price_vs_vwap})")
                parts.append(f"  ATR(14): {ti.atr_14:.4f}")
                parts.append(f"  Recent: {ti.recent_change_pct:+.2f}% last 3 candles, {ti.consecutive_red} red / {ti.consecutive_green} green streak, trend={ti.candle_trend}")
                # TAAPI indicators (only show if populated)
                if ti.adx > 0:
                    adx_label = "strong trend" if ti.adx > 25 else "weak/choppy"
                    parts.append(f"  StochRSI: K={ti.stoch_rsi_k:.1f} D={ti.stoch_rsi_d:.1f}")
                    parts.append(f"  ADX: {ti.adx:.1f} ({adx_label})")
                    parts.append(f"  CCI: {ti.cci:.1f}")
                    parts.append(f"  OBV: {ti.obv:.0f}")
                    parts.append(f"  Taker Flow: {ti.taker_buy_pct:.0f}% buy / {ti.taker_sell_pct:.0f}% sell")
                parts.append("")

            ob = report.orderbook
            parts.append(f"**Orderbook:** imbalance={ob.imbalance:.3f} ({ob.interpretation}) spread={ob.spread_bps:.1f}bps bid_depth={ob.bid_depth:.2f} ask_depth={ob.ask_depth:.2f}")
            if ob.estimated_slippage_bps > 0:
                parts.append(f"  Est. Slippage: {ob.estimated_slippage_bps:.1f}bps")

            dv = report.derivatives
            parts.append(f"**Derivatives:** funding={dv.funding_rate:.6f} ({dv.funding_interpretation}) OI={dv.open_interest:.0f} L/S={dv.ls_ratio:.2f} ({dv.sentiment})")
            if dv.funding_avg_24h != 0 or dv.funding_trend != "flat":
                parts.append(f"  Funding (24h avg): {dv.funding_avg_24h:.6f} trend={dv.funding_trend}")
            if dv.long_liq_volume > 0 or dv.short_liq_volume > 0:
                parts.append(f"  Liquidations (15m): long=${dv.long_liq_volume:.0f} short=${dv.short_liq_volume:.0f} ({dv.liq_bias})")

            parts.append(f"**Volume Delta:** {report.volume_delta:.2f} (ratio={report.volume_delta_ratio:.3f})")

            if report.spot_futures_basis_pct != 0:
                parts.append(f"**Spot-Futures Basis:** {report.spot_futures_basis_pct:.3f}%")

            fg = report.fear_greed_index
            fg_label = "extreme fear" if fg < 25 else "fear" if fg < 40 else "neutral" if fg < 60 else "greed" if fg < 75 else "extreme greed"
            parts.append(f"**Fear & Greed Index:** {fg}/100 ({fg_label})")
            parts.append("")

        parts.append("\nAnalyze all symbols. Output your decisions as JSON.")
        return "\n".join(parts)

    def get_position_prompt(
        self,
        analysis_json: str,
        positions_json: str,
    ) -> tuple[str, str] | None:
        """Build position management prompt for LLM call #2.

        Args:
            analysis_json: Raw JSON string from LLM call #1 (analysis result).
            positions_json: Raw JSON from GET /v1/account/positions.
                Pass the API response directly — the system unwraps it.

        Returns:
            (system_prompt, user_prompt) if any positions exist.
            None if no symbols have positions (use analysis directly).
        """
        try:
            analysis = json.loads(analysis_json)
            positions = json.loads(positions_json)
        except json.JSONDecodeError:
            logger.error("Failed to parse JSON in get_position_prompt")
            return None

        # Handle raw API response: {"success":..., "data": {"positions": [...]}}
        if "data" in positions:
            positions = positions["data"]
        pos_list = positions.get("positions", [])
        if not pos_list:
            return None

        user_prompt = self._build_position_user_prompt(analysis, pos_list)
        return POSITION_PROMPT, user_prompt

    def _build_position_user_prompt(
        self,
        analysis: dict,
        positions: list[dict],
    ) -> str:
        """Build user prompt with per-symbol analysis + position data."""
        # Index positions by symbol
        pos_by_symbol = {}
        for p in positions:
            pos_by_symbol[p.get("symbol", "")] = p

        parts = ["## Analysis Results\n"]

        for decision in analysis.get("decisions", []):
            symbol = decision.get("symbol", "")
            direction = decision.get("direction", decision.get("action", ""))
            confidence = int(decision.get("confidence", 0))
            leverage = decision.get("leverage", 0)
            position_size = decision.get("positionSize", decision.get("position_size", decision.get("quantity", 0)))
            sl = decision.get("stopLoss", decision.get("stop_loss", 0))
            tp = decision.get("takeProfit", decision.get("take_profit", 0))
            entry = decision.get("entryPrice", decision.get("entry_price", 0))
            risk = decision.get("riskLevel", decision.get("risk_level", "MEDIUM"))
            summary = decision.get("summary", decision.get("reasoning", ""))

            parts.append(f"### {symbol}")
            parts.append(f"Direction: {direction} | Confidence: {confidence}/100 | Risk: {risk}")
            parts.append(f"Suggested: leverage={leverage}x, size={position_size}, entry={entry}, SL={sl}, TP={tp}")
            parts.append(f"Summary: {summary}")
            parts.append("")

        parts.append("## Current Positions\n")

        # Show position data for all analysis symbols
        analysis_symbols = [d.get("symbol", "") for d in analysis.get("decisions", [])]
        for symbol in analysis_symbols:
            pos = pos_by_symbol.get(symbol)
            parts.append(f"### {symbol}")
            if pos:
                side = pos.get("side", "unknown").upper()
                size = pos.get("size", 0)
                entry = pos.get("entryPrice", 0)
                mark = pos.get("markPrice", 0)
                pnl = pos.get("pnl", 0)
                leverage = pos.get("leverage", 0)
                liq = pos.get("liquidationPrice", 0)

                parts.append(f"Side: {side} | Size: {size} | Entry: {entry:.2f} | Mark: {mark:.2f}")
                parts.append(f"PnL: ${pnl:.2f} | Leverage: {leverage}x | Liquidation: {liq:.2f}")

                # Show active TP/SL algo orders
                for order in pos.get("associatedOrders", []):
                    algo = order.get("algoType", "")
                    trigger = order.get("triggerPrice", 0)
                    status = order.get("status", "")
                    if algo and trigger:
                        parts.append(f"  {algo}: {trigger:.2f} ({status})")
            else:
                parts.append("(No position)")
            parts.append("")

        parts.append("Compare each symbol's analysis against its position. Output your decisions as JSON.")
        return "\n".join(parts)

    def _parse_response(self, response_text: str) -> MultiSymbolDecision:
        """Parse raw JSON response text into MultiSymbolDecision."""
        content = response_text.strip()

        # Strip markdown code fences if present
        if content.startswith("```"):
            lines = content.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            content = "\n".join(lines)

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Try to extract JSON from the response
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(content[start:end])
            else:
                logger.error("Failed to parse response as JSON: %s", content[:200])
                # Return HOLD for all symbols
                return MultiSymbolDecision(
                    decisions=[
                        TradeDecision.hold(s, "Parse error — defaulting to HOLD")
                        for s in self.config.symbols
                    ],
                    raw_response=content,
                )

        decisions = []
        for d in data.get("decisions", []):
            try:
                decisions.append(TradeDecision.from_dict(d))
            except (KeyError, ValueError) as e:
                logger.warning("Skipping malformed decision: %s (%s)", d, e)

        # Ensure we have a decision for every symbol
        seen_symbols = {d.symbol for d in decisions}
        for s in self.config.symbols:
            if s not in seen_symbols:
                decisions.append(TradeDecision.hold(s, "No decision provided"))

        return MultiSymbolDecision(
            decisions=decisions,
            raw_response=content,
        )

