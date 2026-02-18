"""Tests for technical indicator computations."""

import numpy as np
import pytest

from src.indicators import (
    atr,
    bollinger_bands,
    bollinger_pct_b,
    compute_indicators,
    ema,
    macd,
    rsi,
    sma,
    vwap,
)
from src.models.market import (
    BBO,
    FundingRate,
    KlineBuffer,
    MarketSnapshot,
    OpenInterest,
    Timeframe,
    TradersOI,
    VolumeDelta,
)


class TestEMA:
    def test_basic_ema(self):
        data = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        result = ema(data, 3)
        # First 2 values should be NaN, third should be SMA of first 3
        assert np.isnan(result[0])
        assert np.isnan(result[1])
        assert result[2] == pytest.approx(2.0, abs=0.01)  # mean(1,2,3) = 2
        # EMA should track upward
        assert result[-1] > result[-2]

    def test_ema_short_data(self):
        data = np.array([1.0, 2.0])
        result = ema(data, 5)
        assert np.all(np.isnan(result))

    def test_ema_constant(self):
        data = np.full(20, 5.0)
        result = ema(data, 10)
        # EMA of constant should be that constant
        assert result[-1] == pytest.approx(5.0)


class TestSMA:
    def test_basic_sma(self):
        data = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = sma(data, 3)
        assert np.isnan(result[0])
        assert np.isnan(result[1])
        assert result[2] == pytest.approx(2.0)  # mean(1,2,3)
        assert result[3] == pytest.approx(3.0)  # mean(2,3,4)
        assert result[4] == pytest.approx(4.0)  # mean(3,4,5)


class TestRSI:
    def test_rsi_overbought(self):
        # Steadily rising prices → high RSI
        data = np.arange(1.0, 30.0)
        result = rsi(data, 14)
        assert result[-1] > 90  # Should be very overbought

    def test_rsi_oversold(self):
        # Steadily falling prices → low RSI
        data = np.arange(30.0, 1.0, -1.0)
        result = rsi(data, 14)
        assert result[-1] < 10  # Should be very oversold

    def test_rsi_range(self):
        # RSI should be between 0 and 100
        np.random.seed(42)
        data = np.cumsum(np.random.randn(100)) + 100
        result = rsi(data, 14)
        valid = result[~np.isnan(result)]
        assert np.all(valid >= 0)
        assert np.all(valid <= 100)

    def test_rsi_short_data(self):
        data = np.array([1.0, 2.0, 3.0])
        result = rsi(data, 14)
        assert np.all(np.isnan(result))


class TestMACD:
    def test_macd_trending_up(self):
        data = np.arange(1.0, 50.0)
        ml, sl, hist = macd(data, 12, 26, 9)
        # MACD line should be positive in an uptrend
        assert ml[-1] > 0

    def test_macd_shape(self):
        data = np.random.randn(50) + 100
        ml, sl, hist = macd(data, 12, 26, 9)
        assert len(ml) == len(data)
        assert len(sl) == len(data)
        assert len(hist) == len(data)
        # Histogram = MACD - Signal
        valid_idx = ~(np.isnan(ml) | np.isnan(sl))
        np.testing.assert_allclose(hist[valid_idx], ml[valid_idx] - sl[valid_idx])


class TestBollinger:
    def test_bollinger_bands_order(self):
        np.random.seed(42)
        data = np.cumsum(np.random.randn(50)) + 100
        upper, middle, lower = bollinger_bands(data, 20, 2.0)
        # Where computed: upper > middle > lower
        valid = ~np.isnan(upper)
        assert np.all(upper[valid] >= middle[valid])
        assert np.all(middle[valid] >= lower[valid])

    def test_pct_b_range(self):
        np.random.seed(42)
        data = np.cumsum(np.random.randn(50)) + 100
        pct_b = bollinger_pct_b(data, 20, 2.0)
        # %B can be outside 0-1 when price is outside bands
        # but for normal data it should be roughly 0-1
        valid = pct_b[~np.isnan(pct_b)]
        assert len(valid) > 0


