"""Phase 2: QueryContext model validation and the end-to-end resolver pipeline.

The resolver tests below are the Phase-2-reachable subset of the section 57
required acceptance tests: single-utterance interpretation only, no
conversational state (that's Phase 3 - see mdc/mce/resolver.py docstring).
"""

import pytest
from pydantic import ValidationError

from mdc.llm.mock_provider import MockLLMProvider
from mdc.mce.context import Condition, QueryContext
from mdc.mce.resolver import interpret
from mdc.ontology.loader import load_ontology

ontology = load_ontology()
llm = MockLLMProvider()


def test_condition_rejects_invalid_operator():
    with pytest.raises(ValidationError):
        Condition(field="settlement_balance", operator="~=", value=1, datatype="decimal")


def test_query_context_defaults():
    ctx = QueryContext(session_id="s1")
    assert ctx.intent is None
    assert ctx.fields == []
    assert ctx.clarification_required is False


# -- TEST 001 -----------------------------------------------------------------

def test_show_all_merchants_no_clarification():
    ctx = interpret("Show all merchants", ontology, llm, session_id="s1")
    assert ctx.intent == "FETCH"
    assert ctx.entity == "merchant"
    assert ctx.clarification_required is False


# -- TEST 002 / 008 -------------------------------------------------------------

def test_bare_balance_requires_clarification():
    ctx = interpret("Show merchants with balance", ontology, llm, session_id="s1")
    assert ctx.clarification_required is True
    assert ctx.fields == []


def test_balance_above_5000_still_ambiguous_about_which_balance():
    ctx = interpret("Show merchants with balance above 5000", ontology, llm, session_id="s1")
    assert ctx.clarification_required is True
    assert ctx.conditions == []  # no field confidently chosen -> no bogus condition asserted


# -- TEST 003 -------------------------------------------------------------------

def test_settlement_balance_above_10000_usd():
    ctx = interpret(
        "Show merchants with settlement balance above 10000 USD", ontology, llm, session_id="s1"
    )
    assert ctx.entity == "merchant"
    assert ctx.clarification_required is False
    assert ctx.fields == ["settlement_balance"]
    by_field = {c.field: c for c in ctx.conditions}
    assert by_field["settlement_balance"].operator == ">"
    assert by_field["settlement_balance"].value == 10000.0
    assert by_field["currency"].value == "USD"


# -- TEST 007 -------------------------------------------------------------------

def test_available_balance_above_5000_resolves_to_available_balance_only():
    ctx = interpret("Show merchants with available balance above 5000", ontology, llm, session_id="s1")
    assert ctx.fields == ["available_balance"]
    assert ctx.conditions[0].field == "available_balance"
    assert ctx.conditions[0].value == 5000.0


# -- TEST 009 -------------------------------------------------------------------

def test_count_merchants_in_india():
    ctx = interpret("Count merchants in India", ontology, llm, session_id="s1")
    assert ctx.intent == "COUNT"
    assert ctx.geographic_context == {"country": "IN"}
    assert any(c.field == "country" and c.value == "IN" for c in ctx.conditions)
    # No field was actually mentioned - regression check for the fuzzy
    # fallback previously mistaking "count" for a typo of the "country"
    # field alias and flagging a spurious ambiguity.
    assert ctx.clarification_required is False
    assert ctx.fields == []


# -- TEST 010 -------------------------------------------------------------------

def test_top_20_merchants_by_settlement_balance():
    ctx = interpret("Show top 20 merchants by settlement balance", ontology, llm, session_id="s1")
    assert ctx.entity == "merchant"
    assert ctx.limit == 20
    assert ctx.order_by[0].field == "settlement_balance"
    assert ctx.order_by[0].direction == "DESC"
    # "by settlement balance" is not a filter - no WHERE condition should appear.
    assert ctx.conditions == []


# -- Phase 3 dependency, documented rather than hidden --------------------------

def test_standalone_sort_followup_needs_conversation_state_not_yet_built():
    """"Sort highest first" alone has no entity - Phase 3 must merge it with
    the prior turn's context before this can resolve. Phase 2 correctly
    flags it as needing clarification instead of guessing."""
    ctx = interpret("Sort highest first", ontology, llm, session_id="s1")
    assert ctx.intent == "SORT"
    assert ctx.entity is None
    assert ctx.clarification_required is True
