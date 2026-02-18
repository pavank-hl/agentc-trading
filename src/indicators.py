"""Pure numpy technical indicator computations.

All functions take numpy arrays and return numpy arrays or scalars.
Entry point: compute_indicators(snapshot) → IndicatorReport.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from .models.market import KlineBuffer, MarketSnapshot, Timeframe


# ---------------------------------------------------------------------------
# Low-level indicator functions (pure numpy)
# ---------------------------------------------------------------------------


def ema(data: NDArray[np.float64], period: int) -> NDArray[np.float64]:
    """Exponential moving average. Handles leading NaN values gracefully."""
    if len(data) < period:
        return np.full_like(data, np.nan)

    # Find the first non-NaN index
    valid_mask = ~np.isnan(data)
    valid_indices = np.where(valid_mask)[0]
    if len(valid_indices) < period:
        return np.full_like(data, np.nan)

    start = int(valid_indices[0])
    alpha = 2.0 / (period + 1)
    result = np.full_like(data, np.nan)
    # Seed with SMA of first `period` valid values
    seed_end = start + period
    if seed_end > len(data):
        return result
    result[seed_end - 1] = np.mean(data[start:seed_end])
    for i in range(seed_end, len(data)):
        if np.isnan(data[i]):
            result[i] = result[i - 1]
        else:
            result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
    return result


def sma(data: NDArray[np.float64], period: int) -> NDArray[np.float64]:
    """Simple moving average."""
    if len(data) < period:
        return np.full_like(data, np.nan)
    result = np.full_like(data, np.nan)
    cumsum = np.cumsum(data)
    result[period - 1 :] = (cumsum[period - 1 :] - np.concatenate(([0], cumsum[:-period]))) / period
    return result


def rsi(close: NDArray[np.float64], period: int = 14) -> NDArray[np.float64]:
    """Relative Strength Index using Wilder's smoothing."""
    if len(close) < period + 1:
        return np.full_like(close, np.nan)

    deltas = np.diff(close)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    result = np.full(len(close), np.nan)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100.0 - (100.0 / (1.0 + rs))

    return result


