"""Strategy engine: builds multi-symbol prompts, calls LLM, parses and validates.

This is the central orchestrator that connects indicators, LLM, and risk manager.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .indicators import IndicatorReport, compute_indicators
from .models.config import TradingConfig
from .models.decision import (
    Action,
    AnalysisCycle,
    MultiSymbolDecision,
    TradeDecision,
    ValidatedDecision,
)
from .models.market import MarketSnapshot
from .models.position import PortfolioState, Position
from .risk_manager import RiskManager

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE_PATH = Path(__file__).parent.parent / "prompt_template.md"

SYSTEM_PROMPT = """You are an expert perpetual futures swing trader on Orderly Network. You receive pre-computed technical indicators for multiple symbols and output JSON trading decisions.

## Your Job
- You are a SWING TRADER, not a passive observer. Your job is to find trades, not reasons to avoid them.
- Analyze all symbols for actionable setups. If indicators lean in one direction, TRADE IT.
- HOLD is for genuinely conflicting or flat signals. If 2+ categories agree, that's enough to act.
- The risk manager will protect the downside — your job is to find opportunities.
- Use lower confidence (0.4-0.6) for moderate setups, higher (0.7+) for strong ones.

## Signal Categories

### 1. Trend (15m and 1h)
- EMA alignment: 9 > 21 > 50 = bullish, reverse = bearish
- Price vs VWAP: above = bullish, below = bearish
- MACD direction and histogram

### 2. Momentum (5m and 15m)
- RSI: <40 favors long, >60 favors short. Extremes (<30, >70) are strong signals.
- Bollinger %B: <0.3 = long zone, >0.7 = short zone
- MACD histogram building = momentum, fading = weakening
- Recent candle trend: 3+ red candles = actively dropping (bearish), 3+ green = actively rising (bullish)
- Recent % change: shows actual price movement over last 3 candles — use this to detect sharp moves that lagging indicators miss

### 3. Market Microstructure
- Orderbook imbalance: positive = buy pressure, negative = sell pressure
- Volume delta: positive = buyers aggressive, negative = sellers

### 4. Derivatives Sentiment
- Funding rate direction and magnitude
- Open interest changes
- Long/short ratio extremes = contrarian signal

## When to Trade
- 2 categories agreeing with moderate signals → trade with confidence 0.4-0.6
- 3 categories agreeing → trade with confidence 0.6-0.8
- Strong trend + momentum alignment → trade even without microstructure confirmation
- All symbols moving together in one direction → stronger conviction

## Position Sizing
- Set stop-loss 1-2 ATR from entry at a technical level (EMA, BB band, recent swing)
- Set take-profit at 2:1 or better risk:reward ratio
- Quantity: aim to risk about 1.5-2% of available budget per trade
- Use quantity = (budget * 0.02) / (entry_price * sl_distance_pct) as a guide

## Managing Open Positions
Your SL and TP levels are your trade plan. RESPECT THEM.

**Default is HOLD.** Only CLOSE when the original trade thesis is BROKEN:
- The trend that justified entry has clearly reversed (EMA alignment flipped, MACD crossed against you on 15m+)
- Multiple signal categories that supported the entry now oppose it
- A small unrealized loss or flat P&L is NOT a reason to close — that's normal noise

**Do NOT close because:**
- uPnL is slightly negative — that's what the stop-loss is for
- You're "unsure" — uncertainty means HOLD, not CLOSE
- Only 1 category weakened while others still support the trade
- The position just opened recently and hasn't had time to work

**CLOSE when:**
- 2+ signal categories have flipped against the position direction
- Price action shows clear reversal pattern confirmed by trend indicators
- The reason you entered no longer exists (e.g., bullish EMA alignment is now bearish)

Think of it this way: you set SL/TP for a reason. Let them do their job unless the market structure has fundamentally changed.

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
      "reasoning": "Which categories agree and why"
    }
  ]
}

