"""TAAPI.io bulk indicator client.

Fetches StochRSI, ADX, CCI, OBV, and volume split (taker flow) for multiple
symbols across 3 timeframes (5m, 15m, 1h) using a single bulk POST per symbol.

Usage:
    client = TaapiClient(secret="YOUR_KEY")
    results = await client.fetch_indicators(["PERP_ETH_USDC", "PERP_BTC_USDC"])
    # results["PERP_ETH_USDC"]["5m"] → TaapiResult(stoch_rsi_k=..., adx=..., ...)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

BULK_URL = "https://api.taapi.io/bulk"

# Map our Orderly symbols to TAAPI format (Binance Futures pairs)
SYMBOL_MAP: dict[str, str] = {
    "PERP_ETH_USDC": "ETH/USDT",
    "PERP_BTC_USDC": "BTC/USDT",
    "PERP_SOL_USDC": "SOL/USDT",
}

TIMEFRAMES = ["5m", "15m", "1h"]

# 5 indicators per timeframe
INDICATORS = ["stochrsi", "adx", "cci", "obv", "volumesplit"]


@dataclass
class TaapiResult:
    """Parsed TAAPI indicators for one symbol + one timeframe."""

    stoch_rsi_k: float = 0.0
    stoch_rsi_d: float = 0.0
    adx: float = 0.0
    cci: float = 0.0
    obv: float = 0.0
    taker_buy_pct: float = 50.0
    taker_sell_pct: float = 50.0


class TaapiClient:
    """Fetches bulk indicators from TAAPI.io."""

    def __init__(self, secret: str, exchange: str = "binancefutures") -> None:
        self.secret = secret
        self.exchange = exchange

    async def fetch_indicators(
        self, symbols: list[str]
    ) -> dict[str, dict[str, TaapiResult]]:
        """Fetch indicators for all symbols.

        Returns: {orderly_symbol: {timeframe: TaapiResult}}
        One bulk POST per symbol (15 constructs each: 3 TFs × 5 indicators).
        On any failure, logs warning and returns empty dict — system continues.
        """
        results: dict[str, dict[str, TaapiResult]] = {}

        for symbol in symbols:
            taapi_symbol = SYMBOL_MAP.get(symbol)
            if not taapi_symbol:
                continue

            try:
                tf_results = await self._fetch_symbol(taapi_symbol)
                results[symbol] = tf_results
            except Exception:
                logger.warning("TAAPI fetch failed for %s, skipping", symbol, exc_info=True)

        return results

    async def _fetch_symbol(self, taapi_symbol: str) -> dict[str, TaapiResult]:
        """Single bulk POST for one symbol across all timeframes."""
        constructs = []
        for tf in TIMEFRAMES:
            for ind in INDICATORS:
                construct: dict = {
                    "exchange": self.exchange,
                    "symbol": taapi_symbol,
                    "interval": tf,
                    "indicator": ind,
                }
                # StochRSI returns fastK, fastD by default
                constructs.append(construct)

        payload = {
            "secret": self.secret,
            "construct": {
                "exchange": self.exchange,
                "symbol": taapi_symbol,
                "interval": "1h",  # default, overridden per construct
                "indicators": constructs,
            },
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(BULK_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()

        return self._parse_response(data)

    def _parse_response(self, data: dict) -> dict[str, TaapiResult]:
        """Parse bulk response into per-timeframe TaapiResults."""
        tf_results: dict[str, TaapiResult] = {tf: TaapiResult() for tf in TIMEFRAMES}

        # The response has a "data" list matching the construct order
        indicators_data = data.get("data", [])

        idx = 0
        for tf in TIMEFRAMES:
            result = tf_results[tf]
            for ind in INDICATORS:
                if idx >= len(indicators_data):
                    break
                entry = indicators_data[idx]
                val = entry.get("result", {}) if isinstance(entry.get("result"), dict) else {}

                if ind == "stochrsi":
                    result.stoch_rsi_k = _safe_float(val.get("valueFastK", entry.get("valueFastK", 0)))
                    result.stoch_rsi_d = _safe_float(val.get("valueFastD", entry.get("valueFastD", 0)))
                elif ind == "adx":
                    result.adx = _safe_float(val.get("value", entry.get("value", 0)))
                elif ind == "cci":
                    result.cci = _safe_float(val.get("value", entry.get("value", 0)))
                elif ind == "obv":
                    result.obv = _safe_float(val.get("value", entry.get("value", 0)))
                elif ind == "volumesplit":
                    result.taker_buy_pct = _safe_float(val.get("buyPercentage", entry.get("buyPercentage", 50)))
                    result.taker_sell_pct = _safe_float(val.get("sellPercentage", entry.get("sellPercentage", 50)))

                idx += 1

        return tf_results


def _safe_float(v) -> float:
    """Convert to float, default 0.0 on failure."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
