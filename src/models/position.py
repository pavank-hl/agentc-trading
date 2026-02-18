"""Position and portfolio state models."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .decision import Action


@dataclass
class Position:
    """An open position (paper or live)."""

    symbol: str
    side: Action  # LONG or SHORT
    entry_price: float
    quantity: float
    leverage: float
    stop_loss: float
    take_profit: float
    margin: float
    opened_at: float = field(default_factory=time.time)
    confidence: float = 0.0
    reasoning: str = ""

    @property
    def notional(self) -> float:
        return self.quantity * self.entry_price

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == Action.LONG:
            return self.quantity * (current_price - self.entry_price)
        elif self.side == Action.SHORT:
            return self.quantity * (self.entry_price - current_price)
        return 0.0

    def unrealized_pnl_pct(self, current_price: float) -> float:
        if self.margin == 0:
            return 0.0
        return self.unrealized_pnl(current_price) / self.margin * 100

    def should_stop_loss(self, current_price: float) -> bool:
        if self.stop_loss <= 0:
            return False
        if self.side == Action.LONG:
            return current_price <= self.stop_loss
        elif self.side == Action.SHORT:
            return current_price >= self.stop_loss
        return False

    def should_take_profit(self, current_price: float) -> bool:
        if self.take_profit <= 0:
            return False
        if self.side == Action.LONG:
            return current_price >= self.take_profit
        elif self.side == Action.SHORT:
            return current_price <= self.take_profit
        return False


@dataclass
class ClosedTrade:
    """A completed trade with realized PnL."""

    symbol: str
    side: Action
    entry_price: float
    exit_price: float
    quantity: float
    leverage: float
    margin: float
    pnl: float
    pnl_pct: float
    opened_at: float
    closed_at: float = field(default_factory=time.time)
    close_reason: str = ""  # "SL", "TP", "LLM_CLOSE", "TIME"

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


@dataclass
class PortfolioState:
    """Full portfolio across all symbols."""

    initial_budget: float = 1000.0
    current_budget: float = 1000.0
    peak_budget: float = 1000.0

    open_positions: list[Position] = field(default_factory=list)
    closed_trades: list[ClosedTrade] = field(default_factory=list)

    # Reasoning archive
    analysis_cycles: list = field(default_factory=list)  # list[AnalysisCycle]

    @property
    def total_margin_in_use(self) -> float:
        return sum(p.margin for p in self.open_positions)

    @property
    def available_budget(self) -> float:
        return self.current_budget - self.total_margin_in_use

    def total_unrealized_pnl(self, prices: dict[str, float]) -> float:
        return sum(
            p.unrealized_pnl(prices.get(p.symbol, p.entry_price))
            for p in self.open_positions
        )

    @property
    def total_trades(self) -> int:
        return len(self.closed_trades)

    @property
    def winning_trades(self) -> int:
        return sum(1 for t in self.closed_trades if t.is_win)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    def win_rate_last_n(self, n: int) -> float:
        recent = self.closed_trades[-n:]
        if not recent:
            return 0.0
        return sum(1 for t in recent if t.is_win) / len(recent)

    @property
    def losing_streak(self) -> int:
        """Current consecutive losing streak (from most recent)."""
        streak = 0
        for trade in reversed(self.closed_trades):
            if not trade.is_win:
                streak += 1
            else:
                break
        return streak

    @property
    def drawdown_from_peak(self) -> float:
        """Current drawdown as a fraction (0.0 = at peak, 0.2 = 20% down)."""
        if self.peak_budget == 0:
            return 0.0
        return (self.peak_budget - self.current_budget) / self.peak_budget

    def update_peak(self) -> None:
        if self.current_budget > self.peak_budget:
            self.peak_budget = self.current_budget

    def close_position(
        self, position: Position, exit_price: float, reason: str
    ) -> ClosedTrade:
        """Close a position and record the trade."""
        pnl = position.unrealized_pnl(exit_price)
        pnl_pct = position.unrealized_pnl_pct(exit_price)

        trade = ClosedTrade(
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            leverage=position.leverage,
            margin=position.margin,
            pnl=pnl,
            pnl_pct=pnl_pct,
            opened_at=position.opened_at,
            close_reason=reason,
        )

        # current_budget tracks equity — only adjust by realized PnL
        self.current_budget += pnl
        self.closed_trades.append(trade)
        self.open_positions.remove(position)
        self.update_peak()
        return trade

    def open_position(self, position: Position) -> None:
        # current_budget tracks equity — margin is tracked via open_positions
        self.open_positions.append(position)

    def get_positions_for_symbol(self, symbol: str) -> list[Position]:
        return [p for p in self.open_positions if p.symbol == symbol]

    def to_summary_dict(self, prices: dict[str, float] | None = None) -> dict:
        prices = prices or {}
        return {
            "initial_budget": self.initial_budget,
            "current_budget": round(self.current_budget, 2),
            "available_budget": round(self.available_budget, 2),
            "margin_in_use": round(self.total_margin_in_use, 2),
            "unrealized_pnl": round(self.total_unrealized_pnl(prices), 2),
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate, 3),
            "losing_streak": self.losing_streak,
            "drawdown_from_peak": round(self.drawdown_from_peak, 3),
            "open_positions": [
                {
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "entry": p.entry_price,
                    "qty": p.quantity,
                    "leverage": p.leverage,
                    "sl": p.stop_loss,
                    "tp": p.take_profit,
                    "unrealized_pnl": round(
                        p.unrealized_pnl(prices.get(p.symbol, p.entry_price)), 2
                    ),
                }
                for p in self.open_positions
            ],
            "recent_trades": [
                {
                    "symbol": t.symbol,
                    "side": t.side.value,
                    "pnl": round(t.pnl, 2),
                    "reason": t.close_reason,
                }
                for t in self.closed_trades[-5:]
            ],
        }