Rules:
- One decision per symbol. Always include all symbols.
- HOLD: leverage=1, quantity=0, stop_loss=0, take_profit=0, confidence=0
- CLOSE: quantity=0 (system closes full position)
- Confidence: 0.0-1.0
- Leverage: 1-10 (will be capped by risk manager based on confidence)"""


class StrategyEngine:
    """Orchestrates the analysis cycle: snapshot → indicators → LLM → validate."""

    def __init__(
        self,
        config: TradingConfig,
        portfolio: PortfolioState,
    ) -> None:
        self.config = config
        self.portfolio = portfolio
        self.risk_manager = RiskManager(config)
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

        # Build prompt
        user_prompt = self._build_user_prompt(reports, prices)
        logger.debug("User prompt (%d chars):\n%s", len(user_prompt), user_prompt)

        # Stash state for process_response
        self._pending_reports = reports
        self._pending_prices = prices

        return SYSTEM_PROMPT, user_prompt

    def process_response(
        self,
        response_text: str,
    ) -> list[ValidatedDecision]:
        """Phase 2: Parse JSON response, validate through risk manager, execute.

        Args:
            response_text: Raw JSON string with trading decisions.

        Returns:
            List of validated (and possibly executed) decisions.
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

            # Execute approved decisions
            self._execute_decisions(validated, prices)

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

    def check_stop_loss_take_profit(self, prices: dict[str, float]) -> list[str]:
        """Check all open positions for SL/TP hits. Returns list of close messages."""
        messages = []
        positions_to_close = []

        for pos in self.portfolio.open_positions:
            price = prices.get(pos.symbol)
            if price is None:
                continue
            if pos.should_stop_loss(price):
                positions_to_close.append((pos, price, "SL"))
            elif pos.should_take_profit(price):
                positions_to_close.append((pos, price, "TP"))

        for pos, price, reason in positions_to_close:
            trade = self.portfolio.close_position(pos, price, reason)
            msg = (
                f"Closed {pos.symbol} {pos.side.value} @ {price:.2f} "
                f"({reason}) PnL: ${trade.pnl:.2f}"
            )
            messages.append(msg)
            logger.info(msg)

        return messages

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
                parts.append("")

            ob = report.orderbook
            parts.append(f"**Orderbook:** imbalance={ob.imbalance:.3f} ({ob.interpretation}) spread={ob.spread_bps:.1f}bps bid_depth={ob.bid_depth:.2f} ask_depth={ob.ask_depth:.2f}")

            dv = report.derivatives
            parts.append(f"**Derivatives:** funding={dv.funding_rate:.6f} ({dv.funding_interpretation}) OI={dv.open_interest:.0f} L/S={dv.ls_ratio:.2f} ({dv.sentiment})")

            parts.append(f"**Volume Delta:** {report.volume_delta:.2f} (ratio={report.volume_delta_ratio:.3f})")
            parts.append("")

        # Portfolio state
        summary = self.portfolio.to_summary_dict(prices)
        parts.append("## Portfolio State")
        parts.append(f"Budget: ${summary['current_budget']:.2f} (initial: ${summary['initial_budget']:.2f})")
        parts.append(f"Available for trades: ${summary['available_budget']:.2f}")
        parts.append(f"Margin in use: ${summary['margin_in_use']:.2f}")
        parts.append(f"Unrealized PnL: ${summary['unrealized_pnl']:.2f}")
        parts.append(f"Win rate: {summary['win_rate']:.1%} ({summary['total_trades']} trades)")
        parts.append(f"Current losing streak: {summary['losing_streak']}")
        parts.append(f"Drawdown from peak: {summary['drawdown_from_peak']:.1%}")
        parts.append("")

        # Drawdown warning
        dd = self.portfolio.drawdown_from_peak
        if dd >= self.config.risk.drawdown_halt_pct:
            parts.append("**WARNING: TRADING HALTED — drawdown exceeds halt threshold. Output HOLD for all symbols.**")
        elif dd >= self.config.risk.drawdown_reduce_pct:
            parts.append(f"**CAUTION: Position sizes reduced — drawdown at {dd:.1%}.**")

        # Open positions
        if self.portfolio.open_positions:
            parts.append("\n## Open Positions")
            parts.append("**Default action for open positions is HOLD.** Only CLOSE if the entry thesis is broken (see rules above).\n")
            for pos in self.portfolio.open_positions:
                price = prices.get(pos.symbol, pos.entry_price)
                upnl = pos.unrealized_pnl(price)

                # Distance to SL and TP as percentage
                sl_dist_pct = abs(price - pos.stop_loss) / price * 100 if price > 0 else 0
                tp_dist_pct = abs(pos.take_profit - price) / price * 100 if price > 0 else 0

                # Progress toward TP (0% = at entry, 100% = at TP)
                total_range = abs(pos.take_profit - pos.entry_price)
                if total_range > 0:
                    if pos.side == Action.LONG:
                        progress = (price - pos.entry_price) / total_range * 100
                    else:
                        progress = (pos.entry_price - price) / total_range * 100
                else:
                    progress = 0

                # Time held
                held_seconds = time.time() - pos.opened_at
                held_min = int(held_seconds / 60)

                parts.append(
                    f"- {pos.symbol} {pos.side.value} @ {pos.entry_price:.2f} "
                    f"(qty={pos.quantity:.4f}, lev={pos.leverage:.0f}x, uPnL=${upnl:.2f})\n"
                    f"  SL={pos.stop_loss:.2f} ({sl_dist_pct:.1f}% away) | "
                    f"TP={pos.take_profit:.2f} ({tp_dist_pct:.1f}% away) | "
                    f"Progress to TP: {progress:.0f}% | Held: {held_min}min"
                )

        # Recent trades
        if summary["recent_trades"]:
            parts.append("\n## Recent Closed Trades")
            for t in summary["recent_trades"]:
                parts.append(f"- {t['symbol']} {t['side']} PnL=${t['pnl']:.2f} ({t['reason']})")

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

    def _execute_decisions(
        self,
        validated: list[ValidatedDecision],
        prices: dict[str, float],
    ) -> None:
        """Execute approved decisions (paper trading: update in-memory portfolio)."""
        for v in validated:
            if not v.approved:
                continue

            decision = v.original

            if decision.action == Action.CLOSE:
                # Close all positions for this symbol
                positions = self.portfolio.get_positions_for_symbol(decision.symbol)
                price = prices.get(decision.symbol, 0)
                for pos in positions:
                    trade = self.portfolio.close_position(pos, price, "LLM_CLOSE")
                    logger.info(
                        "Closed %s %s @ %.2f PnL=$%.2f",
                        pos.symbol, pos.side.value, price, trade.pnl,
                    )

            elif decision.action in (Action.LONG, Action.SHORT):
                price = prices.get(decision.symbol, 0)
                if price <= 0:
                    continue
                notional = v.final_quantity * price
                margin = notional / v.final_leverage if v.final_leverage > 0 else notional

                position = Position(
                    symbol=decision.symbol,
                    side=decision.action,
                    entry_price=price,
                    quantity=v.final_quantity,
                    leverage=v.final_leverage,
                    stop_loss=decision.stop_loss,
                    take_profit=decision.take_profit,
                    margin=margin,
                    confidence=decision.confidence,
                    reasoning=decision.reasoning,
                )
                self.portfolio.open_position(position)
                logger.info(
                    "Opened %s %s @ %.2f qty=%.4f lev=%.1fx margin=$%.2f",
                    decision.symbol, decision.action.value, price,
                    v.final_quantity, v.final_leverage, margin,
                )
