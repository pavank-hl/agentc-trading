"""Async main loop: 3 collectors → strategy engine → risk manager → execute/log.

Run with: OPENROUTER_API_KEY=xxx python -m src.main
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import yaml

from .adapters.openrouter_adapter import OpenRouterAdapter
from .collector import DataCollector
from .models.config import TradingConfig
from .models.position import PortfolioState
from .strategy import StrategyEngine

logger = logging.getLogger(__name__)


def load_config() -> TradingConfig:
    """Load config from config.yaml, environment variables, and defaults."""
    config_path = Path(__file__).parent.parent / "config.yaml"
    data = {}
    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

    config = TradingConfig(**data)

    # Override API key from environment
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if api_key:
        config.openrouter.api_key = api_key

    return config


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def main() -> None:
    config = load_config()
    setup_logging(config.log_level)

    if not config.openrouter.api_key:
        logger.error("OPENROUTER_API_KEY not set. Export it or add to config.yaml.")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Orderly Network LLM Trading System")
    logger.info("Symbols: %s", ", ".join(config.symbols))
    logger.info("Model: %s", config.openrouter.model)
    logger.info("Budget: $%.2f (paper=%s)", config.initial_budget, config.paper_trading)
    logger.info("Analysis interval: %ds", config.analysis_interval_seconds)
    logger.info("=" * 60)

    # Account ID: only required for private WS. Public market data
    # works with SDK's default placeholder (empty string → SDK default).
    account_id = config.orderly_account_id

    # Initialize portfolio
    portfolio = PortfolioState(
        initial_budget=config.initial_budget,
        current_budget=config.initial_budget,
        peak_budget=config.initial_budget,
    )

    # Initialize LLM adapter
    llm = OpenRouterAdapter(config.openrouter)

    # Initialize strategy engine
    engine = StrategyEngine(config, llm, portfolio)

    # Initialize collectors (one per symbol)
    collectors: dict[str, DataCollector] = {}
    for symbol in config.symbols:
        collector = DataCollector(
            symbol=symbol,
            ws_account_id=account_id,
            testnet=config.testnet,
            rest_base_url=config.rest_base_url,
        )
        collectors[symbol] = collector

    # Backfill historical klines (blocking, runs before WS connects)
    logger.info("Backfilling historical klines...")
    for symbol, collector in collectors.items():
        collector.backfill_klines()

    # Start WebSocket connections
    logger.info("Starting WebSocket connections...")
    for collector in collectors.values():
        collector.start()

    # Wait for initial data to arrive
    logger.info("Waiting 10s for WebSocket data to stabilize...")
    await asyncio.sleep(10)

    # Shutdown event
    shutdown = asyncio.Event()

    def handle_signal(*_):
        logger.info("Shutdown signal received")
        shutdown.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    logger.info("Entering main trading loop...")
    cycle_count = 0

    try:
        while not shutdown.is_set():
            cycle_start = time.time()
            cycle_count += 1

            try:
                # 1. Get current prices
                prices: dict[str, float] = {}
                for symbol, collector in collectors.items():
                    prices[symbol] = collector.current_price

                # Log prices
                price_str = " | ".join(
                    f"{s}: ${p:.2f}" for s, p in prices.items() if p > 0
                )
                logger.info("Cycle %d — Prices: %s", cycle_count, price_str)

                # 2. Check SL/TP on all open positions
                close_messages = engine.check_stop_loss_take_profit(prices)
                for msg in close_messages:
                    logger.info("SL/TP: %s", msg)

                # 3. Get snapshots from all collectors
                snapshots = {
                    symbol: collector.get_snapshot()
                    for symbol, collector in collectors.items()
                }

                # 4. Run analysis cycle (indicators → LLM → validate → execute)
                validated = await engine.run_cycle(snapshots, prices)

                # 5. Log summary
                approved = sum(1 for v in validated if v.approved and v.original.action.value not in ("HOLD",))
                rejected = sum(1 for v in validated if not v.approved and v.original.action.value not in ("HOLD",))
                logger.info(
                    "Cycle %d complete — Approved: %d, Rejected: %d, "
                    "Budget: $%.2f, Open positions: %d",
                    cycle_count, approved, rejected,
                    portfolio.current_budget, len(portfolio.open_positions),
                )

                # 6. Save reasoning to file
                if config.store_reasoning and engine.cycles:
                    _save_cycle_log(engine.cycles[-1], cycle_count)

            except Exception:
                logger.exception("Error in cycle %d", cycle_count)

            # Wait for next cycle
            elapsed = time.time() - cycle_start
            sleep_time = max(0, config.analysis_interval_seconds - elapsed)
            logger.info("Next cycle in %.0fs...", sleep_time)

            try:
                await asyncio.wait_for(shutdown.wait(), timeout=sleep_time)
            except asyncio.TimeoutError:
                pass

    finally:
        logger.info("Shutting down...")
        for collector in collectors.values():
            collector.stop()
        await llm.close()

        # Final summary
        logger.info("=" * 60)
        logger.info("Final Portfolio Summary")
        logger.info("Budget: $%.2f (started: $%.2f)", portfolio.current_budget, portfolio.initial_budget)
        logger.info("Total trades: %d (Win rate: %.1f%%)", portfolio.total_trades, portfolio.win_rate * 100)
        logger.info("Peak budget: $%.2f", portfolio.peak_budget)
        logger.info("Analysis cycles: %d", cycle_count)
        logger.info("=" * 60)


def _save_cycle_log(cycle, cycle_num: int) -> None:
    """Append cycle data to a JSONL log file."""
    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"cycles_{time.strftime('%Y%m%d')}.jsonl"

    record = {
        "cycle": cycle_num,
        "timestamp": cycle.timestamp,
        "reasoning_content": cycle.reasoning_content[:5000] if cycle.reasoning_content else "",
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


if __name__ == "__main__":
    asyncio.run(main())
