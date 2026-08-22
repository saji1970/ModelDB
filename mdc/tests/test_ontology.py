"""Phase 2: ontology alias resolution and the section 15 ambiguity rule."""

import pytest

from mdc.ontology.loader import load_ontology

ontology = load_ontology()


def test_resolve_entity_exact_alias():
    candidates = ontology.resolve_entity("Show all merchants")
    assert candidates[0].value == "merchant"
    assert candidates[0].confidence == 1.0


def test_generic_balance_alias_is_ambiguous_across_three_fields():
    candidates = ontology.resolve_field("merchant", "show merchants with balance")
    values = {c.value for c in candidates}
    assert values == {"ledger_balance", "available_balance", "settlement_balance"}
    assert all(c.confidence == 1.0 for c in candidates)


def test_specific_alias_resolves_uniquely_despite_containing_generic_alias():
    candidates = ontology.resolve_field("merchant", "settlement balance above 10000 usd")
    assert [c.value for c in candidates] == ["settlement_balance"]


def test_available_balance_does_not_resolve_to_settlement_balance():
    candidates = ontology.resolve_field("merchant", "available balance above 5000")
    assert [c.value for c in candidates] == ["available_balance"]


def test_no_field_mentioned_returns_no_candidates():
    assert ontology.resolve_field("merchant", "show all merchants") == []


def test_typo_is_detected_via_fuzzy_fallback():
    candidates = ontology.resolve_field("merchant", "settlement balnce above 10000 usd")
    assert candidates[0].value == "settlement_balance"
    assert candidates[0].is_typo is True
    assert 0.0 < candidates[0].confidence < 1.0


def test_unknown_entity_raises():
    with pytest.raises(KeyError):
        ontology.resolve_field("spaceship", "balance")
