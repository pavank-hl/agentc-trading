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

SYSTEM_PROMPT = """You are an expert perpetual futures swing trader on Orderly Network. You receive pre-computed technical indicators for multiple symbols and output JSON trading decisions.

## Your Job
- You are a SWING TRADER, not a passive observer. Your job is to find trades, not reasons to avoid them.
- Analyze all symbols for actionable setups. HOLD is for genuinely conflicting, flat, or low-quality signals.
- The risk manager will protect the downside — your job is to find high-quality opportunities.

## Decision Framework — Two Layers

### Layer 1: Foundational Edge (70% of decision weight) — DETERMINES DIRECTION
These are LEADING signals. They tell you which way to trade BEFORE price confirms.

**Funding + OI:**
- Funding rate direction, magnitude, and 24h trend
- Price + OI relationship: price up + OI stable = healthy; price up + OI surging + high funding = crowded/fragile
- OI flush (sharp OI drop + price move) = forced liquidation cascade — trade the continuation

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
- Est. slippage: >5bps = thin book (reduce size); <2bps = deep book (full size OK)

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
For every potential LONG/SHORT, compute a quality score:

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

## Quality-Gated Leverage

| Score | Leverage Range | Margin % of Wallet | Action |
|-------|---------------|-------------------|--------|
| 75-100 | 80x-100x | 60-80% | High conviction trade |
| 55-74 | 45-75x | 35-55% | Standard trade |
| 40-54 | 20-40x | 15-30% | Cautious trade |
| <40 | — | — | HOLD — insufficient quality |

## 7 Filters (check every setup)

### 1. Range Position
- Don't SHORT if price is in bottom 20% of 24h range (Range Position <20%) unless strong continuation evidence (taker sell >65% + falling OI)
- Don't LONG if price is in top 20% of 24h range (Range Position >80%) unless strong continuation evidence (taker buy >65% + stable/rising OI)

### 2. Structural Contradiction
- Taker selling + bid-heavy orderbook = contradiction → subtract 15 from score
- Taker buying + ask-heavy orderbook = contradiction → subtract 15 from score
- If detected, note it in reasoning

### 3. Choppiness Compound Filter
- ADX < 18 + neutral funding + flat OI = choppy environment → HOLD
- All three conditions must be true simultaneously

### 4. Counter-Trend Constraints
- Trading against 1h EMA alignment → cap leverage at 60x, tighten TP to 1.5x ATR
- e.g., shorting when 1h EMA alignment = bullish, or longing when bearish

### 5. ATR-Derived Target Realism
- TP distance must not exceed 2.5x the 1h ATR
- If your TP is farther than 2.5 × ATR(14) on 1h, bring it closer

### 6. Fee Awareness
- TP distance must be ≥ 0.18% from entry (3x round-trip taker fees at 0.03% each side)
- If TP is closer than 0.18%, the trade is unprofitable after fees → HOLD

### 7. Duration-Leverage Coherence
- If TP is >1.5x ATR away (meaning it will take time to hit) AND leverage >60x → cap leverage at 60x
- Distant targets + high leverage = liquidation risk during normal volatility

## CRITICAL: Minimum Order Value — amount × leverage ≥ $10.50
**Every trade MUST satisfy: amount × leverage ≥ $10.50.** Orders below this are REJECTED by the exchange.

## Position Sizing

Given your wallet balance (from wallet skill), use the Quality-Gated Leverage table above. Then:

amount = wallet_balance × margin_pct
notional = amount × leverage

CHECK 1: notional ≥ $10.50? If not, increase leverage.
CHECK 2: amount ≤ wallet_balance? Must be true.

### Examples with $2 wallet:
Score 85: amount=$1.50 (75%), leverage=100x → $150 position ✓
Score 65: amount=$0.90 (45%), leverage=60x → $54 position ✓
Score 45: amount=$0.50 (25%), leverage=30x → $15 position ✓
Score 35: → HOLD

### SL/TP Rules
- Set stop-loss 1-2 ATR from entry at a technical level (EMA, BB band, recent swing)
- Set take-profit at 2:1 or better risk:reward, capped at 2.5x 1h ATR
- TP must be ≥ 0.18% from entry (fee awareness)
- ALWAYS verify: amount × leverage ≥ $10.50

## Managing Open Positions
**You must check real positions via GET /v1/account/positions every cycle.** Never assume a position exists from a previous cycle.

The exchange handles SL/TP automatically via algo orders. Your job is to decide whether to HOLD or CLOSE based on current signals.

**Default is HOLD.** Only CLOSE when the original trade thesis is BROKEN:
- The trend that justified entry has clearly reversed (EMA alignment flipped, MACD crossed against you on 15m+)
- Multiple signal categories that supported the entry now oppose it
- A small unrealized loss or flat P&L is NOT a reason to close — the exchange TP/SL handles exits

**CLOSE when:**
- 2+ signal categories have flipped against the position direction
- Price action shows clear reversal pattern confirmed by trend indicators
- The reason you entered no longer exists (e.g., bullish EMA alignment is now bearish)

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
      "action": "LONG|SHORT|HOLD|CLOSE",
      "leverage": 1,
      "quantity": 0.0,
      "stop_loss": 0.0,
      "take_profit": 0.0,
      "confidence": 0.0,
      "reasoning": "Score: XX/100. Which layers agree and why. Filters checked."
    }
  ]
}

Rules:
- One decision per symbol. Always include all symbols.
- HOLD: leverage=1, quantity=0, stop_loss=0, take_profit=0, confidence=0
- CLOSE: quantity=0 (system closes full position)
- Confidence: 0.0-1.0 (maps roughly to score/100)
- Leverage: USE HIGH LEVERAGE gated by quality score. Your stop-loss protects the downside.
- FINAL CHECK: amount × leverage ≥ $10.50. If not, increase leverage."""


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

        return SYSTEM_PROMPT, user_prompt

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
                    "%s %s: %s (approved=%s, lev=%.1f, qty=%.4f) %s",
                    decision.symbol,
                    decision.action.value,
                    decision.reasoning[:80],
                    v.approved,
                    v.final_leverage,
                    v.final_quantity,
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

