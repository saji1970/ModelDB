"""Confidence and ambiguity math (CLAUDE.md sections 22-23).

This is the pure scoring function the ambiguity rule reduces to: take
the top two candidate confidences, require the winner to clear an
absolute threshold *and* lead the runner-up by a minimum margin. Both
values are configurable. The interactive "ask the user which one they
meant" clarification engine (section 24) and multi-turn conversational
state (sections 25-27) that *use* this signal are Phase 3 work; this
module only decides whether clarification is needed, not how to ask.
"""

from __future__ import annotations

from dataclasses import dataclass

from mdc.ontology.ontology import Candidate

READ_QUERY_THRESHOLD = 0.90
HIGH_RISK_THRESHOLD = 0.99
AMBIGUITY_MARGIN = 0.15


@dataclass(frozen=True)
class AmbiguityResult:
    top_confidence: float
    margin: float
    confident: bool
    clarification_required: bool
    candidates: tuple[Candidate, ...]


def evaluate_ambiguity(
    candidates: list[Candidate],
    threshold: float = READ_QUERY_THRESHOLD,
    ambiguity_margin: float = AMBIGUITY_MARGIN,
) -> AmbiguityResult:
    if not candidates:
        return AmbiguityResult(
            top_confidence=0.0, margin=0.0, confident=False,
            clarification_required=True, candidates=(),
        )

    ranked = sorted(candidates, key=lambda c: c.confidence, reverse=True)
    p1 = ranked[0].confidence
    p2 = ranked[1].confidence if len(ranked) > 1 else 0.0
    margin = p1 - p2
    confident = p1 >= threshold and margin >= ambiguity_margin

    return AmbiguityResult(
        top_confidence=p1,
        margin=margin,
        confident=confident,
        clarification_required=not confident,
        candidates=tuple(ranked),
    )
