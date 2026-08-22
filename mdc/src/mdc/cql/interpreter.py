"""Conversational turn orchestration (CLAUDE.md section 3, 55: "interpreter").

This is where `Conversation -> MCE -> MDCOperation -> DataEngine`
(section 3) actually happens - the single place that decides, for one
turn of user input, whether to:

1. Resume a pending clarification (section 19, 24) or destructive-
   action confirmation (section 58) from the previous turn.
2. Route a CREATE/UPDATE/DELETE intent through `cql.crud` into an
   `MDCOperation` and execute it against the `DataEngine` - never
   building a `QueryContext` for these, since they're not queries.
3. Route everything else through the read-oriented MCE pipeline
   (`mce.resolver`), including the multi-turn context-modification
   behavior a bare FETCH/COUNT/... conversation already had.

Every write goes through `engine.execute()` - never direct storage
access - so the CLI, and anything else built on top of this later
(REST API, SDK), stays on the one Data Engine (section 28).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mdc.cql.crud import MERCHANTS_COLLECTION, build_delete_operation, extract_create_data, extract_update_request, locate_alias
from mdc.engine.base import DataEngine, OperationResult
from mdc.engine.errors import DataEngineError
from mdc.llm.interface import LLMProvider
from mdc.mce.ambiguity import ClarificationQuestion, build_question, resolve_answer
from mdc.mce.confidence import evaluate_ambiguity
from mdc.mce.context import Condition, QueryContext
from mdc.mce.entities import COUNTRY_NAMES, extract_conditions
from mdc.mce.resolver import interpret_detailed
from mdc.mce.state import ConversationState, PendingClarification, PendingConfirmation, PendingCrudContinuation
from mdc.model.operation import CreateOperation, DeleteOperation, Filter, ReadOperation, UpdateOperation
from mdc.ontology.ontology import Ontology
from mdc.schema.registry import SchemaError

MERCHANT_ENTITY = "merchant"

_FETCH_VERB = re.compile(r"^\s*(?:show me|show|get|find|give me|display)\s+", re.IGNORECASE)
_LEADING_MERCHANT_WORD = re.compile(r"^\s*(?:the\s+)?merchants?\s+", re.IGNORECASE)
_FOLLOWUP_HINT_WORDS = ("only", "sort", "highest", "lowest", "ascending", "descending", "above", "below", "over", "under")


def _looks_like_a_followup_not_a_name(text: str) -> bool:
    """"Only India" / "Sort highest first" default to FETCH intent (the
    MockLLMProvider's fallback label) just like "Show me ABC Store" does,
    but they're context-modification follow-ups (sections 25-27), not a
    proper-noun record lookup - recognized here by the same country-name/
    filter-keyword vocabulary the read pipeline itself uses, so they fall
    through to the follow-up merge instead of being searched for by name."""
    normalized = text.lower()
    if any(re.search(r"\b" + re.escape(name) + r"\b", normalized) for name in COUNTRY_NAMES):
        return True
    return any(re.search(r"\b" + word + r"\b", normalized) for word in _FOLLOWUP_HINT_WORDS)


@dataclass(frozen=True)
class TurnResult:
    context: QueryContext
    question: ClarificationQuestion | None = None
    unresolved_answer: bool = False
    confirmation_prompt: str | None = None
    operation_result: OperationResult | None = None
    error: str | None = None


def _crud_context(state: ConversationState, intent: str, **overrides) -> QueryContext:
    """A `QueryContext` shaped only for display (`/context`, TurnResult.context)
    - CRUD handlers never write this onto `state.context`. If they did, a later
    turn's failed-lookup or genuinely entity-less utterance would treat this
    leftover, condition-less context as the analytics query to extend
    (sections 25-27), instead of correctly falling through to "I need more
    information" - `state.context` is reserved for the read pipeline.
    """
    return QueryContext(session_id=state.session_id, intent=intent, entity=MERCHANT_ENTITY, **overrides)


def process_turn(state: ConversationState, text: str, ontology: Ontology, llm: LLMProvider, engine: DataEngine) -> TurnResult:
    if state.confirmation is not None:
        return _resolve_confirmation(state, text, engine)

    if state.pending is not None:
        return _continue_clarification(state, text, ontology, engine)

    detailed = interpret_detailed(text, ontology, llm, state.session_id)
    ctx = detailed.context

    if ctx.intent == "CREATE":
        return _handle_create(state, text, engine)
    if ctx.intent == "DELETE":
        return _handle_delete(state, text)
    if ctx.intent == "UPDATE":
        return _handle_update(state, text, ontology, engine)

    if ctx.intent == "FETCH" and ctx.entity is None:
        lookup = _try_fetch_by_name(state, text, engine)
        if lookup is not None:
            return lookup

    previous = state.context
    if ctx.entity is None and previous is not None and previous.entity is not None and not previous.clarification_required:
        merged = _merge_followup(previous, text, ontology)
        state.context = merged
        return TurnResult(context=merged)

    if ctx.clarification_required:
        candidates = detailed.field_candidates if len(detailed.field_candidates) >= 2 else (
            detailed.entity_candidates if len(detailed.entity_candidates) >= 2 else ()
        )
        if candidates:
            question = build_question(list(candidates))
            state.pending = PendingClarification(question=question, original_text=text, base_context=ctx)
            return TurnResult(context=ctx, question=question)
        state.pending = None
        return TurnResult(context=ctx)

    state.context = ctx
    state.pending = None
    return TurnResult(context=ctx)


# -- CREATE ---------------------------------------------------------------------

def _handle_create(state: ConversationState, text: str, engine: DataEngine) -> TurnResult:
    data = extract_create_data(text)
    ctx = _crud_context(state, "CREATE")
    try:
        result = engine.create(CreateOperation(collection=MERCHANTS_COLLECTION, data=data))
    except SchemaError as exc:
        return TurnResult(context=ctx, error=str(exc))
    return TurnResult(context=ctx, operation_result=result)


# -- DELETE (asks for confirmation first, section 58) ----------------------------

def _handle_delete(state: ConversationState, text: str) -> TurnResult:
    operation = build_delete_operation(text, collection=MERCHANTS_COLLECTION)
    name = operation.filters[0].value
    description = f"delete merchant {name!r}"
    state.confirmation = PendingConfirmation(operation=operation, description=description)
    ctx = _crud_context(state, "DELETE", clarification_required=True)
    return TurnResult(context=ctx, confirmation_prompt=f"Delete merchant {name!r}? (yes/no)")


def _resolve_confirmation(state: ConversationState, answer: str, engine: DataEngine) -> TurnResult:
    pending = state.confirmation
    assert pending is not None
    state.confirmation = None
    ctx = _crud_context(state, "DELETE")

    if answer.strip().lower() not in ("yes", "y", "confirm"):
        return TurnResult(context=ctx, error="Cancelled - nothing was deleted.")

    try:
        result = engine.delete(pending.operation)
    except DataEngineError as exc:
        return TurnResult(context=ctx, error=str(exc))
    return TurnResult(context=ctx, operation_result=result)


# -- FETCH-by-name lookup against the merchants collection ----------------------
#
# "Show all merchants with settlement balance above 10000" resolves an
# ontology entity and goes through the analytics read pipeline
# (mce.resolver) unchanged. "Show me ABC Store" doesn't match any
# ontology entity/field alias at all - it's a plain proper-noun lookup
# against a record created via CRUD - so it's handled here instead,
# directly against the DataEngine.

def _try_fetch_by_name(state: ConversationState, text: str, engine: DataEngine) -> TurnResult | None:
    remainder = _FETCH_VERB.sub("", text)
    remainder = _LEADING_MERCHANT_WORD.sub("", remainder)
    name = remainder.strip(" .?")
    if not name or _looks_like_a_followup_not_a_name(name):
        return None

    result = engine.read(ReadOperation(collection=MERCHANTS_COLLECTION, filters=[Filter(field="name", operator="=", value=name)]))
    if result.count == 0:
        return TurnResult(context=_crud_context(state, "FETCH"), error=f"No merchant found named {name!r}.")
    if result.count > 1:
        return None  # ambiguous multi-match on name - not handled specially, fall through
    return TurnResult(context=_crud_context(state, "FETCH"), operation_result=result)


# -- UPDATE (field ambiguity reuses the same clarification engine as reads) -----

def _handle_update(state: ConversationState, text: str, ontology: Ontology, engine: DataEngine) -> TurnResult:
    field_candidates = ontology.resolve_field(MERCHANT_ENTITY, text.lower())
    ctx = _crud_context(state, "UPDATE", clarification_required=True)
    if not field_candidates:
        return TurnResult(context=ctx, error="I couldn't tell which field to update.")

    ambiguity = evaluate_ambiguity(field_candidates)
    top_candidate = field_candidates[0]
    alias_start = locate_alias(text, top_candidate)
    request = extract_update_request(text, top_candidate.matched_alias, alias_start)
    name_filter = Filter(field="name", operator="=", value=request.name)

    if not ambiguity.confident:
        question = build_question(field_candidates)
        state.pending = PendingClarification(
            question=question,
            original_text=text,
            base_context=ctx,
            crud=PendingCrudContinuation(kind="UPDATE", collection=MERCHANTS_COLLECTION, name_filter=name_filter, value=request.value),
        )
        return TurnResult(context=ctx, question=question)

    return _execute_update(state, engine, name_filter, top_candidate.value, request.value)


def _execute_update(state: ConversationState, engine: DataEngine, name_filter: Filter, field_name: str, value) -> TurnResult:
    ctx = _crud_context(state, "UPDATE")
    try:
        result = engine.update(UpdateOperation(collection=MERCHANTS_COLLECTION, filters=[name_filter], data={field_name: value}))
    except (SchemaError, DataEngineError) as exc:
        return TurnResult(context=ctx, error=str(exc))
    return TurnResult(context=ctx, operation_result=result)


def _continue_clarification(state: ConversationState, answer: str, ontology: Ontology, engine: DataEngine) -> TurnResult:
    pending = state.pending
    assert pending is not None
    resolved_value = resolve_answer(pending.question, answer)

    if resolved_value is None:
        return TurnResult(context=pending.base_context, question=pending.question, unresolved_answer=True)

    state.pending = None

    if pending.crud is not None:
        return _execute_update(state, engine, pending.crud.name_filter, resolved_value, pending.crud.value)

    entity = pending.base_context.entity
    assert entity is not None
    extraction = extract_conditions(pending.original_text, entity, ontology, resolved_value)

    geographic_context = pending.base_context.geographic_context
    for condition in extraction.conditions:
        if condition.field == "country":
            geographic_context = {"country": condition.value}

    ctx = pending.base_context.model_copy(
        update={
            "fields": [resolved_value],
            "conditions": extraction.conditions,
            "order_by": extraction.order_by or pending.base_context.order_by,
            "limit": extraction.limit if extraction.limit is not None else pending.base_context.limit,
            "geographic_context": geographic_context,
            "confidence": 1.0,
            "complete": True,
            "clarification_required": False,
        }
    )
    state.context = ctx
    return TurnResult(context=ctx)


def _merge_conditions(previous: list[Condition], new: list[Condition]) -> list[Condition]:
    by_field = {c.field: c for c in previous}
    order = [c.field for c in previous]
    for condition in new:
        if condition.field not in by_field:
            order.append(condition.field)
        by_field[condition.field] = condition
    return [by_field[field_name] for field_name in order]


def _merge_followup(previous: QueryContext, text: str, ontology: Ontology) -> QueryContext:
    entity = previous.entity
    assert entity is not None

    top_field = previous.fields[0] if previous.fields else None
    extraction = extract_conditions(text, entity, ontology, top_field)

    conditions = _merge_conditions(previous.conditions, extraction.conditions)
    geographic_context = previous.geographic_context
    for condition in extraction.conditions:
        if condition.field == "country":
            geographic_context = {"country": condition.value}

    return previous.model_copy(
        update={
            "conditions": conditions,
            "order_by": extraction.order_by or previous.order_by,
            "limit": extraction.limit if extraction.limit is not None else previous.limit,
            "geographic_context": geographic_context,
            "clarification_required": False,
            "complete": True,
        }
    )
