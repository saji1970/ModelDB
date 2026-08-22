"""Deterministic offline LLMProvider (CLAUDE.md section 42 - mandatory).

Pure keyword matching, no network calls, so tests and local development
never depend on an external API. Confidence values are heuristic but
fixed, so the same input always classifies the same way.
"""

from __future__ import annotations

from typing import Any

from mdc.llm.interface import LLMProvider

INTENT_KEYWORDS: dict[str, list[str]] = {
    "COUNT": ["count", "how many", "number of"],
    "SUM": ["sum", "total"],
    "AVG": ["average", "avg", "mean"],
    # "highest"/"lowest" are deliberately excluded here - in this domain
    # they mark sort direction ("sort highest first"), not the MIN/MAX
    # aggregate intent, so they belong to SORT's vocabulary instead.
    "MIN": ["minimum", "smallest"],
    "MAX": ["maximum", "largest"],
    "SORT": ["sort", "order by", "rank", "highest first", "lowest first", "ascending", "descending"],
    "HELP": ["help"],
    "EXPLAIN": ["explain"],
    "CREATE": ["create", "add a", "register a", "new merchant"],
    "UPDATE": ["change", "update", "set "],
    "DELETE": ["delete", "remove"],
    "FETCH": ["show", "list", "find", "give me", "which", "get", "display"],
}

BASE_CONFIDENCE = 0.80
DEFAULT_FETCH_CONFIDENCE = 0.60


class MockLLMProvider(LLMProvider):
    def classify(self, text: str, labels: list[str]) -> tuple[str, float]:
        normalized = f" {text.lower()} "

        # FETCH ("show", "list", "find", ...) is the generic fallback verb
        # and commonly co-occurs with a more specific intent ("show the
        # minimum settlement balance" is MIN, not FETCH), so any specific
        # intent match takes priority over a FETCH match.
        specific_labels = [label for label in labels if label != "FETCH"]
        best_label, best_score = self._best_match(normalized, specific_labels)
        if best_label is None and "FETCH" in labels:
            best_label, best_score = self._best_match(normalized, ["FETCH"])

        if best_label is not None:
            return best_label, round(best_score, 4)
        if "FETCH" in labels:
            return "FETCH", DEFAULT_FETCH_CONFIDENCE
        return labels[0], 0.5

    @staticmethod
    def _best_match(normalized: str, labels: list[str]) -> tuple[str | None, float]:
        best_label: str | None = None
        best_score = 0.0
        for label in labels:
            keywords = INTENT_KEYWORDS.get(label, [label.lower()])
            matched = [kw for kw in keywords if kw in normalized]
            if not matched:
                continue
            longest = max(matched, key=len)
            score = min(0.99, BASE_CONFIDENCE + 0.02 * len(longest.split()))
            if score > best_score:
                best_score = score
                best_label = label
        return best_label, best_score

    def interpret(self, text: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"raw_text": text, "tokens": text.lower().split()}

    def extract(self, text: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {}

    def explain(self, context: dict[str, Any]) -> str:
        parts = []
        if context.get("intent"):
            parts.append(f"Intent: {context['intent']}")
        if context.get("entity"):
            parts.append(f"Entity: {context['entity']}")
        if context.get("conditions"):
            parts.append(f"Filters: {len(context['conditions'])}")
        return " | ".join(parts) if parts else "No interpretation available yet."
