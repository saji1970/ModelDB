"""LLMProvider interface (CLAUDE.md section 41).

Nothing outside this package may import a specific vendor SDK
(OpenAI/Anthropic/Gemini/Ollama/...) directly - everything goes through
this interface, so `mdc/mce` never knows or cares which provider is
configured. The LLM proposes meaning (intent classification, rough
extraction); it never sees or produces SQL (section 3, section 36).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMProvider(ABC):
    @abstractmethod
    def classify(self, text: str, labels: list[str]) -> tuple[str, float]:
        """Return the best-matching label from `labels` and a confidence in [0, 1]."""

    @abstractmethod
    def interpret(self, text: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return a rough, unvalidated structural reading of `text` (proposal only)."""

    @abstractmethod
    def extract(self, text: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Extract values from `text` matching the shape of `schema` (proposal only)."""

    @abstractmethod
    def explain(self, context: dict[str, Any]) -> str:
        """Produce a human-readable explanation of an already-resolved context."""
