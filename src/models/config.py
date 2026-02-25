"""Configuration models with Pydantic validation."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RiskConfig(BaseModel):
    """Risk management parameters."""

    max_loss_per_trade_pct: float = 0.02  # 2% of available balance
    max_total_exposure_pct: float = 0.80  # Max 80% of balance in margin
    min_sl_atr_multiple: float = 0.5  # SL must be >= 0.5 ATR away
    max_sl_atr_multiple: float = 3.0  # SL must be <= 3 ATR away


class TradingConfig(BaseModel):
    """Top-level configuration."""

    symbols: list[str] = [
        "PERP_ETH_USDC",
        "PERP_BTC_USDC",
        "PERP_SOL_USDC",
    ]
    leverage_pct: int = 100  # % of market's max leverage to use (10-200)
    paper_trading: bool = True

    risk: RiskConfig = Field(default_factory=RiskConfig)

    # Network
    testnet: bool = False
    rest_base_url: str = "https://api-evm.orderly.org"
    orderly_account_id: str = ""

    # Logging
    log_level: str = "INFO"
    store_reasoning: bool = True
