"""Clarification dialogue, multi-turn conversational state, and CRUD-through-
conversation (CLAUDE.md sections 12, 19-21, 24-27, 58), driven through
`mdc.cql.interpreter.process_turn`.
"""

from pathlib import Path

import pytest

from mdc.cql.interpreter import process_turn
from mdc.engine.data_engine import MDCDataEngine
from mdc.llm.mock_provider import MockLLMProvider
from mdc.mce.state import ConversationState
from mdc.ontology.loader import load_ontology
from mdc.schema.loader import load_default_registry
from mdc.storage.duckdb_store import DuckDBStore

ontology = load_ontology()
llm = MockLLMProvider()


@pytest.fixture
def engine(tmp_path: Path) -> MDCDataEngine:
    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    return MDCDataEngine(store, load_default_registry())


def _state() -> ConversationState:
    return ConversationState(session_id="s1")


# -- TEST 002 / 008, resolved via clarification --------------------------------

def test_bare_balance_asks_then_resolves_by_number(engine: MDCDataEngine):
    state = _state()
    first = process_turn(state, "Show merchants with balance above 5000", ontology, llm, engine)
    assert first.question is not None
    assert first.context.clarification_required is True
    assert [o.value for o in first.question.options] == [
        "ledger_balance", "available_balance", "settlement_balance",
    ]
    assert state.pending is not None

    second = process_turn(state, "3", ontology, llm, engine)
    assert second.question is None
    assert second.context.clarification_required is False
    assert second.context.fields == ["settlement_balance"]
    assert second.context.conditions[0].field == "settlement_balance"
    assert second.context.conditions[0].value == 5000.0
    assert state.pending is None
    assert state.context is second.context


def test_bare_balance_resolves_by_typed_field_name(engine: MDCDataEngine):
    state = _state()
    process_turn(state, "Show merchants with balance", ontology, llm, engine)
    result = process_turn(state, "settlement balance", ontology, llm, engine)
    assert result.context.fields == ["settlement_balance"]
    assert result.context.clarification_required is False


def test_unrecognized_answer_reasks_without_dropping_state(engine: MDCDataEngine):
    state = _state()
    process_turn(state, "Show merchants with balance", ontology, llm, engine)
    result = process_turn(state, "the moon", ontology, llm, engine)
    assert result.unresolved_answer is True
    assert result.question is not None
    assert state.pending is not None  # still waiting


# -- TEST 006: typo -> clarification (options include the corrected guess) -----

def test_typo_in_field_name_triggers_clarification_with_correction_offered(engine: MDCDataEngine):
    state = _state()
    result = process_turn(
        state, "Show merchants with settlement balnce above 10000 USD", ontology, llm, engine
    )
    assert result.question is not None
    assert "settlement_balance" in [o.value for o in result.question.options]

    resolved = process_turn(state, "settlement balance", ontology, llm, engine)
    assert resolved.context.fields == ["settlement_balance"]
    assert resolved.context.conditions[0].value == 10000.0


# -- TEST 004: follow-up narrows by country, keeps prior filter ----------------

def test_followup_country_narrows_previous_query(engine: MDCDataEngine):
    state = _state()
    process_turn(state, "Show merchants with settlement balance above 10000 USD", ontology, llm, engine)
    result = process_turn(state, "Only India", ontology, llm, engine)

    assert result.context.entity == "merchant"
    assert result.context.geographic_context == {"country": "IN"}
    by_field = {c.field: c for c in result.context.conditions}
    assert by_field["settlement_balance"].value == 10000.0
    assert by_field["currency"].value == "USD"
    assert by_field["country"].value == "IN"
    assert result.context.clarification_required is False


# -- symbol operators (">", ">=", "<", "<=", "=") alongside the textual ones ----

def test_symbol_operator_matches_textual_phrase_equivalent(engine: MDCDataEngine):
    state_symbol = _state()
    symbol_result = process_turn(state_symbol, "Show merchants with settlement balance > 10000 USD", ontology, llm, engine)

    state_phrase = _state()
    phrase_result = process_turn(state_phrase, "Show merchants with settlement balance above 10000 USD", ontology, llm, engine)

    assert symbol_result.context.conditions[0].field == phrase_result.context.conditions[0].field
    assert symbol_result.context.conditions[0].operator == phrase_result.context.conditions[0].operator == ">"
    assert symbol_result.context.conditions[0].value == phrase_result.context.conditions[0].value == 10000.0