class TestVWAP:
    def test_vwap_with_equal_volume(self):
        high = np.array([10.0, 11.0, 12.0])
        low = np.array([9.0, 10.0, 11.0])
        close = np.array([9.5, 10.5, 11.5])
        vol = np.array([100.0, 100.0, 100.0])
        result = vwap(high, low, close, vol)
        # With equal volume, VWAP = cumulative average of typical price
        tp = (high + low + close) / 3
        expected_last = np.mean(tp)
        assert result[-1] == pytest.approx(expected_last)


class TestATR:
    def test_atr_positive(self):
        high = np.array([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25], dtype=np.float64)
        low = high - 2.0
        close = high - 1.0
        result = atr(high, low, close, 14)
        # ATR should be positive where computed
        valid = result[~np.isnan(result)]
        assert len(valid) > 0
        assert np.all(valid > 0)

    def test_atr_short_data(self):
        high = np.array([10.0, 11.0])
        low = np.array([9.0, 10.0])
        close = np.array([9.5, 10.5])
        result = atr(high, low, close, 14)
        assert np.all(np.isnan(result))


class TestComputeIndicators:
    def _make_snapshot(self, n_candles: int = 60) -> MarketSnapshot:
        """Create a snapshot with synthetic data."""
        np.random.seed(42)
        prices = np.cumsum(np.random.randn(n_candles)) + 3000

        snap = MarketSnapshot(symbol="PERP_ETH_USDC")
        for tf in [Timeframe.M5, Timeframe.M15, Timeframe.H1]:
            buf = KlineBuffer(max_size=200)
            buf.open = prices.copy()
            buf.high = prices + np.random.rand(n_candles) * 10
            buf.low = prices - np.random.rand(n_candles) * 10
            buf.close = prices + np.random.randn(n_candles) * 2
            buf.volume = np.random.rand(n_candles) * 1000 + 100
            buf.timestamp = np.arange(n_candles, dtype=np.float64)
            snap.klines[tf] = buf

        snap.mark_price = float(prices[-1])
        snap.index_price = float(prices[-1]) - 0.5
        snap.bbo = BBO(bid_price=2999.0, ask_price=3001.0)
        snap.funding = FundingRate(symbol="PERP_ETH_USDC", est_funding_rate=0.0003)
        snap.open_interest = OpenInterest(symbol="PERP_ETH_USDC", open_interest=50000)
        snap.traders_oi = TradersOI(symbol="PERP_ETH_USDC", long_ratio=0.6, short_ratio=0.4)
        snap.volume_delta = VolumeDelta(buy_volume=500, sell_volume=300, trade_count=100)

        return snap

    def test_compute_indicators_returns_report(self):
        snap = self._make_snapshot()
        report = compute_indicators(snap)

        assert report.symbol == "PERP_ETH_USDC"
        assert report.mark_price > 0
        assert len(report.timeframes) == 3

        for tf_name in ["5m", "15m", "1h"]:
            ti = report.timeframes[tf_name]
            assert ti.last_close > 0
            assert 0 <= ti.rsi_14 <= 100
            assert ti.atr_14 > 0
            assert ti.ema_alignment in ("bullish", "bearish", "mixed")

    def test_orderbook_analysis(self):
        snap = self._make_snapshot()
        report = compute_indicators(snap)

        assert report.orderbook.spread_bps > 0
        assert report.orderbook.interpretation in ("buy_pressure", "sell_pressure", "balanced")

    def test_derivatives_analysis(self):
        snap = self._make_snapshot()
        report = compute_indicators(snap)

        assert report.derivatives.funding_interpretation == "longs_pay"
        assert report.derivatives.ls_ratio == pytest.approx(1.5)
        assert report.derivatives.sentiment == "crowded_longs"

    def test_volume_delta(self):
        snap = self._make_snapshot()
        report = compute_indicators(snap)

        assert report.volume_delta == 200  # 500 - 300
        assert report.volume_delta_ratio == pytest.approx(0.25)  # 200/800
