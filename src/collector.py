"""WebSocket data collector using orderly-evm-connector SDK.

One collector per symbol. Thread-safe state updates via the SDK's threaded
WebSocket client, consumed by the async main loop.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import httpx
from orderly_evm_connector.websocket.websocket_api import WebsocketPublicAPIClient

from .models.market import (
    BBO,
    FundingRate,
    KlineBuffer,
    MarketSnapshot,
    OpenInterest,
    OrderbookLevel,
    OrderbookSnapshot,
    RecentTrade,
    TickerData,
    Timeframe,
    TradersOI,
    VolumeDelta,
)

logger = logging.getLogger(__name__)

# Max recent trades to keep for volume delta computation
MAX_RECENT_TRADES = 500


class DataCollector:
    """Collects real-time market data for a single symbol via WebSocket.

    The SDK's WebSocket runs in a background thread. Data is stored in
    thread-safe structures and read by the async main loop.
    """

    def __init__(
        self,
        symbol: str,
        ws_account_id: str,
        testnet: bool = False,
        rest_base_url: str = "https://api-evm.orderly.org",
    ) -> None:
        self.symbol = symbol
        self.ws_account_id = ws_account_id
        self.testnet = testnet
        self.rest_base_url = rest_base_url

        # Derive the base asset for index price (PERP_ETH_USDC → SPOT_ETH_USDC)
        parts = symbol.split("_")
        self.spot_symbol = f"SPOT_{parts[1]}_{parts[2]}" if len(parts) == 3 else symbol

        # Thread-safe data stores
        self._lock = threading.Lock()
        self._klines: dict[Timeframe, KlineBuffer] = {
            Timeframe.M5: KlineBuffer(max_size=200),
            Timeframe.M15: KlineBuffer(max_size=200),
            Timeframe.H1: KlineBuffer(max_size=200),
        }
        self._orderbook = OrderbookSnapshot()
        self._bbo = BBO()
        self._funding = FundingRate(symbol=symbol)
        self._open_interest = OpenInterest(symbol=symbol)
        self._traders_oi = TradersOI(symbol=symbol)
        self._ticker = TickerData(symbol=symbol)
        self._recent_trades: list[RecentTrade] = []
        self._mark_price: float = 0.0
        self._index_price: float = 0.0

        self._ws_client: WebsocketPublicAPIClient | None = None
        self._started = False

    def start(self) -> None:
        """Start WebSocket connection and subscribe to all topics."""
        if self._started:
            return

        # Account ID: SDK defaults to a public placeholder if empty/None.
        # Public market data streams don't require a real account.
        kwargs = {
            "orderly_testnet": self.testnet,
            "wss_id": f"trader-{self.symbol}",
            "on_message": self._on_message,
            "on_close": self._on_close,
            "on_error": self._on_error,
            "on_open": self._on_open,
        }
        if self.ws_account_id:
            kwargs["orderly_account_id"] = self.ws_account_id

        self._ws_client = WebsocketPublicAPIClient(**kwargs)

        s = self.symbol
        # Kline subscriptions
        self._ws_client.get_kline(f"{s}@kline_5m")
        self._ws_client.get_kline(f"{s}@kline_15m")
        self._ws_client.get_kline(f"{s}@kline_1h")

        # Market data
        self._ws_client.get_orderbook(f"{s}@orderbook")
        self._ws_client.get_bbo(f"{s}@bbo")
        self._ws_client.get_trade(f"{s}@trade")
        self._ws_client.get_24h_ticker(f"{s}@ticker")

        # Derivatives
        self._ws_client.get_estimated_funding_rate(f"{s}@estfundingrate")
        self._ws_client.get_open_interest(f"{s}@openinterest")

        # Prices
        self._ws_client.get_mark_price(f"{s}@markprice")
        self._ws_client.get_index_price(f"{self.spot_symbol}@indexprice")

        self._started = True
        logger.info("Collector started for %s", self.symbol)

    def stop(self) -> None:
        """Stop WebSocket connection."""
        if self._ws_client:
            self._ws_client.stop()
            self._started = False
            logger.info("Collector stopped for %s", self.symbol)

    def backfill_klines(self) -> None:
        """Fetch historical klines via REST to populate buffers on startup.

        Uses the public TradingView endpoint (no auth required).
        """
        now = int(time.time())
        resolution_map = {
            Timeframe.M5: ("5", 200 * 5 * 60),
            Timeframe.M15: ("15", 200 * 15 * 60),
            Timeframe.H1: ("60", 200 * 60 * 60),
        }

        for tf, (resolution, lookback) in resolution_map.items():
            try:
                from_ts = now - lookback
                url = (
                    f"{self.rest_base_url}/v1/tv/history"
                    f"?symbol={self.symbol}&resolution={resolution}"
                    f"&from={from_ts}&to={now}"
                )
                resp = httpx.get(url, timeout=15)
                resp.raise_for_status()
                data = resp.json()

                # TradingView endpoint returns: t[], o[], h[], l[], c[], v[]
                times = data.get("t", [])
                opens = data.get("o", [])
                highs = data.get("h", [])
                lows = data.get("l", [])
                closes = data.get("c", [])
                volumes = data.get("v", [])

                if times:
                    with self._lock:
                        self._klines[tf].load_bulk(
                            opens=[float(x) for x in opens],
                            highs=[float(x) for x in highs],
                            lows=[float(x) for x in lows],
                            closes=[float(x) for x in closes],
                            volumes=[float(x) for x in volumes],
                            timestamps=[float(x) for x in times],
                        )
                    logger.info(
                        "Backfilled %s %s: %d candles",
                        self.symbol, tf.value, len(times),
                    )
            except Exception:
                logger.exception("Failed to backfill %s %s", self.symbol, tf.value)

    def get_snapshot(self) -> MarketSnapshot:
        """Create a thread-safe snapshot of current market data."""
        with self._lock:
            # Deep-copy kline buffers (numpy arrays are referenced, not copied)
            klines = {}
            for tf, buf in self._klines.items():
                new_buf = KlineBuffer(max_size=buf.max_size)
                new_buf.open = buf.open.copy()
                new_buf.high = buf.high.copy()
                new_buf.low = buf.low.copy()
                new_buf.close = buf.close.copy()
                new_buf.volume = buf.volume.copy()
                new_buf.timestamp = buf.timestamp.copy()
                klines[tf] = new_buf

            # Compute volume delta from recent trades
            buy_vol = sum(t.quantity for t in self._recent_trades if t.side == "BUY")
            sell_vol = sum(t.quantity for t in self._recent_trades if t.side == "SELL")

            return MarketSnapshot(
                symbol=self.symbol,
                klines=klines,
                orderbook=OrderbookSnapshot(
                    bids=list(self._orderbook.bids),
                    asks=list(self._orderbook.asks),
                    timestamp=self._orderbook.timestamp,
                ),
                bbo=BBO(
                    bid_price=self._bbo.bid_price,
                    bid_qty=self._bbo.bid_qty,
                    ask_price=self._bbo.ask_price,
                    ask_qty=self._bbo.ask_qty,
                    timestamp=self._bbo.timestamp,
                ),
                funding=FundingRate(
                    symbol=self._funding.symbol,
                    funding_rate=self._funding.funding_rate,
                    est_funding_rate=self._funding.est_funding_rate,
                    next_funding_time=self._funding.next_funding_time,
                    timestamp=self._funding.timestamp,
                ),
                open_interest=OpenInterest(
                    symbol=self._open_interest.symbol,
                    open_interest=self._open_interest.open_interest,
                    timestamp=self._open_interest.timestamp,
                ),
                traders_oi=TradersOI(
                    symbol=self._traders_oi.symbol,
                    long_ratio=self._traders_oi.long_ratio,
                    short_ratio=self._traders_oi.short_ratio,
                    timestamp=self._traders_oi.timestamp,
                ),
                volume_delta=VolumeDelta(
                    buy_volume=buy_vol,
                    sell_volume=sell_vol,
                    trade_count=len(self._recent_trades),
                ),
                ticker=TickerData(
                    symbol=self._ticker.symbol,
                    open_24h=self._ticker.open_24h,
                    high_24h=self._ticker.high_24h,
                    low_24h=self._ticker.low_24h,
                    close_24h=self._ticker.close_24h,
                    volume_24h=self._ticker.volume_24h,
                    change_24h=self._ticker.change_24h,
                    timestamp=self._ticker.timestamp,
                ),
                mark_price=self._mark_price,
                index_price=self._index_price,
            )

    @property
    def current_price(self) -> float:
        """Best available price (mark > mid > last close)."""
        with self._lock:
            if self._mark_price > 0:
                return self._mark_price
            if self._bbo.mid_price > 0:
                return self._bbo.mid_price
            # Fallback to last 5m close
            buf = self._klines.get(Timeframe.M5)
            if buf and buf.size > 0:
                return float(buf.close[-1])
            return 0.0

    # ------------------------------------------------------------------
    # WebSocket message handlers
    # ------------------------------------------------------------------

    def _on_message(self, _ws, raw_message: str) -> None:
        try:
            msg = json.loads(raw_message)
        except json.JSONDecodeError:
            return

        topic = msg.get("topic", "")
        data = msg.get("data")
        if data is None:
            return

        try:
            if "@kline_5m" in topic:
                self._handle_kline(data, Timeframe.M5)
            elif "@kline_15m" in topic:
                self._handle_kline(data, Timeframe.M15)
            elif "@kline_1h" in topic:
                self._handle_kline(data, Timeframe.H1)
            elif "@orderbook" in topic and "@orderbookupdate" not in topic:
                self._handle_orderbook(data)
            elif "@bbo" in topic:
                self._handle_bbo(data)
            elif "@trade" in topic:
                self._handle_trade(data)
            elif "@ticker" in topic:
                self._handle_ticker(data)
            elif "@estfundingrate" in topic:
                self._handle_funding(data)
            elif "@openinterest" in topic:
                self._handle_oi(data)
            elif "@markprice" in topic:
                self._handle_mark_price(data)
            elif "@indexprice" in topic:
                self._handle_index_price(data)
        except Exception:
            logger.exception("Error handling topic %s", topic)

    def _on_open(self, _manager) -> None:
        logger.info("WebSocket connected for %s", self.symbol)

    def _on_close(self, _manager) -> None:
        logger.warning("WebSocket closed for %s", self.symbol)

    def _on_error(self, _manager, error) -> None:
        logger.error("WebSocket error for %s: %s", self.symbol, error)

    # ------------------------------------------------------------------
    # Data handlers (called from WS thread, must lock)
    # ------------------------------------------------------------------

    def _handle_kline(self, data: dict, tf: Timeframe) -> None:
        with self._lock:
            self._klines[tf].append(
                o=float(data.get("open", 0)),
                h=float(data.get("high", 0)),
                l=float(data.get("low", 0)),
                c=float(data.get("close", 0)),
                v=float(data.get("volume", 0)),
                ts=float(data.get("startTime", 0)),
            )

    def _handle_orderbook(self, data: dict) -> None:
        with self._lock:
            self._orderbook.bids = [
                OrderbookLevel(price=float(b[0]), quantity=float(b[1]))
                for b in data.get("bids", [])[:20]
            ]
            self._orderbook.asks = [
                OrderbookLevel(price=float(a[0]), quantity=float(a[1]))
                for a in data.get("asks", [])[:20]
            ]
            self._orderbook.timestamp = float(data.get("ts", time.time()))

    def _handle_bbo(self, data: dict) -> None:
        with self._lock:
            self._bbo.bid_price = float(data.get("bid", 0))
            self._bbo.bid_qty = float(data.get("bidSize", 0))
            self._bbo.ask_price = float(data.get("ask", 0))
            self._bbo.ask_qty = float(data.get("askSize", 0))
            self._bbo.timestamp = float(data.get("timestamp", time.time()))

    def _handle_trade(self, data: dict) -> None:
        with self._lock:
            self._recent_trades.append(
                RecentTrade(
                    price=float(data.get("price", 0)),
                    quantity=float(data.get("size", 0)),
                    side=data.get("side", "BUY"),
                    timestamp=float(data.get("timestamp", time.time())),
                )
            )
            if len(self._recent_trades) > MAX_RECENT_TRADES:
                self._recent_trades = self._recent_trades[-MAX_RECENT_TRADES:]

    def _handle_ticker(self, data: dict) -> None:
        with self._lock:
            self._ticker.open_24h = float(data.get("open", 0))
            self._ticker.high_24h = float(data.get("high", 0))
            self._ticker.low_24h = float(data.get("low", 0))
            self._ticker.close_24h = float(data.get("close", 0))
            self._ticker.volume_24h = float(data.get("volume", 0))
            if self._ticker.open_24h > 0:
                self._ticker.change_24h = (
                    (self._ticker.close_24h - self._ticker.open_24h)
                    / self._ticker.open_24h
                    * 100
                )
            self._ticker.timestamp = time.time()

    def _handle_funding(self, data: dict) -> None:
        with self._lock:
            self._funding.est_funding_rate = float(data.get("estFundingRate", 0))
            self._funding.funding_rate = float(data.get("lastFundingRate", 0))
            self._funding.next_funding_time = float(data.get("nextFundingTime", 0))
            self._funding.timestamp = time.time()

    def _handle_oi(self, data: dict) -> None:
        with self._lock:
            self._open_interest.open_interest = float(data.get("openInterest", 0))
            self._open_interest.timestamp = time.time()

    def _handle_mark_price(self, data: dict) -> None:
        with self._lock:
            self._mark_price = float(data.get("price", 0))

    def _handle_index_price(self, data: dict) -> None:
        with self._lock:
            self._index_price = float(data.get("price", 0))
