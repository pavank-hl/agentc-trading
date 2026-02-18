"""Configuration models with Pydantic validation."""

from __future__ import annotations

from pydantic import BaseModel, Field


class OpenRouterConfig(BaseModel):
    api_key: str = ""  # Set via OPENROUTER_API_KEY env var
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "x-ai/grok-3-mini"
    reasoning_effort: str = "high"
    max_tokens: int = 4096
    temperature: float = 0.2
    timeout: float = 60.0


class ReserveThresholds(BaseModel):
    """Graduated reserve system thresholds."""

    # Free zone: always available
    free_pct: float = 0.70

    # Guarded reserve
    guarded_pct: float = 0.20
    guarded_win_rate: float = 0.45
    guarded_min_trades: int = 20
    guarded_max_losing_streak: int = 3
    guarded_min_confidence: float = 0.75
    guarded_min_rr: float = 2.0
    guarded_max_leverage: float = 3.0

    # Hard floor
    floor_pct: float = 0.05  # Accessible portion
    floor_win_rate: float = 0.60
    floor_min_trades: int = 30
    floor_min_confidence: float = 0.9
    floor_min_rr: float = 3.0

    # True lockout: never touched
    lockout_pct: float = 0.05


class RiskConfig(BaseModel):
    """Risk management parameters."""

    reserve: ReserveThresholds = Field(default_factory=ReserveThresholds)
    max_loss_per_trade_pct: float = 0.02  # 2% of available budget
    max_total_exposure_pct: float = 0.80  # Max 80% of budget in margin
    min_sl_atr_multiple: float = 0.5  # SL must be >= 0.5 ATR away
    max_sl_atr_multiple: float = 3.0  # SL must be <= 3 ATR away
    drawdown_reduce_pct: float = 0.10  # Reduce size at 10% drawdown
    drawdown_halt_pct: float = 0.20  # Halt at 20% drawdown


class LeverageScale(BaseModel):
    """Confidence-to-leverage mapping."""

    thresholds: list[tuple[float, float, float]] = [
        # (min_confidence, max_confidence, max_leverage)
        (0.0, 0.3, 1.0),
        (0.3, 0.5, 2.0),
        (0.5, 0.7, 5.0),
        (0.7, 0.85, 7.0),
        (0.85, 1.01, 10.0),
    ]

    def max_leverage_for(self, confidence: float) -> float:
        for lo, hi, lev in self.thresholds:
            if lo <= confidence < hi:
                return lev
        return 1.0


class TradingConfig(BaseModel):
    """Top-level configuration."""

    symbols: list[str] = [
        "PERP_ETH_USDC",
        "PERP_BTC_USDC",
        "PERP_SOL_USDC",
    ]
    analysis_interval_seconds: int = 300  # 5 minutes
    initial_budget: float = 1000.0
    paper_trading: bool = True

    openrouter: OpenRouterConfig = Field(default_factory=OpenRouterConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    leverage_scale: LeverageScale = Field(default_factory=LeverageScale)

    # Network
    testnet: bool = False
    rest_base_url: str = "https://api-evm.orderly.org"
    # Account ID is only needed for private WS (orders/fills).
    # Public market data WS works with the SDK's default placeholder.
    orderly_account_id: str = ""

    # Logging
    log_level: str = "INFO"
    store_reasoning: bool = True
