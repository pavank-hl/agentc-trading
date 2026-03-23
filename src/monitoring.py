"""Monitoring helpers for synchronous decision event ingestion."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class MonitoringConfig:
    api_url: str
    bot_api_key: str
    prompt_version_id: str | None = None

    @classmethod
    def from_env(cls) -> MonitoringConfig | None:
        api_url = os.environ.get("VOLT_API_URL", "").rstrip("/")
        bot_api_key = os.environ.get("BOT_MONITORING_API_KEY", "")
        prompt_version_id = os.environ.get("PROMPT_VERSION_ID", "") or None

        if not api_url and not bot_api_key and not prompt_version_id:
            return None

        missing = [
            name
            for name, value in (
                ("VOLT_API_URL", api_url),
                ("BOT_MONITORING_API_KEY", bot_api_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Monitoring is partially configured. Missing env vars: "
                + ", ".join(missing)
            )

        return cls(
            api_url=api_url,
            bot_api_key=bot_api_key,
            prompt_version_id=prompt_version_id,
        )


class DecisionMonitoringClient:
    def __init__(self, config: MonitoringConfig | None = None) -> None:
        self.config = config or MonitoringConfig.from_env()

    @property
    def enabled(self) -> bool:
        return self.config is not None

    def ingest(self, payload: dict) -> None:
        if not self.config:
            return

        body = json.dumps(payload, default=str).encode()
        req = urllib.request.Request(
            f"{self.config.api_url}/monitoring/ingest",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Bot-Api-Key": self.config.bot_api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status not in (200, 201):
                    raise RuntimeError(
                        f"Monitoring ingest returned unexpected status {resp.status}"
                    )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(
                f"Monitoring ingest failed with status {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Monitoring ingest connection failed: {exc}") from exc

    def get_active_prompt_version(self) -> dict | None:
        if not self.config:
            return None

        req = urllib.request.Request(
            f"{self.config.api_url}/monitoring/prompt-version/active",
            headers={"X-Bot-Api-Key": self.config.bot_api_key},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"Active prompt fetch returned unexpected status {resp.status}"
                    )
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            detail = exc.read().decode("utf-8", errors="ignore")
            logger.warning(
                "Active prompt fetch failed with status %s: %s",
                exc.code,
                detail,
            )
            return None
        except urllib.error.URLError as exc:
            logger.warning("Active prompt fetch connection failed: %s", exc)
            return None
