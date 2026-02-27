"""Sentiment, liquidation tracking, and funding history.

- fetch_fear_greed(): Fear & Greed Index from alternative.me (cached 30 min)
- LiquidationTracker: Binance forced liquidation WebSocket, rolling 15-min window
- FundingHistory: Rolling 72-snapshot history from existing WS feed
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import httpx
import websocket

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fear & Greed Index
# ---------------------------------------------------------------------------

_fear_greed_cache: dict[str, tuple[int, float]] = {}  # {"value": (index, timestamp)}
FEAR_GREED_CACHE_SECONDS = 1800  # 30 minutes


def fetch_fear_greed() -> int:
    """Fetch the Fear & Greed Index (0-100). Cached for 30 minutes.

    Returns 50 on failure (neutral).
    """
    now = time.time()
    cached = _fear_greed_cache.get("value")
    if cached and (now - cached[1]) < FEAR_GREED_CACHE_SECONDS:
        return cached[0]

    try:
        resp = httpx.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        value = int(data["data"][0]["value"])
        _fear_greed_cache["value"] = (value, now)
        logger.info("Fear & Greed Index: %d", value)
        return value
    except Exception:
        logger.warning("Failed to fetch Fear & Greed Index, using default 50", exc_info=True)
        return 50


# ---------------------------------------------------------------------------
# Liquidation Tracker
# ---------------------------------------------------------------------------

# Binance symbols → our Orderly symbols
_BINANCE_SYMBOL_MAP: dict[str, str] = {
    "ETHUSDT": "PERP_ETH_USDC",
    "BTCUSDT": "PERP_BTC_USDC",
    "SOLUSDT": "PERP_SOL_USDC",
}

LIQUIDATION_WINDOW_SECONDS = 900  # 15 minutes


@dataclass
class LiquidationEvent:
    symbol: str  # Our Orderly symbol
    side: str  # "SELL" (long liquidated) or "BUY" (short liquidated)
    quantity: float
    price: float
    timestamp: float


@dataclass
class LiquidationSummary:
    long_liq_volume: float = 0.0  # USD volume of liquidated longs
    short_liq_volume: float = 0.0  # USD volume of liquidated shorts
    bias: str = "balanced"  # "long_squeeze", "short_squeeze", "balanced"


class LiquidationTracker:
    """Tracks Binance forced liquidations via WebSocket.

    Accumulates long/short liquidation volumes per symbol over a rolling
    15-minute window. Runs in a background thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: deque[LiquidationEvent] = deque()
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        """Start the liquidation WebSocket in a background thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_ws, daemon=True, name="liq-tracker")
        self._thread.start()
        logger.info("LiquidationTracker started")

    def stop(self) -> None:
        """Stop the WebSocket connection."""
        self._running = False
        if self._ws:
            self._ws.close()
        logger.info("LiquidationTracker stopped")

    def get_summary(self, symbol: str) -> LiquidationSummary:
        """Get liquidation summary for a symbol over the rolling window."""
        cutoff = time.time() - LIQUIDATION_WINDOW_SECONDS

        with self._lock:
            # Prune old events
            while self._events and self._events[0].timestamp < cutoff:
                self._events.popleft()

            long_vol = 0.0
            short_vol = 0.0
            for ev in self._events:
                if ev.symbol != symbol:
                    continue
                usd_value = ev.quantity * ev.price
                if ev.side == "SELL":
                    # SELL side in forceOrder = long position being liquidated
                    long_vol += usd_value
                else:
                    # BUY side = short position being liquidated
                    short_vol += usd_value

        summary = LiquidationSummary(long_liq_volume=long_vol, short_liq_volume=short_vol)

        total = long_vol + short_vol
        if total > 0:
            if long_vol > short_vol * 2:
                summary.bias = "long_squeeze"
            elif short_vol > long_vol * 2:
                summary.bias = "short_squeeze"
            else:
                summary.bias = "balanced"

        return summary

    def _run_ws(self) -> None:
        """WebSocket event loop (runs in background thread)."""
        url = "wss://fstream.binance.com/ws/!forceOrder@arr"

        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                logger.warning("Liquidation WS disconnected, reconnecting in 5s", exc_info=True)

            if self._running:
                time.sleep(5)

    def _on_message(self, _ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
            order = msg.get("o", {})
            binance_symbol = order.get("s", "")
            our_symbol = _BINANCE_SYMBOL_MAP.get(binance_symbol)
            if not our_symbol:
                return

            event = LiquidationEvent(
                symbol=our_symbol,
                side=order.get("S", ""),
                quantity=float(order.get("q", 0)),
                price=float(order.get("p", 0)),
                timestamp=time.time(),
            )

            with self._lock:
                self._events.append(event)
        except Exception:
            logger.debug("Failed to parse liquidation event", exc_info=True)

    def _on_error(self, _ws, error) -> None:
        logger.debug("Liquidation WS error: %s", error)

    def _on_close(self, _ws, *args) -> None:
        logger.debug("Liquidation WS closed")


# ---------------------------------------------------------------------------
# Funding History
# ---------------------------------------------------------------------------

MAX_FUNDING_SNAPSHOTS = 72  # ~24h at 8h funding intervals, or more if polled frequently


@dataclass
class FundingSnapshot:
    rate: float
    timestamp: float


@dataclass
class FundingStats:
    current: float = 0.0
    avg_24h: float = 0.0
    min_24h: float = 0.0
    max_24h: float = 0.0
    trend: str = "flat"  # "rising", "falling", "flat"


class FundingHistory:
    """Stores rolling funding rate snapshots per symbol.

    Call record() whenever a new funding rate WS update arrives.
    Call get_stats() to get aggregated stats.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._history: dict[str, deque[FundingSnapshot]] = {}

    def record(self, symbol: str, rate: float, timestamp: float) -> None:
        """Record a new funding rate snapshot."""
        with self._lock:
            if symbol not in self._history:
                self._history[symbol] = deque(maxlen=MAX_FUNDING_SNAPSHOTS)
            self._history[symbol].append(FundingSnapshot(rate=rate, timestamp=timestamp))

    def get_stats(self, symbol: str) -> FundingStats:
        """Get funding rate statistics for a symbol."""
        with self._lock:
            snapshots = list(self._history.get(symbol, []))

        if not snapshots:
            return FundingStats()

        rates = [s.rate for s in snapshots]
        current = rates[-1]

        # 24h window
        cutoff = time.time() - 86400
        recent_rates = [s.rate for s in snapshots if s.timestamp >= cutoff]
        if not recent_rates:
            recent_rates = rates  # fallback to all available

        stats = FundingStats(
            current=current,
            avg_24h=sum(recent_rates) / len(recent_rates),
            min_24h=min(recent_rates),
            max_24h=max(recent_rates),
        )

        # Trend: compare last third vs first third of available data
        if len(rates) >= 6:
            third = len(rates) // 3
            early_avg = sum(rates[:third]) / third
            late_avg = sum(rates[-third:]) / third
            diff = late_avg - early_avg
            if abs(diff) < 0.00001:
                stats.trend = "flat"
            elif diff > 0:
                stats.trend = "rising"
            else:
                stats.trend = "falling"

        return stats
