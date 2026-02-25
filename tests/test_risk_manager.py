"""Tests for the risk manager's validation layers."""

import pytest

from src.indicators import IndicatorReport, TimeframeIndicators
from src.models.config import TradingConfig
from src.models.decision import Action, TradeDecision
from src.models.position import ClosedTrade, PortfolioState, Position
from src.risk_manager import RiskManager


@pytest.fixture
def config() -> TradingConfig:
    return TradingConfig()


@pytest.fixture
def risk(config) -> RiskManager:
    return RiskManager(config)


@pytest.fixture
def portfolio() -> PortfolioState:
    return PortfolioState()


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


class TestConfidenceValidation:
    def test_low_confidence_rejected(self, risk, portfolio, indicator_report):
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.LONG,
            leverage=5, quantity=0.1, stop_loss=2940.0,
            take_profit=3120.0, confidence=0.05,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert not result.approved
        assert any("confidence" in r.lower() for r in result.rejection_reasons)

    def test_leverage_passed_through(self, risk, portfolio, indicator_report):
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.LONG,
            leverage=5, quantity=0.05, stop_loss=2940.0,
            take_profit=3120.0, confidence=0.75,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert result.approved
        assert result.adjusted_leverage == 5.0


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
        # SL 5 away, ATR is 30 -> 0.17x ATR (< 0.5x minimum)
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.LONG,
            leverage=5, quantity=0.1, stop_loss=2995.0,
            take_profit=3120.0, confidence=0.6,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert not result.approved
        assert any("tight" in r.lower() for r in result.rejection_reasons)


class TestRiskReward:
    def test_bad_rr_rejected(self, risk, portfolio, indicator_report):
        # SL 60 away, TP 30 away -> R:R 0.5 (below 1.5 minimum)
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.LONG,
            leverage=5, quantity=0.1, stop_loss=2940.0,
            take_profit=3030.0, confidence=0.6,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert not result.approved
        assert any("r:r" in r.lower() for r in result.rejection_reasons)


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


class TestPositionConflicts:
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

    def test_opposite_position_rejected(self, risk, portfolio, indicator_report):
        portfolio.open_positions.append(
            Position(
                symbol="PERP_ETH_USDC", side=Action.LONG,
                entry_price=2900, quantity=0.1, leverage=5,
                stop_loss=2850, take_profit=3000, margin=60.0,
            )
        )
        decision = TradeDecision(
            symbol="PERP_ETH_USDC", action=Action.SHORT,
            leverage=5, quantity=0.1, stop_loss=3060.0,
            take_profit=2880.0, confidence=0.6,
        )
        result = risk.validate_decision(decision, portfolio, indicator_report, 3000.0)
        assert not result.approved
        assert any("opposite" in r.lower() for r in result.rejection_reasons)
