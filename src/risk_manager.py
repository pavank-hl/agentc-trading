"""Risk manager with graduated reserve system and 9-layer validation.

The risk manager has absolute veto power over every LLM decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .indicators import IndicatorReport
from .models.config import TradingConfig
from .models.decision import Action, TradeDecision, ValidatedDecision
from .models.position import PortfolioState

logger = logging.getLogger(__name__)


@dataclass
class BudgetZones:
    """Computed budget zones based on graduated reserve."""

    total: float = 0.0
    free: float = 0.0
    guarded: float = 0.0
    floor: float = 0.0
    lockout: float = 0.0
    accessible: float = 0.0  # How much is actually usable right now


class RiskManager:
    """Graduated reserve system + multi-layer validation."""

    def __init__(self, config: TradingConfig) -> None:
        self.config = config
        self.risk = config.risk
        self.reserve = config.risk.reserve
        self.leverage_scale = config.leverage_scale

    def compute_budget_zones(self, portfolio: PortfolioState) -> BudgetZones:
        """Determine how much budget is accessible given current performance."""
        total = portfolio.current_budget
        r = self.reserve

        zones = BudgetZones(
            total=total,
            free=total * r.free_pct,
            guarded=total * r.guarded_pct,
            floor=total * r.floor_pct,
            lockout=total * r.lockout_pct,
        )

        # Always have the free zone
        zones.accessible = zones.free

        # Check guarded zone unlock conditions
        if self._guarded_unlocked(portfolio):
            zones.accessible += zones.guarded

        # Check floor zone unlock conditions
        if self._floor_unlocked(portfolio):
            zones.accessible += zones.floor

        # Subtract margin already in use
        zones.accessible = max(0, zones.accessible - portfolio.total_margin_in_use)

        return zones

    def _guarded_unlocked(self, portfolio: PortfolioState) -> bool:
        r = self.reserve
        if portfolio.total_trades < r.guarded_min_trades:
            return False
        if portfolio.win_rate_last_n(r.guarded_min_trades) < r.guarded_win_rate:
            return False
        if portfolio.losing_streak >= r.guarded_max_losing_streak:
            return False
        return True

    def _floor_unlocked(self, portfolio: PortfolioState) -> bool:
        r = self.reserve
        if portfolio.total_trades < r.floor_min_trades:
            return False
        if portfolio.win_rate_last_n(r.floor_min_trades) < r.floor_win_rate:
            return False
        return True

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

        # Layer 1: Drawdown circuit breaker
        drawdown = portfolio.drawdown_from_peak
        if drawdown >= self.risk.drawdown_halt_pct:
            reasons.append(
                f"HALTED: drawdown {drawdown:.1%} >= {self.risk.drawdown_halt_pct:.0%} halt threshold"
            )
            result.rejection_reasons = reasons
            return result

        size_multiplier = 1.0
        if drawdown >= self.risk.drawdown_reduce_pct:
            size_multiplier = 0.5
            reasons.append(f"Size halved: drawdown {drawdown:.1%} >= reduce threshold")

        # Layer 2: Confidence validation
        confidence = max(0.0, min(1.0, decision.confidence))
        if confidence < 0.1:
            reasons.append(f"Confidence too low: {confidence}")
            result.rejection_reasons = reasons
            return result

        # Layer 3: Leverage cap by confidence
        max_lev = self.leverage_scale.max_leverage_for(confidence)
        adjusted_leverage = min(decision.leverage, max_lev)

        # Layer 4: Budget zone access
        zones = self.compute_budget_zones(portfolio)

        # For guarded zone, also check per-decision confidence and R:R
        if portfolio.available_budget - zones.free > 0:
            # We'd be dipping into guarded territory
            if confidence < self.reserve.guarded_min_confidence:
                # Can only use free zone, and cap leverage
                zones.accessible = min(
                    zones.accessible,
                    max(0, zones.free - portfolio.total_margin_in_use),
                )
                if adjusted_leverage > self.reserve.guarded_max_leverage:
                    adjusted_leverage = min(adjusted_leverage, self.reserve.guarded_max_leverage)

        if zones.accessible <= 0:
            reasons.append("No accessible budget (all zones locked or in use)")
            result.rejection_reasons = reasons
            return result

        # Layer 5: Stop-loss validation
        if decision.stop_loss <= 0:
            reasons.append("No stop-loss provided")
            result.rejection_reasons = reasons
            return result

        sl_distance = abs(current_price - decision.stop_loss)
        sl_pct = sl_distance / current_price if current_price > 0 else 0

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

        # Layer 6: Risk/reward ratio
        if decision.take_profit > 0:
            tp_distance = abs(decision.take_profit - current_price)
            rr_ratio = tp_distance / sl_distance if sl_distance > 0 else 0

            # Check R:R requirements based on which zone we're accessing
            min_rr = 1.5  # base minimum
            if zones.accessible > zones.free:
                min_rr = max(min_rr, self.reserve.guarded_min_rr)
            if rr_ratio < min_rr:
                reasons.append(f"R:R ratio {rr_ratio:.2f} below minimum {min_rr}")
                result.rejection_reasons = reasons
                return result

        # Layer 7: Position sizing (2% max loss rule)
        max_loss_budget = zones.accessible * self.risk.max_loss_per_trade_pct
        max_loss_budget *= size_multiplier

        if sl_pct > 0:
            max_quantity = max_loss_budget / (current_price * sl_pct)
        else:
            max_quantity = 0

        adjusted_quantity = min(decision.quantity, max_quantity) if max_quantity > 0 else 0

        if adjusted_quantity <= 0:
            reasons.append("Position size rounds to zero after risk limits")
            result.rejection_reasons = reasons
            return result

        # Layer 8: Margin and total exposure check
        notional = adjusted_quantity * current_price
        margin_needed = notional / adjusted_leverage if adjusted_leverage > 0 else notional

        if margin_needed > zones.accessible:
            # Reduce to fit
            margin_needed = zones.accessible
            notional = margin_needed * adjusted_leverage
            adjusted_quantity = notional / current_price if current_price > 0 else 0

        total_exposure = portfolio.total_margin_in_use + margin_needed
        max_exposure = portfolio.current_budget * self.risk.max_total_exposure_pct
        if total_exposure > max_exposure:
            # Reduce margin to fit
            allowed_margin = max(0, max_exposure - portfolio.total_margin_in_use)
            if allowed_margin <= 0:
                reasons.append("Total exposure limit reached")
                result.rejection_reasons = reasons
                return result
            margin_needed = allowed_margin
            notional = margin_needed * adjusted_leverage
            adjusted_quantity = notional / current_price if current_price > 0 else 0

        # Layer 9: Existing position conflict
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

        # Compute final max loss
        max_loss = adjusted_quantity * sl_distance

        # All checks passed
        result.approved = True
        result.adjusted_leverage = adjusted_leverage
        result.adjusted_quantity = adjusted_quantity
        result.margin_required = margin_needed
        result.max_loss = max_loss
        result.rejection_reasons = reasons  # May contain info messages
        return result

    def _get_atr(self, report: IndicatorReport) -> float:
        """Get ATR from the best available timeframe (prefer 15m, fallback to 5m, 1h)."""
        for tf in ["15m", "5m", "1h"]:
            if tf in report.timeframes:
                val = report.timeframes[tf].atr_14
                if val > 0:
                    return val
        return 0.0
