"""Phase 2: intent classification via the mandatory offline MockLLMProvider."""

import pytest

from mdc.llm.mock_provider import MockLLMProvider
from mdc.mce.intent import classify_intent

llm = MockLLMProvider()


@pytest.mark.parametrize(
    "text,expected_intent",
    [
        ("Show all merchants", "FETCH"),
        ("Count merchants in India", "COUNT"),
        ("What is the total settlement amount", "SUM"),
        ("What is the average settlement balance", "AVG"),
        ("Show the minimum settlement balance", "MIN"),
        ("Show the maximum settlement balance", "MAX"),
        ("Sort highest first", "SORT"),
        ("help", "HELP"),
        ("explain the last query", "EXPLAIN"),
    ],
)
def test_classify_intent(text: str, expected_intent: str):
    intent, confidence = classify_intent(text, llm)
    assert intent == expected_intent
    assert 0.0 <= confidence <= 1.0


def test_unrecognized_text_falls_back_to_fetch_with_lower_confidence():
    intent, confidence = classify_intent("merchants settlement balance", llm)
    assert intent == "FETCH"
    assert confidence < 0.9


def test_classification_is_deterministic():
    a = classify_intent("Show merchants with settlement balance above 10000 USD", llm)
    b = classify_intent("Show merchants with settlement balance above 10000 USD", llm)
    assert a == b
