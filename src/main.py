"""TradingSystem: callable Python API for LLM-orchestrated trading.

Usage (from an LLM that can execute Python):

    from src.main import TradingSystem

    system = TradingSystem()
    await system.start()

    # Each analysis cycle:
    prompt = system.get_prompt()
    # Read prompt["system_prompt"] and prompt["user_prompt"], produce JSON
    result = system.submit_decision('{"decisions": [...]}')

    # Anytime:
    system.check_stops()   # check SL/TP on open positions
    system.get_status()    # portfolio summary

    await system.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import yaml

from .collector import DataCollector
from .models.config import TradingConfig
from .models.position import PortfolioState
from .strategy import StrategyEngine

logger = logging.getLogger(__name__)


def load_config() -> TradingConfig:
    """Load config from config.yaml, with env var overrides."""
    config_path = Path(__file__).parent.parent / "config.yaml"
    data = {}
    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

    # Environment variable overrides
    budget = os.environ.get("INITIAL_BUDGET")
    if budget:
        data["initial_budget"] = float(budget)

    return TradingConfig(**data)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


class TradingSystem:
    """Callable trading system for LLM orchestration.

    The LLM controls the cadence — call get_prompt() whenever you want
    a new analysis, then submit_decision() with your JSON response.
    """

    def __init__(self) -> None:
        self.config: TradingConfig | None = None
        self.portfolio: PortfolioState | None = None
        self.engine: StrategyEngine | None = None
        self.collectors: dict[str, DataCollector] = {}
        self._started = False
        self._cycle_count = 0

    async def start(self, stabilization_seconds: int = 10) -> str:
        """Load config, start WebSocket collectors, wait for data.

        Returns a status message describing what was started.
        """
        self.config = load_config()
        setup_logging(self.config.log_level)

        self.portfolio = PortfolioState(
            initial_budget=self.config.initial_budget,
            current_budget=self.config.initial_budget,
            peak_budget=self.config.initial_budget,
        )

        self.engine = StrategyEngine(self.config, self.portfolio)

        account_id = self.config.orderly_account_id

        # Initialize collectors (one per symbol)
        for symbol in self.config.symbols:
            self.collectors[symbol] = DataCollector(
                symbol=symbol,
                ws_account_id=account_id,
                testnet=self.config.testnet,
                rest_base_url=self.config.rest_base_url,
            )

        # Backfill historical klines
        logger.info("Backfilling historical klines...")
        for collector in self.collectors.values():
            collector.backfill_klines()

        # Start WebSocket connections
        logger.info("Starting WebSocket connections...")
        for collector in self.collectors.values():
            collector.start()

        # Wait for data to stabilize
        logger.info("Waiting %ds for WebSocket data...", stabilization_seconds)
        await asyncio.sleep(stabilization_seconds)

        self._started = True

        msg = (
            f"Trading system started.\n"
            f"Symbols: {', '.join(self.config.symbols)}\n"
            f"Budget: ${self.config.initial_budget:.2f} "
            f"(paper={self.config.paper_trading})\n"
            f"Collectors active: {len(self.collectors)}"
        )
        logger.info(msg)
        return msg

    async def stop(self) -> str:
        """Shut down collectors and return final summary."""
        for collector in self.collectors.values():
            collector.stop()
        self._started = False

        summary = self.get_status()
        logger.info("Trading system stopped.")
        logger.info(
            "Final — Budget: $%.2f, Trades: %d, Win rate: %.1f%%",
            summary.get("current_budget", 0),
            summary.get("total_trades", 0),
            summary.get("win_rate", 0) * 100,
        )
        return summary

    def get_prompt(self) -> dict:
        """Get current market analysis prompt for the LLM.

        Returns dict with:
            - system_prompt: trading instructions and rules
            - user_prompt: current market data, indicators, portfolio state
            - sl_tp_events: list of SL/TP close messages (if any triggered)
        """
        if not self._started:
            raise RuntimeError("System not started. Call start() first.")

        # Get current prices
        prices: dict[str, float] = {}
        for symbol, collector in self.collectors.items():
            prices[symbol] = collector.current_price

        # Check SL/TP before analysis
        close_messages = self.engine.check_stop_loss_take_profit(prices)
        for msg in close_messages:
            logger.info("SL/TP: %s", msg)

        # Get snapshots from all collectors
        snapshots = {
            symbol: collector.get_snapshot()
            for symbol, collector in self.collectors.items()
        }

        # Build prompts (computes indicators internally)
        system_prompt, user_prompt = self.engine.prepare_analysis(snapshots, prices)

        price_str = " | ".join(
            f"{s}: ${p:.2f}" for s, p in prices.items() if p > 0
        )
        logger.info("Prompt generated — Prices: %s", price_str)

        return {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "sl_tp_events": close_messages,
        }

    def submit_decision(self, response_json: str) -> dict:
        """Submit the LLM's JSON decision for validation and execution.

        Args:
            response_json: Raw JSON string with trading decisions.

        Returns:
            Dict with cycle number, approved/rejected counts, per-decision
            details, and current portfolio status.
        """
        if not self._started:
            raise RuntimeError("System not started. Call start() first.")

        self._cycle_count += 1
        validated = self.engine.process_response(response_json)

        approved = sum(
            1 for v in validated
            if v.approved and v.original.action.value not in ("HOLD",)
        )
        rejected = sum(
            1 for v in validated
            if not v.approved and v.original.action.value not in ("HOLD",)
        )

        # Save cycle log
        if self.config.store_reasoning and self.engine.cycles:
            _save_cycle_log(self.engine.cycles[-1], self._cycle_count)

        result = {
            "cycle": self._cycle_count,
            "approved_trades": approved,
            "rejected_trades": rejected,
            "decisions": [
                {
                    "symbol": v.original.symbol,
                    "action": v.original.action.value,
                    "approved": v.approved,
                    "leverage": v.final_leverage,
                    "quantity": v.final_quantity,
                    "rejection_reasons": v.rejection_reasons,
                }
                for v in validated
            ],
            "portfolio": self.get_status(),
        }

        logger.info(
            "Cycle %d — Approved: %d, Rejected: %d",
            self._cycle_count, approved, rejected,
        )

        return result

    def check_stops(self) -> list[str]:
        """Check SL/TP on all open positions.

        Returns list of close event messages (empty if nothing triggered).
        """
        if not self._started:
            raise RuntimeError("System not started. Call start() first.")

        prices = {
            symbol: collector.current_price
            for symbol, collector in self.collectors.items()
        }
        messages = self.engine.check_stop_loss_take_profit(prices)
        for msg in messages:
            logger.info("SL/TP: %s", msg)
        return messages

    def get_status(self) -> dict:
        """Get current portfolio and system status."""
        if not self.portfolio:
            return {"error": "System not initialized"}

        prices = {}
        if self._started:
            prices = {
                symbol: collector.current_price
                for symbol, collector in self.collectors.items()
            }

        summary = self.portfolio.to_summary_dict(prices)
        summary["cycles_completed"] = self._cycle_count
        summary["system_running"] = self._started
        return summary


def _save_cycle_log(cycle, cycle_num: int) -> None:
    """Append cycle data to a JSONL log file."""
    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"cycles_{time.strftime('%Y%m%d')}.jsonl"

    record = {
        "cycle": cycle_num,
        "timestamp": cycle.timestamp,
        "decisions": [
            {
                "symbol": v.original.symbol,
                "action": v.original.action.value,
                "confidence": v.original.confidence,
                "approved": v.approved,
                "adj_leverage": v.adjusted_leverage,
                "adj_quantity": v.adjusted_quantity,
                "rejection_reasons": v.rejection_reasons,
            }
            for v in cycle.validated_decisions
        ],
        "portfolio_before": cycle.portfolio_state_before,
        "portfolio_after": cycle.portfolio_state_after,
        "error": cycle.error,
    }

    with open(log_file, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
