"""TradingSystem: callable Python API for LLM-orchestrated trading.

Usage (from an LLM that can execute Python):

    from src.main import TradingSystem

    system = TradingSystem()
    await system.start()

    # Each analysis cycle:
    prompt = system.get_prompt()        # Market data + indicators
    # Check positions/balance via VoltPerps API (GET /v1/account/positions, etc.)
    # Analyze prompt["system_prompt"] + prompt["user_prompt"], produce JSON
    result = system.submit_decision('{"decisions": [...]}')
    # Execute approved trades via x402 VoltPerps API (POST /v1/intent)

    await system.stop()
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from pathlib import Path

import yaml

from .collector import DataCollector
from .monitoring import DecisionMonitoringClient
from .models.config import TradingConfig
from .models.position import PortfolioState
from .sentiment import FundingHistory, LiquidationTracker
from .strategy import StrategyEngine
from .taapi import TaapiClient

logger = logging.getLogger(__name__)


def load_config() -> TradingConfig:
    """Load config from config.yaml, with env var overrides."""
    config_path = Path(__file__).parent.parent / "config.yaml"
    data = {}
    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

    # Environment variable overrides
    leverage_pct = os.environ.get("LEVERAGE_PCT")
    if leverage_pct:
        data["leverage_pct"] = int(leverage_pct)

    taapi_secret = os.environ.get("TAAPI_SECRET")
    if taapi_secret:
        data["taapi_secret"] = taapi_secret

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
        self.taapi_client: TaapiClient = None
        self.liquidation_tracker: LiquidationTracker | None = None
        self.funding_history: FundingHistory | None = None
        self._started = False
        self._cycle_count = 0
        self._last_prompt: str = ""
        self._last_indicators: dict = {}
        self._last_symbols: list[str] = []
        self._last_prices: dict[str, float] = {}
        self._pending_analysis_event: dict | None = None
        self._pending_position_event: dict | None = None
        self.monitoring = DecisionMonitoringClient()
        self._active_prompt_version_id: str | None = None

    async def start(self, stabilization_seconds: int = 10) -> str:
        """Load config, start WebSocket collectors, wait for data.

        Returns a status message describing what was started.
        """
        self.config = load_config()
        setup_logging(self.config.log_level)

        self.portfolio = PortfolioState()

        # Init TAAPI client (required)
        if not self.config.taapi_secret:
            raise RuntimeError(
                "TAAPI_SECRET environment variable is required. "
                "Get a key at https://taapi.io and set TAAPI_SECRET."
            )
        self.taapi_client = TaapiClient(
            secret=self.config.taapi_secret,
            exchange=self.config.taapi_exchange,
        )
        logger.info("TAAPI client enabled (exchange=%s)", self.config.taapi_exchange)

        # Init liquidation tracker
        self.liquidation_tracker = LiquidationTracker()
        self.liquidation_tracker.start()

        # Init funding history
        self.funding_history = FundingHistory()

        self.engine = StrategyEngine(
            self.config,
            self.portfolio,
            taapi_client=self.taapi_client,
            liquidation_tracker=self.liquidation_tracker,
            funding_history=self.funding_history,
        )
        self._refresh_active_prompt_version()

        account_id = self.config.orderly_account_id

        # Initialize collectors (one per symbol)
        for symbol in self.config.symbols:
            self.collectors[symbol] = DataCollector(
                symbol=symbol,
                ws_account_id=account_id,
                rest_base_url=self.config.rest_base_url,
                on_funding_update=self.funding_history.record,
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
            f"Leverage PCT: {self.config.leverage_pct}%\n"
            f"Collectors active: {len(self.collectors)}\n"
            f"TAAPI indicators: enabled\n"
            f"Liquidation tracker: running\n"
            f"Funding history: recording"
        )
        logger.info(msg)
        return msg

    async def stop(self) -> str:
        """Shut down collectors and return final summary."""
        for collector in self.collectors.values():
            collector.stop()
        if self.liquidation_tracker:
            self.liquidation_tracker.stop()
        self._started = False

        summary = self.get_status()
        logger.info("Trading system stopped.")
        logger.info(
            "Final — Trades: %d, Win rate: %.1f%%",
            summary.get("total_trades", 0),
            summary.get("win_rate", 0) * 100,
        )
        return summary

    def get_prompt(self) -> dict:
        """Get current market analysis prompt for the LLM.

        Returns dict with:
            - system_prompt: trading instructions and rules
            - user_prompt: current market data and indicators
        """
        if not self._started:
            raise RuntimeError("System not started. Call start() first.")

        # Get current prices
        self._refresh_active_prompt_version()
        prices: dict[str, float] = {}
        for symbol, collector in self.collectors.items():
            prices[symbol] = collector.current_price

        # Get snapshots from all collectors
        snapshots = {
            symbol: collector.get_snapshot()
            for symbol, collector in self.collectors.items()
        }

        # Build prompts (computes indicators internally)
        system_prompt, user_prompt = self.engine.prepare_analysis(snapshots, prices)

        self._last_symbols = list(self.engine._pending_prices.keys())
        self._last_indicators = {
            sym: json.loads(json.dumps(dataclasses.asdict(report), default=float))
            for sym, report in self.engine._pending_reports.items()
        }
        self._last_prompt = user_prompt
        self._last_prices = prices
        self._pending_analysis_event = {
            "step_type": "analysis",
            "event_timestamp": time.time(),
            "system_prompt": system_prompt,
            "strategy_prompt": self.engine.analysis_prompt,
            "user_prompt": user_prompt,
            "rendered_prompt": self._rendered_prompt(system_prompt, user_prompt),
            "daemon_data": {
                "symbols": list(self._last_symbols),
                "prices": dict(prices),
                "indicators": self._last_indicators,
            },
        }
        self._pending_position_event = None

        price_str = " | ".join(
            f"{s}: ${p:.2f}" for s, p in prices.items() if p > 0
        )
        logger.info("Prompt generated — Prices: %s", price_str)

        self._cycle_count += 1

        return {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        }

    def get_position_prompt(self, analysis_json: str, positions_json: str) -> dict | None:
        """Build position management prompt for LLM call #2.

        Args:
            analysis_json: Raw JSON from LLM call #1 (analysis result).
            positions_json: Raw JSON from the positions API.
                Pass the API response directly — the system unwraps it.

        Returns:
            {"system_prompt": str, "user_prompt": str} if positions exist.
            None if no positions (use analysis directly).
        """
        if not self._started:
            raise RuntimeError("System not started. Call start() first.")

        result = self.engine.get_position_prompt(analysis_json, positions_json)
        if result is None:
            return None

        system_prompt, user_prompt = result
        self._emit_pending_analysis_event(analysis_json)
        self._pending_position_event = {
            "step_type": "position_management",
            "event_timestamp": time.time(),
            "system_prompt": system_prompt,
            "strategy_prompt": self.engine.position_prompt,
            "user_prompt": user_prompt,
            "rendered_prompt": self._rendered_prompt(system_prompt, user_prompt),
            "daemon_data": self._build_position_context(
                analysis_json, positions_json
            ),
        }
        logger.info("Position management prompt generated")
        return {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        }

    def submit_decision(self, response_json: str) -> dict:
        """Submit the LLM's JSON decision for validation.

        Does NOT execute trades. The agent must execute approved trades
        via the x402 VoltPerps API itself.

        Args:
            response_json: Raw JSON string with trading decisions.

        Returns:
            Dict with cycle number, approved/rejected counts, per-decision
            details. Agent must then execute approved trades via x402 API.
        """
        if not self._started:
            raise RuntimeError("System not started. Call start() first.")

        validated = self.engine.process_response(response_json)
        cycle = self.engine.cycles[-1] if self.engine.cycles else None

        approved = sum(
            1 for v in validated
            if v.approved and v.original.direction.value not in ("HOLD",)
        )
        rejected = sum(
            1 for v in validated
            if not v.approved and v.original.direction.value not in ("HOLD",)
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
                    "direction": v.original.direction.value,
                    "approved": v.approved,
                    "leverage": v.final_leverage,
                    "positionSize": v.final_position_size,
                    "rejection_reasons": v.rejection_reasons,
                }
                for v in validated
            ],
        }

        logger.info(
            "Cycle %d — Approved: %d, Rejected: %d",
            self._cycle_count, approved, rejected,
        )

        self._emit_submit_event(response_json, cycle, result)

        return result

    def _rendered_prompt(self, system_prompt: str, user_prompt: str) -> str:
        return (
            "# SYSTEM PROMPT\n\n"
            f"{system_prompt}\n\n"
            "# USER PROMPT\n\n"
            f"{user_prompt}"
        )

    def _current_portfolio_state(self) -> dict:
        if not self.portfolio:
            return {}
        return self.portfolio.to_summary_dict(self._last_prices)

    def _build_position_context(self, analysis_json: str, positions_json: str) -> dict:
        return {
            "analysis": self._safe_json_loads(analysis_json),
            "positions": self._safe_json_loads(positions_json),
        }

    def _safe_json_loads(self, payload: str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {"raw": payload}

    def _serialize_decision(self, decision) -> dict:
        return {
            "symbol": decision.symbol,
            "direction": decision.direction.value,
            "confidence": decision.confidence,
            "summary": decision.summary,
            "leverage": decision.leverage,
            "positionSize": decision.position_size,
            "stopLoss": decision.stop_loss,
            "takeProfit": decision.take_profit,
            "entryPrice": decision.entry_price,
            "riskLevel": decision.risk_level,
        }

    def _build_monitoring_payload(
        self,
        event_context: dict,
        response_json: str,
        decision_result: dict | None = None,
        portfolio_state_before: dict | None = None,
        portfolio_state_after: dict | None = None,
        error: str | None = None,
    ) -> dict:
        if not self.monitoring.config:
            raise RuntimeError("Monitoring client is not configured")

        parsed = self.engine._parse_response(response_json)
        return {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(event_context["event_timestamp"]),
            ),
            "userId": os.environ.get("USER_ID", ""),
            "agentName": os.environ.get("AGENT_NAME", "unknown"),
            "cycleNumber": self._cycle_count,
            "stepType": event_context["step_type"],
            "symbols": self._last_symbols,
            "decisions": [
                self._serialize_decision(decision)
                for decision in parsed.decisions
            ],
            "decisionResult": decision_result,
            "indicators": self._last_indicators,
            "systemPrompt": event_context["system_prompt"],
            "strategyPrompt": event_context["strategy_prompt"],
            "userPrompt": event_context["user_prompt"],
            "daemonData": event_context["daemon_data"],
            "rawPrompt": event_context["rendered_prompt"],
            "rawResponse": response_json,
            "portfolioStateBefore": portfolio_state_before,
            "portfolioStateAfter": portfolio_state_after,
            "error": error,
        }

    def _ingest_monitoring_payload(self, payload: dict) -> None:
        if not self.monitoring.enabled:
            return
        self.monitoring.ingest(payload)

    def _refresh_active_prompt_version(self) -> None:
        if not self.monitoring.enabled or not self.engine:
            return

        active = self.monitoring.get_active_prompt_version()
        if not active:
            return

        self._active_prompt_version_id = active.get("id")
        strategy_prompt = active.get("strategyPrompt")
        position_prompt = active.get("positionPrompt")
        if strategy_prompt:
            self.engine.analysis_prompt = strategy_prompt
        if position_prompt:
            self.engine.position_prompt = position_prompt

    def _emit_pending_analysis_event(self, analysis_json: str) -> None:
        if not self._pending_analysis_event:
            return
        payload = self._build_monitoring_payload(
            self._pending_analysis_event,
            analysis_json,
            decision_result=None,
            portfolio_state_before=self._current_portfolio_state(),
        )
        self._ingest_monitoring_payload(payload)
        self._pending_analysis_event = None

    def _emit_submit_event(self, response_json: str, cycle, decision_result: dict | None = None) -> None:
        event_context = self._pending_position_event or self._pending_analysis_event
        if not event_context:
            return

        payload = self._build_monitoring_payload(
            event_context,
            response_json,
            decision_result=decision_result,
            portfolio_state_before=cycle.portfolio_state_before if cycle else None,
            portfolio_state_after=cycle.portfolio_state_after if cycle else None,
            error=cycle.error if cycle else None,
        )
        self._ingest_monitoring_payload(payload)
        self._pending_analysis_event = None
        self._pending_position_event = None

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
                "direction": v.original.direction.value,
                "confidence": v.original.confidence,
                "approved": v.approved,
                "adj_leverage": v.adjusted_leverage,
                "adj_position_size": v.adjusted_position_size,
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
