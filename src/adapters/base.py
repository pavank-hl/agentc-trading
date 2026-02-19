"""Data structures for LLM responses."""

from __future__ import annotations

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
