from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from src.main import TradingSystem
from src.models.decision import Action, MultiSymbolDecision, TradeDecision
from src.monitoring import DecisionMonitoringClient, MonitoringConfig


class TestDecisionMonitoringClient:
    def test_ingest_success(self, monkeypatch):
        client = DecisionMonitoringClient(
            MonitoringConfig(
                api_url="http://localhost:3000",
                bot_api_key="secret",
            )
        )
        called = {}

        class DummyResponse:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(req, timeout):
            called["url"] = req.full_url
            called["headers"] = dict(req.header_items())
            called["timeout"] = timeout
            return DummyResponse()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        client.ingest({"hello": "world"})

        assert called["url"] == "http://localhost:3000/monitoring/ingest"
        assert called["headers"]["X-bot-api-key"] == "secret"
        assert called["timeout"] == 10

    def test_ingest_http_error_raises(self, monkeypatch):
        client = DecisionMonitoringClient(
            MonitoringConfig(
                api_url="http://localhost:3000",
                bot_api_key="secret",
            )
        )

        def fake_urlopen(req, timeout):
            raise HTTPError(
                url=req.full_url,
                code=500,
                msg="boom",
                hdrs=None,
                fp=None,
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        with pytest.raises(RuntimeError):
            client.ingest({"hello": "world"})


class TestTradingSystemMonitoringPayload:
    def test_build_analysis_payload(self):
        system = TradingSystem()
        system._cycle_count = 3
        system._last_symbols = ["PERP_ETH_USDC"]
        system._last_indicators = {"PERP_ETH_USDC": {"fear_greed_index": 42}}
        system.monitoring = DecisionMonitoringClient(
            MonitoringConfig(
                api_url="http://localhost:3000",
                bot_api_key="secret",
            )
        )
        system.engine = SimpleNamespace(
            _parse_response=lambda payload: MultiSymbolDecision(
                decisions=[
                    TradeDecision(
                        symbol="PERP_ETH_USDC",
                        direction=Action.LONG,
                        confidence=75,
                        summary="Bullish",
                        leverage=5,
                        position_size=0.25,
                        stop_loss=2500,
                        take_profit=2800,
                        entry_price=2600,
                        risk_level="HIGH",
                    )
                ]
            )
        )

        payload = system._build_monitoring_payload(
            {
                "step_type": "analysis",
                "event_timestamp": 1_700_000_000,
                "system_prompt": "sys",
                "strategy_prompt": "strategy",
                "user_prompt": "user",
                "rendered_prompt": "prompt",
                "daemon_data": {"symbols": ["PERP_ETH_USDC"]},
            },
            '{"decisions":[{"symbol":"PERP_ETH_USDC","direction":"LONG"}]}',
            portfolio_state_before={"margin_in_use": 0},
            portfolio_state_after={"margin_in_use": 10},
        )

        assert payload["stepType"] == "analysis"
        assert payload["cycleNumber"] == 3
        assert payload["decisions"][0]["direction"] == "LONG"
        assert payload["portfolioStateAfter"]["margin_in_use"] == 10
        assert payload["strategyPrompt"] == "strategy"

    def test_build_position_payload(self):
        system = TradingSystem()
        system._cycle_count = 4
        system._last_symbols = ["PERP_BTC_USDC"]
        system._last_indicators = {"PERP_BTC_USDC": {"fear_greed_index": 55}}
        system.monitoring = DecisionMonitoringClient(
            MonitoringConfig(
                api_url="http://localhost:3000",
                bot_api_key="secret",
            )
        )
        system.engine = SimpleNamespace(
            _parse_response=lambda payload: MultiSymbolDecision(
                decisions=[
                    TradeDecision(
                        symbol="PERP_BTC_USDC",
                        direction=Action.HOLD,
                        confidence=45,
                        summary="Wait",
                    )
                ]
            )
        )

        payload = system._build_monitoring_payload(
            {
                "step_type": "position_management",
                "event_timestamp": 1_700_000_010,
                "system_prompt": "sys",
                "strategy_prompt": "position-strategy",
                "user_prompt": "position-user",
                "rendered_prompt": "position-prompt",
                "daemon_data": {"analysis": {"decisions": []}},
            },
            '{"decisions":[{"symbol":"PERP_BTC_USDC","direction":"HOLD"}]}',
        )

        assert payload["stepType"] == "position_management"
        assert payload["decisions"][0]["direction"] == "HOLD"
        assert payload["userPrompt"] == "position-user"
