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
        adjusted leverage/position size) or rejected with reasons.
        """
        result = ValidatedDecision(original=decision)
        reasons: list[str] = []

        # Layer 0: HOLD/CLOSE pass through (no risk check needed)
        if decision.direction in (Action.HOLD, Action.CLOSE):
            result.approved = True
            result.adjusted_leverage = decision.leverage
            result.adjusted_position_size = decision.position_size
            return result

        # Layer 1: Confidence validation
        # LONG/SHORT require confidence >= 40 (quality gate for new trades)
        # HOLD/CLOSE are already handled above in Layer 0
        confidence = max(0, min(100, decision.confidence))
        if confidence < 40:
            reasons.append(f"Confidence {confidence:.0f} below 40 quality gate")
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
        if decision.direction == Action.LONG and decision.stop_loss >= current_price:
            reasons.append("LONG stop-loss must be below current price")
            result.rejection_reasons = reasons
            return result
        if decision.direction == Action.SHORT and decision.stop_loss <= current_price:
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

        adjusted_position_size = decision.position_size

        # Layer 4: Minimum order value ($10.50)
        notional = adjusted_position_size * current_price
        order_value = notional * adjusted_leverage if adjusted_leverage > 0 else notional
        if order_value < 10.50:
            reasons.append(
                f"Order value ${order_value:.2f} below $10.50 minimum "
                f"(size={adjusted_position_size}, price={current_price:.2f}, lev={adjusted_leverage})"
            )
            result.rejection_reasons = reasons
            return result

        # Compute final max loss
        max_loss = adjusted_position_size * sl_distance

        # Compute margin
        margin_needed = notional / adjusted_leverage if adjusted_leverage > 0 else notional

        # All checks passed
        result.approved = True
        result.adjusted_leverage = adjusted_leverage
        result.adjusted_position_size = adjusted_position_size
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