@pytest.mark.parametrize(
    ("symbol", "operator"),
    [(">=", ">="), ("<=", "<="), ("<", "<"), ("=", "=")],
)
def test_symbol_operators_map_to_the_right_operator(engine: MDCDataEngine, symbol: str, operator: str):
    state = _state()
    result = process_turn(state, f"Show merchants with settlement balance {symbol} 10000", ontology, llm, engine)
    assert result.context.conditions[0].operator == operator
    assert result.context.conditions[0].value == 10000.0


# -- TEST 005: follow-up adds sort, keeps prior filters -------------------------

def test_followup_sort_adds_order_by_and_keeps_filters(engine: MDCDataEngine):
    state = _state()
    process_turn(state, "Show merchants with settlement balance above 10000 USD", ontology, llm, engine)
    process_turn(state, "Only India", ontology, llm, engine)
    result = process_turn(state, "Sort highest first", ontology, llm, engine)

    assert result.context.order_by[0].field == "settlement_balance"
    assert result.context.order_by[0].direction == "DESC"
    by_field = {c.field: c for c in result.context.conditions}
    assert by_field["country"].value == "IN"
    assert by_field["settlement_balance"].value == 10000.0


# -- A fresh, self-contained query resets prior conversational state -----------

def test_new_complete_query_replaces_prior_context_instead_of_merging(engine: MDCDataEngine):
    state = _state()
    process_turn(state, "Show merchants with settlement balance above 10000 USD", ontology, llm, engine)
    result = process_turn(state, "Show all merchants", ontology, llm, engine)

    assert result.context.conditions == []
    assert result.context.geographic_context is None


# -- TEST 058: full CRUD-through-conversation acceptance flow -------------------

def test_full_crud_conversation_acceptance_flow(engine: MDCDataEngine):
    state = _state()

    created = process_turn(state, "Create a merchant called ABC Store in India", ontology, llm, engine)
    assert created.operation_result is not None
    assert created.operation_result.records[0].fields["name"] == "ABC Store"
    assert created.operation_result.records[0].fields["country"] == "IN"

    shown = process_turn(state, "Show me ABC Store", ontology, llm, engine)
    assert shown.operation_result is not None
    assert shown.operation_result.records[0].fields["name"] == "ABC Store"

    ambiguous = process_turn(state, "Change ABC Store balance to 15000", ontology, llm, engine)
    assert ambiguous.question is not None
    assert state.pending is not None

    updated = process_turn(state, "settlement balance", ontology, llm, engine)
    assert updated.operation_result is not None
    assert updated.operation_result.records[0].fields["settlement_balance"] == 15000.0

    shown_again = process_turn(state, "Show me ABC Store", ontology, llm, engine)
    assert shown_again.operation_result.records[0].fields["settlement_balance"] == 15000.0

    asked = process_turn(state, "Delete merchant ABC Store", ontology, llm, engine)
    assert asked.confirmation_prompt is not None
    assert state.confirmation is not None

    deleted = process_turn(state, "yes", ontology, llm, engine)
    assert deleted.operation_result is not None
    assert deleted.operation_result.count == 1
    assert state.confirmation is None

    gone = process_turn(state, "Show me ABC Store", ontology, llm, engine)
    assert gone.operation_result is None
    assert gone.error == "No merchant found named 'ABC Store'."


def test_delete_confirmation_declined_does_not_delete(engine: MDCDataEngine):
    state = _state()
    process_turn(state, "Create a merchant called XYZ Retail in India", ontology, llm, engine)
    process_turn(state, "Delete merchant XYZ Retail", ontology, llm, engine)
    result = process_turn(state, "no", ontology, llm, engine)

    assert result.error is not None
    assert state.confirmation is None
    still_there = process_turn(state, "Show me XYZ Retail", ontology, llm, engine)
    assert still_there.operation_result is not None


def test_create_without_required_name_reports_schema_error(engine: MDCDataEngine):
    state = _state()
    result = process_turn(state, "Create a merchant in India", ontology, llm, engine)
    assert result.error is not None
