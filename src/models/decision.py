"""Trade decision models: what the LLM outputs and what the risk manager validates."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Action(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    HOLD = "HOLD"
    CLOSE = "CLOSE"


@dataclass
class TradeDecision:
    """Single per-symbol decision from the LLM."""

    symbol: str
    direction: Action
    leverage: float = 1.0
    position_size: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    entry_price: float = 0.0
    confidence: float = 0  # 0-100 scale
    risk_level: str = "MEDIUM"
    summary: str = ""

    @classmethod
    def hold(cls, symbol: str, summary: str = "No action") -> TradeDecision:
        return cls(symbol=symbol, direction=Action.HOLD, summary=summary)

    @classmethod
    def from_dict(cls, d: dict) -> TradeDecision:
        return cls(
            symbol=d.get("symbol", ""),
            direction=Action(d.get("direction", d.get("action", "HOLD")).upper()),
            leverage=float(d.get("leverage", 1)),
            position_size=float(d.get("positionSize", d.get("position_size", d.get("quantity", 0)))),
            stop_loss=float(d.get("stopLoss", d.get("stop_loss", 0))),
            take_profit=float(d.get("takeProfit", d.get("take_profit", 0))),
            entry_price=float(d.get("entryPrice", d.get("entry_price", 0))),
            confidence=float(d.get("confidence", 0)),
            risk_level=d.get("riskLevel", d.get("risk_level", "MEDIUM")),
            summary=d.get("summary", d.get("reasoning", "")),
        )


@dataclass
class MultiSymbolDecision:
    """Array of per-symbol decisions from a single LLM call."""

    decisions: list[TradeDecision] = field(default_factory=list)
    raw_response: str = ""
    reasoning_content: str = ""  # Grok's thinking chain
    model: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class ValidatedDecision:
    """A decision after risk manager processing."""

    original: TradeDecision
    approved: bool = False
    adjusted_leverage: float = 0.0
    adjusted_position_size: float = 0.0
    rejection_reasons: list[str] = field(default_factory=list)
    margin_required: float = 0.0
    max_loss: float = 0.0

    @property
    def final_leverage(self) -> float:
        return self.adjusted_leverage if self.approved else 0.0

    @property
    def final_position_size(self) -> float:
        return self.adjusted_position_size if self.approved else 0.0


@dataclass
class AnalysisCycle:
    """Full audit record for one analysis cycle."""

    timestamp: float = field(default_factory=time.time)
    reasoning_content: str = ""  # Full Grok thinking process
    llm_output: MultiSymbolDecision | None = None
    validated_decisions: list[ValidatedDecision] = field(default_factory=list)
    portfolio_state_before: dict = field(default_factory=dict)
    portfolio_state_after: dict = field(default_factory=dict)
    error: str | None = None
