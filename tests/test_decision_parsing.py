"""Tests for decision parsing and portfolio operations."""

import pytest

from src.models.decision import Action, MultiSymbolDecision, TradeDecision
from src.models.position import ClosedTrade, PortfolioState, Position


class TestTradeDecision:
    def test_from_dict(self):
        d = {
            "symbol": "PERP_ETH_USDC",
            "direction": "LONG",
            "leverage": 5,
            "positionSize": 0.267,
            "stopLoss": 2940.0,
            "takeProfit": 3120.0,
            "entryPrice": 3000.0,
            "confidence": 72,
            "riskLevel": "MEDIUM",
            "summary": "Bullish setup",
        }
        td = TradeDecision.from_dict(d)
        assert td.symbol == "PERP_ETH_USDC"
        assert td.direction == Action.LONG
        assert td.leverage == 5.0
        assert td.position_size == 0.267
        assert td.confidence == 72
        assert td.entry_price == 3000.0
        assert td.risk_level == "MEDIUM"
        assert td.summary == "Bullish setup"

    def test_from_dict_legacy_keys(self):
        """Backward compat: accept old snake_case keys."""
        d = {
            "symbol": "PERP_ETH_USDC",
            "action": "LONG",
            "quantity": 0.1,
            "stop_loss": 2940.0,
            "take_profit": 3120.0,
            "confidence": 60,
            "reasoning": "Old format",
        }
        td = TradeDecision.from_dict(d)
        assert td.direction == Action.LONG
        assert td.position_size == 0.1
        assert td.stop_loss == 2940.0
        assert td.take_profit == 3120.0
        assert td.summary == "Old format"

    def test_from_dict_case_insensitive(self):
        d = {"symbol": "PERP_BTC_USDC", "direction": "hold"}
        td = TradeDecision.from_dict(d)
        assert td.direction == Action.HOLD

    def test_hold_factory(self):
        td = TradeDecision.hold("PERP_SOL_USDC", "No signal")
        assert td.direction == Action.HOLD
        assert td.symbol == "PERP_SOL_USDC"
        assert td.position_size == 0
        assert td.summary == "No signal"


class TestPosition:
    def test_unrealized_pnl_long(self):
        pos = Position(
            symbol="PERP_ETH_USDC", side=Action.LONG,
            entry_price=3000.0, quantity=0.1, leverage=5,
            stop_loss=2940.0, take_profit=3120.0, margin=60.0,
        )
        assert pos.unrealized_pnl(3060.0) == pytest.approx(6.0)  # 0.1 * 60
        assert pos.unrealized_pnl(2940.0) == pytest.approx(-6.0)

    def test_unrealized_pnl_short(self):
        pos = Position(
            symbol="PERP_ETH_USDC", side=Action.SHORT,
            entry_price=3000.0, quantity=0.1, leverage=5,
            stop_loss=3060.0, take_profit=2880.0, margin=60.0,
        )
        assert pos.unrealized_pnl(2940.0) == pytest.approx(6.0)
        assert pos.unrealized_pnl(3060.0) == pytest.approx(-6.0)

    def test_stop_loss_trigger(self):
        pos = Position(
            symbol="PERP_ETH_USDC", side=Action.LONG,
            entry_price=3000.0, quantity=0.1, leverage=5,
            stop_loss=2940.0, take_profit=3120.0, margin=60.0,
        )
        assert pos.should_stop_loss(2939.0) is True
        assert pos.should_stop_loss(2940.0) is True
        assert pos.should_stop_loss(2950.0) is False

    def test_take_profit_trigger(self):
        pos = Position(
            symbol="PERP_ETH_USDC", side=Action.SHORT,
            entry_price=3000.0, quantity=0.1, leverage=5,
            stop_loss=3060.0, take_profit=2880.0, margin=60.0,
        )
        assert pos.should_take_profit(2880.0) is True
        assert pos.should_take_profit(2870.0) is True
        assert pos.should_take_profit(2900.0) is False


class TestPortfolioState:
    def test_open_and_close_position(self):
        portfolio = PortfolioState()

        pos = Position(
            symbol="PERP_ETH_USDC", side=Action.LONG,
            entry_price=3000.0, quantity=0.1, leverage=5,
            stop_loss=2940.0, take_profit=3120.0, margin=60.0,
        )
        portfolio.open_position(pos)
        assert portfolio.total_margin_in_use == 60.0
        assert len(portfolio.open_positions) == 1

        # Close at profit
        trade = portfolio.close_position(pos, 3060.0, "TP")
        assert trade.pnl == pytest.approx(6.0)
        assert trade.is_win is True
        assert len(portfolio.open_positions) == 0

    def test_win_rate(self):
        portfolio = PortfolioState()
        portfolio.closed_trades = [
            ClosedTrade(symbol="X", side=Action.LONG, entry_price=100, exit_price=110,
                        quantity=1, leverage=1, margin=100, pnl=10, pnl_pct=10, opened_at=0, close_reason="TP"),
            ClosedTrade(symbol="X", side=Action.LONG, entry_price=100, exit_price=90,
                        quantity=1, leverage=1, margin=100, pnl=-10, pnl_pct=-10, opened_at=0, close_reason="SL"),
            ClosedTrade(symbol="X", side=Action.LONG, entry_price=100, exit_price=105,
                        quantity=1, leverage=1, margin=100, pnl=5, pnl_pct=5, opened_at=0, close_reason="TP"),
        ]
        assert portfolio.win_rate == pytest.approx(2 / 3)
        assert portfolio.win_rate_last_n(2) == pytest.approx(0.5)

    def test_losing_streak(self):
        portfolio = PortfolioState()
        portfolio.closed_trades = [
            ClosedTrade(symbol="X", side=Action.LONG, entry_price=100, exit_price=110,
                        quantity=1, leverage=1, margin=100, pnl=10, pnl_pct=10, opened_at=0, close_reason="TP"),
            ClosedTrade(symbol="X", side=Action.LONG, entry_price=100, exit_price=95,
                        quantity=1, leverage=1, margin=100, pnl=-5, pnl_pct=-5, opened_at=0, close_reason="SL"),
            ClosedTrade(symbol="X", side=Action.LONG, entry_price=100, exit_price=90,
                        quantity=1, leverage=1, margin=100, pnl=-10, pnl_pct=-10, opened_at=0, close_reason="SL"),
        ]
        assert portfolio.losing_streak == 2

    def test_summary_dict(self):
        portfolio = PortfolioState()
        summary = portfolio.to_summary_dict({})
        assert summary["win_rate"] == 0.0
        assert summary["margin_in_use"] == 0.0
        assert isinstance(summary["open_positions"], list)
        assert isinstance(summary["recent_trades"], list)
