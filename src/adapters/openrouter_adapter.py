"""OpenRouter adapter — single adapter for all models via OpenRouter API."""

from __future__ import annotations

import json
import logging

import httpx

from ..models.config import OpenRouterConfig
from .base import BaseLLMAdapter, LLMResponse

logger = logging.getLogger(__name__)


class OpenRouterAdapter(BaseLLMAdapter):
    """Calls any model through OpenRouter's unified API."""

    def __init__(self, config: OpenRouterConfig) -> None:
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/orderly-trader",
            },
            timeout=config.timeout,
        )

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> LLMResponse:
        """Send a chat completion request to OpenRouter."""
        body: dict = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }

        # Grok models support reasoning_effort for extended thinking
        if "grok" in self.config.model:
            body["reasoning"] = {"effort": self.config.reasoning_effort}

        logger.info("Calling OpenRouter model=%s", self.config.model)

        resp = await self.client.post("/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]["message"]
        usage = data.get("usage", {})

        # Log raw message keys to debug reasoning_content capture
        logger.debug("Raw message keys: %s", list(choice.keys()))
        if "reasoning_content" in choice:
            logger.info("Reasoning content captured: %d chars", len(choice["reasoning_content"] or ""))
        elif "reasoning" in choice:
            logger.info("Found 'reasoning' key instead of 'reasoning_content'")
        else:
            logger.warning("No reasoning field in response. Message keys: %s", list(choice.keys()))

        # OpenRouter may return reasoning under different keys
        reasoning = (
            choice.get("reasoning_content")
            or choice.get("reasoning")
            or ""
        )

        return LLMResponse(
            content=choice.get("content", ""),
            reasoning_content=reasoning,
            model=data.get("model", self.config.model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )

    async def close(self) -> None:
        await self.client.aclose()
