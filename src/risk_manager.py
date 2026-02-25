"""Risk manager with multi-layer validation.

The risk manager has absolute veto power over every LLM decision.
"""

from __future__ import annotations

import logging

from .indicators import IndicatorReport
from .models.config import TradingConfig
from .models.decision import Action, TradeDecision, ValidatedDecision
from .models.position import PortfolioState

logger = logging.getLogger(__name__)


class RiskManager:
    """Multi-layer validation for trade decisions."""

    def __init__(self, config: TradingConfig) -> None:
        self.config = config
        self.risk = config.risk

    def validate_decision(
        self,
        decision: TradeDecision,
        portfolio: PortfolioState,
        indicator_report: IndicatorReport,
        current_price: float,
    ) -> ValidatedDecision:
        """Run all validation layers on a single decision.

        Returns a ValidatedDecision which may be approved (possibly with
        adjusted leverage/quantity) or rejected with reasons.
        """
        result = ValidatedDecision(original=decision)
        reasons: list[str] = []

        # Layer 0: HOLD/CLOSE pass through (no risk check needed)
        if decision.action in (Action.HOLD, Action.CLOSE):
            result.approved = True
            result.adjusted_leverage = decision.leverage
            result.adjusted_quantity = decision.quantity
            return result

        # Layer 1: Confidence validation
        confidence = max(0.0, min(1.0, decision.confidence))
        if confidence < 0.1:
            reasons.append(f"Confidence too low: {confidence}")
            result.rejection_reasons = reasons
            return result

        adjusted_leverage = decision.leverage

        # Layer 2: Stop-loss validation
        if decision.stop_loss <= 0:
            reasons.append("No stop-loss provided")
            result.rejection_reasons = reasons
            return result

        sl_distance = abs(current_price - decision.stop_loss)

        # Validate SL direction
        if decision.action == Action.LONG and decision.stop_loss >= current_price:
            reasons.append("LONG stop-loss must be below current price")
            result.rejection_reasons = reasons
            return result
        if decision.action == Action.SHORT and decision.stop_loss <= current_price:
            reasons.append("SHORT stop-loss must be above current price")
            result.rejection_reasons = reasons
            return result

        # ATR-based SL validation
        atr_value = self._get_atr(indicator_report)
        if atr_value > 0:
            sl_atr_ratio = sl_distance / atr_value
            if sl_atr_ratio < self.risk.min_sl_atr_multiple:
                reasons.append(
                    f"SL too tight: {sl_atr_ratio:.2f}x ATR (min {self.risk.min_sl_atr_multiple}x)"
                )
                result.rejection_reasons = reasons
                return result
            if sl_atr_ratio > self.risk.max_sl_atr_multiple:
                reasons.append(
                    f"SL too wide: {sl_atr_ratio:.2f}x ATR (max {self.risk.max_sl_atr_multiple}x)"
                )
                result.rejection_reasons = reasons
                return result

        # Layer 3: Risk/reward ratio
        if decision.take_profit > 0:
            tp_distance = abs(decision.take_profit - current_price)
            rr_ratio = tp_distance / sl_distance if sl_distance > 0 else 0
            min_rr = 1.5
            if rr_ratio < min_rr:
                reasons.append(f"R:R ratio {rr_ratio:.2f} below minimum {min_rr}")
                result.rejection_reasons = reasons
                return result

        # Layer 4: Existing position conflict
        existing = portfolio.get_positions_for_symbol(decision.symbol)
        for pos in existing:
            if pos.side == decision.action:
                reasons.append(
                    f"Already have {pos.side.value} position on {decision.symbol}"
                )
                result.rejection_reasons = reasons
                return result
            else:
                # Opposite direction — must CLOSE existing first
                reasons.append(
                    f"Have opposite {pos.side.value} position on {decision.symbol} — CLOSE it first"
                )
                result.rejection_reasons = reasons
                return result

        adjusted_quantity = decision.quantity

        # Compute final max loss
        max_loss = adjusted_quantity * sl_distance

        # Compute margin
        notional = adjusted_quantity * current_price
        margin_needed = notional / adjusted_leverage if adjusted_leverage > 0 else notional

        # All checks passed
        result.approved = True
        result.adjusted_leverage = adjusted_leverage
        result.adjusted_quantity = adjusted_quantity
        result.margin_required = margin_needed
        result.max_loss = max_loss
        result.rejection_reasons = reasons
        return result

    def _get_atr(self, report: IndicatorReport) -> float:
        """Get ATR from the best available timeframe (prefer 15m, fallback to 5m, 1h)."""
        for tf in ["15m", "5m", "1h"]:
            if tf in report.timeframes:
                val = report.timeframes[tf].atr_14
                if val > 0:
                    return val
        return 0.0
