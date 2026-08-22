"""Intent classification (CLAUDE.md section 19)."""

from __future__ import annotations

from mdc.llm.interface import LLMProvider

INTENT_LABELS = [
    "FETCH", "COUNT", "SUM", "AVG", "MIN", "MAX", "SORT", "HELP", "EXPLAIN",
    "CREATE", "UPDATE", "DELETE",
]


def classify_intent(text: str, llm: LLMProvider) -> tuple[str, float]:
    return llm.classify(text, INTENT_LABELS)
