"""Top-level MCE pipeline: raw text -> QueryContext (CLAUDE.md section 55, steps 1-6).

`interpret()` covers the pipeline up through ambiguity detection for a
*single* utterance: intent classification, deterministic entity/field
resolution, and condition/order/limit extraction. It does not merge
against a prior turn's context (section 25-27) or run the interactive
clarification dialogue (section 24) - both need conversational state,
implemented in `mdc.cql.interpreter` (Phase 3) on top of this module.
A standalone follow-up like "Sort highest first" is therefore
correctly reported here as needing clarification (no entity in this
utterance to sort) rather than silently guessing one; it's
`cql.interpreter.process_turn` that recognizes it as a follow-up and
merges it against the prior turn.

`interpret_detailed()` is the same pipeline but also returns the raw
entity/field candidates, which `interpret()` discards - the
clarification engine (`mce.ambiguity`) needs those candidates to build
a "did you mean" question; the plain `QueryContext` alone doesn't
carry them (by design - section 20 doesn't include them, and Phase
4/5 consumers of `QueryContext` shouldn't need to care).
"""

from __future__ import annotations

from dataclasses import dataclass

from mdc.llm.interface import LLMProvider
from mdc.mce.confidence import evaluate_ambiguity
from mdc.mce.context import QueryContext
from mdc.mce.entities import ExtractionResult, extract_conditions
from mdc.mce.intent import classify_intent
from mdc.ontology.ontology import Candidate, Ontology


@dataclass(frozen=True)
class InterpretationResult:
    context: QueryContext
    entity_candidates: tuple[Candidate, ...]
    field_candidates: tuple[Candidate, ...]


def interpret(text: str, ontology: Ontology, llm: LLMProvider, session_id: str) -> QueryContext:
    return interpret_detailed(text, ontology, llm, session_id).context


def interpret_detailed(
    text: str, ontology: Ontology, llm: LLMProvider, session_id: str
) -> InterpretationResult:
    intent, intent_confidence = classify_intent(text, llm)

    entity_candidates = ontology.resolve_entity(text)
    entity = entity_candidates[0].value if entity_candidates else None
    if entity_candidates:
        entity_ambiguity = evaluate_ambiguity(entity_candidates)
        entity_clarification = entity_ambiguity.clarification_required
        entity_confidence = entity_ambiguity.top_confidence
    else:
        entity_clarification = True
        entity_confidence = 0.0

    field_candidates = ontology.resolve_field(entity, text) if entity else []
    top_field: str | None = None
    field_clarification = False
    field_confidence = 1.0
    if field_candidates:
        field_ambiguity = evaluate_ambiguity(field_candidates)
        field_clarification = field_ambiguity.clarification_required
        field_confidence = field_ambiguity.top_confidence
        if field_ambiguity.confident:
            top_field = field_candidates[0].value

    extraction: ExtractionResult = (
        extract_conditions(text, entity, ontology, top_field) if entity else ExtractionResult()
    )

    geographic_context = None
    for condition in extraction.conditions:
        if condition.field == "country":
            geographic_context = {"country": condition.value}
            break

    clarification_required = entity_clarification or field_clarification
    overall_confidence = round(min(intent_confidence, entity_confidence, field_confidence), 4)

    context = QueryContext(
        session_id=session_id,
        intent=intent,
        entity=entity,
        fields=[top_field] if top_field else [],
        conditions=extraction.conditions,
        order_by=extraction.order_by,
        limit=extraction.limit,
        geographic_context=geographic_context,
        confidence=overall_confidence,
        complete=entity is not None and not clarification_required,
        clarification_required=clarification_required,
    )
    return InterpretationResult(
        context=context,
        entity_candidates=tuple(entity_candidates),
        field_candidates=tuple(field_candidates),
    )
