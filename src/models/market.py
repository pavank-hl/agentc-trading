"""Market data models: kline buffers, orderbook, BBO, funding, OI, snapshots."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from numpy.typing import NDArray


class Timeframe(str, Enum):
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"


@dataclass
class KlineBuffer:
    """Fixed-size ring buffer of OHLCV data backed by numpy arrays.

    Columns: open, high, low, close, volume, timestamp.
    New candles are appended; when full the oldest is dropped.
    """

    max_size: int = 200

    open: NDArray[np.float64] = field(default_factory=lambda: np.empty(0, dtype=np.float64))
    high: NDArray[np.float64] = field(default_factory=lambda: np.empty(0, dtype=np.float64))
    low: NDArray[np.float64] = field(default_factory=lambda: np.empty(0, dtype=np.float64))
    close: NDArray[np.float64] = field(default_factory=lambda: np.empty(0, dtype=np.float64))
    volume: NDArray[np.float64] = field(default_factory=lambda: np.empty(0, dtype=np.float64))
    timestamp: NDArray[np.float64] = field(default_factory=lambda: np.empty(0, dtype=np.float64))

    @property
    def size(self) -> int:
        return len(self.close)

    def append(
        self,
        o: float,
        h: float,
        l: float,  # noqa: E741
        c: float,
        v: float,
        ts: float,
    ) -> None:
        """Append a new candle. If buffer is full, drop the oldest."""
        if self.size > 0 and self.timestamp[-1] == ts:
            # Update in-progress candle.
            self.open[-1] = o
            self.high[-1] = h
            self.low[-1] = l
            self.close[-1] = c
            self.volume[-1] = v
            return

        self.open = np.append(self.open, o)
        self.high = np.append(self.high, h)
        self.low = np.append(self.low, l)
        self.close = np.append(self.close, c)
        self.volume = np.append(self.volume, v)
        self.timestamp = np.append(self.timestamp, ts)

        if self.size > self.max_size:
            self.open = self.open[-self.max_size :]
            self.high = self.high[-self.max_size :]
            self.low = self.low[-self.max_size :]
            self.close = self.close[-self.max_size :]
            self.volume = self.volume[-self.max_size :]
            self.timestamp = self.timestamp[-self.max_size :]

    def load_bulk(
        self,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
        timestamps: list[float],
    ) -> None:
        """Load historical candles in bulk (oldest first)."""
        self.open = np.array(opens[-self.max_size :], dtype=np.float64)
        self.high = np.array(highs[-self.max_size :], dtype=np.float64)
        self.low = np.array(lows[-self.max_size :], dtype=np.float64)
        self.close = np.array(closes[-self.max_size :], dtype=np.float64)
        self.volume = np.array(volumes[-self.max_size :], dtype=np.float64)
        self.timestamp = np.array(timestamps[-self.max_size :], dtype=np.float64)


@dataclass
class OrderbookLevel:
    price: float
    quantity: float


@dataclass
class OrderbookSnapshot:
    """Current orderbook state (top N levels each side)."""

    bids: list[OrderbookLevel] = field(default_factory=list)
    asks: list[OrderbookLevel] = field(default_factory=list)
    timestamp: float = 0.0

    @property
    def bid_depth(self) -> float:
        return sum(l.quantity for l in self.bids)

    @property
    def ask_depth(self) -> float:
        return sum(l.quantity for l in self.asks)

    @property
    def imbalance(self) -> float:
        """Positive = bid-heavy (buy pressure), negative = ask-heavy."""
        total = self.bid_depth + self.ask_depth
        if total == 0:
            return 0.0
        return (self.bid_depth - self.ask_depth) / total


@dataclass
class BBO:
    """Best bid/offer."""

    bid_price: float = 0.0
    bid_qty: float = 0.0
    ask_price: float = 0.0
    ask_qty: float = 0.0
    timestamp: float = 0.0

    @property
    def mid_price(self) -> float:
        if self.bid_price == 0 or self.ask_price == 0:
            return 0.0
        return (self.bid_price + self.ask_price) / 2

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price

    @property
    def spread_bps(self) -> float:
        mid = self.mid_price
        if mid == 0:
            return 0.0
        return (self.spread / mid) * 10_000


@dataclass
class FundingRate:
    symbol: str = ""
    funding_rate: float = 0.0
    est_funding_rate: float = 0.0
    next_funding_time: float = 0.0
    timestamp: float = 0.0


@dataclass
class OpenInterest:
    symbol: str = ""
    open_interest: float = 0.0
    timestamp: float = 0.0


@dataclass
class TradersOI:
    """Long/short ratio from traders open interest."""

    symbol: str = ""
    long_ratio: float = 0.5
    short_ratio: float = 0.5
    timestamp: float = 0.0

    @property
    def ls_ratio(self) -> float:
        if self.short_ratio == 0:
            return float("inf")
        return self.long_ratio / self.short_ratio


@dataclass
class RecentTrade:
    price: float
    quantity: float
    side: str  # "BUY" or "SELL"
    timestamp: float


@dataclass
class VolumeDelta:
    """Aggregated buy vs sell volume from recent trades."""

    buy_volume: float = 0.0
    sell_volume: float = 0.0
    trade_count: int = 0

    @property
    def delta(self) -> float:
        return self.buy_volume - self.sell_volume

    @property
    def delta_ratio(self) -> float:
        total = self.buy_volume + self.sell_volume
        if total == 0:
            return 0.0
        return self.delta / total


@dataclass
class TickerData:
    """24h ticker summary."""

    symbol: str = ""
    open_24h: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    close_24h: float = 0.0
    volume_24h: float = 0.0
    change_24h: float = 0.0
    timestamp: float = 0.0


@dataclass
class MarketSnapshot:
    """Complete market state for one symbol at a point in time.

    This is the input to indicator computation and prompt building.
    """

    symbol: str = ""
    snapshot_time: float = field(default_factory=time.time)

    # Kline data per timeframe
    klines: dict[Timeframe, KlineBuffer] = field(default_factory=dict)

    # Orderbook
    orderbook: OrderbookSnapshot = field(default_factory=OrderbookSnapshot)
    bbo: BBO = field(default_factory=BBO)

    # Derivatives data
    funding: FundingRate = field(default_factory=FundingRate)
    open_interest: OpenInterest = field(default_factory=OpenInterest)
    traders_oi: TradersOI = field(default_factory=TradersOI)

    # Volume
    volume_delta: VolumeDelta = field(default_factory=VolumeDelta)
    recent_trades: list[RecentTrade] = field(default_factory=list)

    # Ticker
    ticker: TickerData = field(default_factory=TickerData)

    # Prices
    mark_price: float = 0.0
    index_price: float = 0.0