def macd(
    close: NDArray[np.float64],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """MACD line, signal line, histogram."""
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal_period)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(
    close: NDArray[np.float64], period: int = 20, num_std: float = 2.0
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Upper band, middle (SMA), lower band."""
    middle = sma(close, period)
    if len(close) < period:
        nan_arr = np.full_like(close, np.nan)
        return nan_arr, nan_arr, nan_arr

    std = np.full_like(close, np.nan)
    for i in range(period - 1, len(close)):
        std[i] = np.std(close[i - period + 1 : i + 1], ddof=0)

    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def bollinger_pct_b(
    close: NDArray[np.float64], period: int = 20, num_std: float = 2.0
) -> NDArray[np.float64]:
    """%B: (price - lower) / (upper - lower). 0 = at lower band, 1 = at upper."""
    upper, _, lower = bollinger_bands(close, period, num_std)
    width = upper - lower
    with np.errstate(divide="ignore", invalid="ignore"):
        pct_b = np.where(width != 0, (close - lower) / width, 0.5)
    return pct_b


def vwap(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    volume: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Volume-weighted average price (cumulative from start of buffer)."""
    typical = (high + low + close) / 3.0
    cum_tp_vol = np.cumsum(typical * volume)
    cum_vol = np.cumsum(volume)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(cum_vol != 0, cum_tp_vol / cum_vol, 0.0)
    return result


def atr(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    period: int = 14,
) -> NDArray[np.float64]:
    """Average True Range."""
    if len(close) < 2:
        return np.full_like(close, np.nan)

    tr = np.empty(len(close))
    tr[0] = high[0] - low[0]
    for i in range(1, len(close)):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    result = np.full_like(close, np.nan)
    if len(tr) < period:
        return result

    result[period - 1] = np.mean(tr[:period])
    for i in range(period, len(tr)):
        result[i] = (result[i - 1] * (period - 1) + tr[i]) / period

    return result


# ---------------------------------------------------------------------------
# Indicator report (structured output from a kline buffer)
# ---------------------------------------------------------------------------


@dataclass
class TimeframeIndicators:
    """Computed indicators for one timeframe."""

    timeframe: str = ""
    last_close: float = 0.0

    rsi_14: float = 0.0
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0

    bb_upper: float = 0.0
    bb_middle: float = 0.0
    bb_lower: float = 0.0
    bb_pct_b: float = 0.0

    ema_9: float = 0.0
    ema_21: float = 0.0
    ema_50: float = 0.0
    ema_alignment: str = ""  # "bullish", "bearish", "mixed"

    vwap_value: float = 0.0
    price_vs_vwap: str = ""  # "above", "below", "at"

    atr_14: float = 0.0

    # Recent price action
    recent_change_pct: float = 0.0  # % change over last 3 candles
    consecutive_red: int = 0  # consecutive bearish candles from latest
    consecutive_green: int = 0  # consecutive bullish candles from latest
    candle_trend: str = ""  # "dropping", "rising", "choppy"


@dataclass
class OrderbookAnalysis:
    """Derived orderbook metrics."""

    bid_depth: float = 0.0
    ask_depth: float = 0.0
    imbalance: float = 0.0  # -1 to +1
    spread_bps: float = 0.0
    mid_price: float = 0.0
    interpretation: str = ""  # "buy_pressure", "sell_pressure", "balanced"


@dataclass
class DerivativesAnalysis:
    """Funding + OI analysis."""

    funding_rate: float = 0.0
    funding_interpretation: str = ""  # "longs_pay", "shorts_pay", "neutral"
    open_interest: float = 0.0
    long_ratio: float = 0.5
    short_ratio: float = 0.5
    ls_ratio: float = 1.0
    sentiment: str = ""  # "crowded_longs", "crowded_shorts", "balanced"


@dataclass
class IndicatorReport:
    """Full indicator report for one symbol."""

    symbol: str = ""
    mark_price: float = 0.0
    index_price: float = 0.0

    timeframes: dict[str, TimeframeIndicators] = field(default_factory=dict)
    orderbook: OrderbookAnalysis = field(default_factory=OrderbookAnalysis)
    derivatives: DerivativesAnalysis = field(default_factory=DerivativesAnalysis)
    volume_delta: float = 0.0
    volume_delta_ratio: float = 0.0

    ticker_change_24h: float = 0.0
    ticker_volume_24h: float = 0.0


# ---------------------------------------------------------------------------
# Compute from snapshot
# ---------------------------------------------------------------------------


def _compute_timeframe(buf: KlineBuffer, tf_name: str) -> TimeframeIndicators:
    """Compute all indicators for a single timeframe buffer."""
    ti = TimeframeIndicators(timeframe=tf_name)

    if buf.size < 2:
        return ti

    c = buf.close
    ti.last_close = float(c[-1])

    # RSI
    rsi_arr = rsi(c, 14)
    ti.rsi_14 = float(rsi_arr[-1]) if not np.isnan(rsi_arr[-1]) else 50.0

    # MACD
    ml, sl, hist = macd(c, 12, 26, 9)
    ti.macd_line = float(ml[-1]) if not np.isnan(ml[-1]) else 0.0
    ti.macd_signal = float(sl[-1]) if not np.isnan(sl[-1]) else 0.0
    ti.macd_histogram = float(hist[-1]) if not np.isnan(hist[-1]) else 0.0

    # Bollinger Bands
    bb_u, bb_m, bb_l = bollinger_bands(c, 20, 2.0)
    ti.bb_upper = float(bb_u[-1]) if not np.isnan(bb_u[-1]) else 0.0
    ti.bb_middle = float(bb_m[-1]) if not np.isnan(bb_m[-1]) else 0.0
    ti.bb_lower = float(bb_l[-1]) if not np.isnan(bb_l[-1]) else 0.0
    pct_b = bollinger_pct_b(c, 20, 2.0)
    ti.bb_pct_b = float(pct_b[-1]) if not np.isnan(pct_b[-1]) else 0.5

    # EMAs
    e9 = ema(c, 9)
    e21 = ema(c, 21)
    e50 = ema(c, 50)
    ti.ema_9 = float(e9[-1]) if not np.isnan(e9[-1]) else 0.0
    ti.ema_21 = float(e21[-1]) if not np.isnan(e21[-1]) else 0.0
    ti.ema_50 = float(e50[-1]) if not np.isnan(e50[-1]) else 0.0

    if ti.ema_9 > ti.ema_21 > ti.ema_50 and ti.ema_50 > 0:
        ti.ema_alignment = "bullish"
    elif ti.ema_50 > ti.ema_21 > ti.ema_9 and ti.ema_9 > 0:
        ti.ema_alignment = "bearish"
    else:
        ti.ema_alignment = "mixed"

    # VWAP
    v = vwap(buf.high, buf.low, c, buf.volume)
    ti.vwap_value = float(v[-1]) if not np.isnan(v[-1]) else 0.0
    if ti.vwap_value > 0:
        if ti.last_close > ti.vwap_value * 1.001:
            ti.price_vs_vwap = "above"
        elif ti.last_close < ti.vwap_value * 0.999:
            ti.price_vs_vwap = "below"
        else:
            ti.price_vs_vwap = "at"

    # ATR
    a = atr(buf.high, buf.low, c, 14)
    ti.atr_14 = float(a[-1]) if not np.isnan(a[-1]) else 0.0

    # Recent price action (last 3 candles)
    if buf.size >= 4:
        recent_close = c[-3:]
        ref_close = c[-4]  # close 3 candles ago
        if ref_close > 0:
            ti.recent_change_pct = (recent_close[-1] - ref_close) / ref_close * 100

        # Consecutive red/green candles from latest
        red = 0
        green = 0
        for i in range(buf.size - 1, 0, -1):
            if c[i] < c[i - 1]:
                if green > 0:
                    break
                red += 1
            elif c[i] > c[i - 1]:
                if red > 0:
                    break
                green += 1
            else:
                break
        ti.consecutive_red = red
        ti.consecutive_green = green

        if red >= 3:
            ti.candle_trend = "dropping"
        elif green >= 3:
            ti.candle_trend = "rising"
        else:
            ti.candle_trend = "choppy"

    return ti


def _analyze_orderbook(snapshot: MarketSnapshot) -> OrderbookAnalysis:
    ob = snapshot.orderbook
    bbo = snapshot.bbo
    analysis = OrderbookAnalysis(
        bid_depth=ob.bid_depth,
        ask_depth=ob.ask_depth,
        imbalance=ob.imbalance,
        spread_bps=bbo.spread_bps,
        mid_price=bbo.mid_price,
    )
    if analysis.imbalance > 0.2:
        analysis.interpretation = "buy_pressure"
    elif analysis.imbalance < -0.2:
        analysis.interpretation = "sell_pressure"
    else:
        analysis.interpretation = "balanced"
    return analysis


def _analyze_derivatives(snapshot: MarketSnapshot) -> DerivativesAnalysis:
    fr = snapshot.funding
    oi = snapshot.open_interest
    toi = snapshot.traders_oi

    analysis = DerivativesAnalysis(
        funding_rate=fr.est_funding_rate,
        open_interest=oi.open_interest,
        long_ratio=toi.long_ratio,
        short_ratio=toi.short_ratio,
        ls_ratio=toi.ls_ratio,
    )

    if fr.est_funding_rate > 0.0001:
        analysis.funding_interpretation = "longs_pay"
    elif fr.est_funding_rate < -0.0001:
        analysis.funding_interpretation = "shorts_pay"
    else:
        analysis.funding_interpretation = "neutral"

    if toi.ls_ratio >= 1.49:
        analysis.sentiment = "crowded_longs"
    elif toi.ls_ratio <= 0.67:
        analysis.sentiment = "crowded_shorts"
    else:
        analysis.sentiment = "balanced"

    return analysis


def compute_indicators(snapshot: MarketSnapshot) -> IndicatorReport:
    """Compute all indicators for a single symbol's snapshot.

    This is the main entry point called by the strategy engine.
    """
    report = IndicatorReport(
        symbol=snapshot.symbol,
        mark_price=snapshot.mark_price,
        index_price=snapshot.index_price,
    )

    # Timeframe indicators
    for tf, buf in snapshot.klines.items():
        report.timeframes[tf.value] = _compute_timeframe(buf, tf.value)

    # Orderbook
    report.orderbook = _analyze_orderbook(snapshot)

    # Derivatives
    report.derivatives = _analyze_derivatives(snapshot)

    # Volume delta
    vd = snapshot.volume_delta
    report.volume_delta = vd.delta
    report.volume_delta_ratio = vd.delta_ratio

    # Ticker
    report.ticker_change_24h = snapshot.ticker.change_24h
    report.ticker_volume_24h = snapshot.ticker.volume_24h

    return report
