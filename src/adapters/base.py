"""Abstract base class for LLM adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    """Structured response from an LLM call."""

    content: str  # The main response text (JSON)
    reasoning_content: str = ""  # Extended thinking / reasoning chain
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class BaseLLMAdapter(ABC):
    """Interface for LLM providers."""

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> LLMResponse:
        """Send a prompt and get a structured response."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources."""
        ...
