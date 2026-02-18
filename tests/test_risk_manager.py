"""Tests for the risk manager's graduated reserve system and validation layers."""

import pytest

from src.indicators import IndicatorReport, TimeframeIndicators
from src.models.config import TradingConfig
from src.models.decision import Action, TradeDecision
from src.models.position import ClosedTrade, PortfolioState, Position
from src.risk_manager import RiskManager


@pytest.fixture
def config() -> TradingConfig:
    return TradingConfig(initial_budget=1000.0)


@pytest.fixture
def risk(config) -> RiskManager:
    return RiskManager(config)


@pytest.fixture
def portfolio() -> PortfolioState:
    return PortfolioState(initial_budget=1000.0, current_budget=1000.0, peak_budget=1000.0)


@pytest.fixture
def indicator_report() -> IndicatorReport:
    report = IndicatorReport(symbol="PERP_ETH_USDC", mark_price=3000.0)
    report.timeframes["15m"] = TimeframeIndicators(timeframe="15m", atr_14=30.0)
    return report


def _make_winning_trades(n: int, symbol: str = "PERP_ETH_USDC") -> list[ClosedTrade]:
    return [
        ClosedTrade(
            symbol=symbol, side=Action.LONG, entry_price=3000, exit_price=3060,
            quantity=0.1, leverage=5, margin=60, pnl=6.0, pnl_pct=10.0,
            opened_at=0, close_reason="TP",
        )
        for _ in range(n)
    ]


def _make_losing_trades(n: int, symbol: str = "PERP_ETH_USDC") -> list[ClosedTrade]:
    return [
        ClosedTrade(
            symbol=symbol, side=Action.LONG, entry_price=3000, exit_price=2940,
            quantity=0.1, leverage=5, margin=60, pnl=-6.0, pnl_pct=-10.0,
            opened_at=0, close_reason="SL",
        )
        for _ in range(n)
    ]


class TestBudgetZones:
    def test_default_zones(self, risk, portfolio):
        zones = risk.compute_budget_zones(portfolio)
        # Only free zone accessible by default (0 trades)
        assert zones.free == 700.0
        assert zones.guarded == 200.0
        assert zones.lockout == 50.0
        assert zones.accessible == 700.0  # Only free zone

    def test_guarded_unlocked(self, risk, portfolio):
        # Add 20 winning trades (100% win rate)
        portfolio.closed_trades = _make_winning_trades(20)
        zones = risk.compute_budget_zones(portfolio)
        # Free + guarded should be accessible
        assert zones.accessible == 900.0

    def test_guarded_locked_by_losing_streak(self, risk, portfolio):
        # 17 wins + 3 losses = 85% win rate but on a 3-loss streak
        portfolio.closed_trades = _make_winning_trades(17) + _make_losing_trades(3)
        zones = risk.compute_budget_zones(portfolio)
        # Guarded should be locked due to losing streak
        assert zones.accessible == 700.0

    def test_floor_unlocked(self, risk, portfolio):
        # 30 trades, >60% win rate, losses first so no losing streak at end
        portfolio.closed_trades = _make_losing_trades(5) + _make_winning_trades(25)
        zones = risk.compute_budget_zones(portfolio)
        # Free + guarded + floor
        assert zones.accessible == 950.0

    def test_lockout_never_accessible(self, risk, portfolio):
        portfolio.closed_trades = _make_winning_trades(50)
        zones = risk.compute_budget_zones(portfolio)
        # Even with perfect record, lockout stays locked
        assert zones.accessible == 950.0  # 700 + 200 + 50, not 1000


class TestLeverageCap:
    def test_low_confidence_caps_leverage(self, risk, portfolio, indicator_report):
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.LONG,
            leverage=10, quantity=0.1, stop_loss=2940.0,
            take_profit=3120.0, confidence=0.4,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        # Confidence 0.4 → max leverage 2
        assert result.approved
        assert result.adjusted_leverage == 2.0

    def test_high_confidence_allows_leverage(self, risk, indicator_report):
        # Portfolio with proven track record unlocks guarded zone, removing 3x cap
        portfolio = PortfolioState(initial_budget=1000.0, current_budget=1000.0, peak_budget=1000.0)
        portfolio.closed_trades = _make_winning_trades(20)

        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.LONG,
            leverage=5, quantity=0.05, stop_loss=2940.0,
            take_profit=3120.0, confidence=0.75,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert result.approved
        assert result.adjusted_leverage == 5.0  # Confidence 0.75 (>= guarded threshold) allows up to 7x


class TestStopLossValidation:
    def test_no_stop_loss_rejected(self, risk, portfolio, indicator_report):
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.LONG,
            leverage=5, quantity=0.1, stop_loss=0, confidence=0.6,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert not result.approved
        assert any("stop-loss" in r.lower() for r in result.rejection_reasons)

    def test_long_sl_above_price_rejected(self, risk, portfolio, indicator_report):
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.LONG,
            leverage=5, quantity=0.1, stop_loss=3100.0,
            take_profit=3200.0, confidence=0.6,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert not result.approved

    def test_short_sl_below_price_rejected(self, risk, portfolio, indicator_report):
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.SHORT,
            leverage=5, quantity=0.1, stop_loss=2900.0,
            take_profit=2800.0, confidence=0.6,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert not result.approved

    def test_sl_too_tight_rejected(self, risk, portfolio, indicator_report):
        # SL 5 away, ATR is 30 → 0.17x ATR (< 0.5x minimum)
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.LONG,
            leverage=5, quantity=0.1, stop_loss=2995.0,
            take_profit=3120.0, confidence=0.6,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert not result.approved
        assert any("tight" in r.lower() for r in result.rejection_reasons)


class TestDrawdownCircuitBreaker:
    def test_halt_at_20pct_drawdown(self, risk, indicator_report):
        portfolio = PortfolioState(
            initial_budget=1000.0, current_budget=790.0, peak_budget=1000.0
        )
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.LONG,
            leverage=5, quantity=0.1, stop_loss=2940.0,
            take_profit=3120.0, confidence=0.8,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert not result.approved
        assert any("HALTED" in r for r in result.rejection_reasons)

    def test_size_reduced_at_10pct_drawdown(self, risk, indicator_report):
        portfolio = PortfolioState(
            initial_budget=1000.0, current_budget=895.0, peak_budget=1000.0
        )
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.LONG,
            leverage=2, quantity=1.0, stop_loss=2940.0,
            take_profit=3120.0, confidence=0.6,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        # Should be approved but with reduced quantity
        assert result.approved
        assert any("halved" in r.lower() for r in result.rejection_reasons)


class TestHoldAndClose:
    def test_hold_always_approved(self, risk, portfolio, indicator_report):
        decision = TradeDecision.hold("PERP_ETH_USDC")
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert result.approved

    def test_close_always_approved(self, risk, portfolio, indicator_report):
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.CLOSE, confidence=0.5,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert result.approved


class TestExposureLimits:
    def test_total_exposure_cap(self, risk, indicator_report):
        portfolio = PortfolioState(
            initial_budget=1000.0, current_budget=1000.0, peak_budget=1000.0
        )
        # Add existing position using 750 in margin
        portfolio.open_positions.append(
            Position(
                symbol="PERP_BTC_USDC", side=Action.LONG,
                entry_price=60000, quantity=0.1, leverage=5,
                stop_loss=59000, take_profit=62000, margin=750.0,
            )
        )
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.LONG,
            leverage=5, quantity=0.5, stop_loss=2940.0,
            take_profit=3120.0, confidence=0.6,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        # Should be capped by total exposure limit (80% of 1000 = 800, already using 750)
        if result.approved:
            assert result.margin_required <= 50.0  # Only 50 left under exposure cap

    def test_duplicate_position_rejected(self, risk, portfolio, indicator_report):
        portfolio.open_positions.append(
            Position(
                symbol="PERP_ETH_USDC", side=Action.LONG,
                entry_price=2900, quantity=0.1, leverage=5,
                stop_loss=2850, take_profit=3000, margin=60.0,
            )
        )
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.LONG,
            leverage=5, quantity=0.1, stop_loss=2940.0,
            take_profit=3120.0, confidence=0.6,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert not result.approved
        assert any("already" in r.lower() for r in result.rejection_reasons)
