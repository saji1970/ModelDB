"""Phase 2: pure ambiguity math (CLAUDE.md sections 22-23)."""

from mdc.mce.confidence import AMBIGUITY_MARGIN, READ_QUERY_THRESHOLD, evaluate_ambiguity
from mdc.ontology.ontology import Candidate


def test_single_high_confidence_candidate_is_confident():
    result = evaluate_ambiguity([Candidate(value="settlement_balance", confidence=1.0, matched_alias="settlement balance")])
    assert result.confident is True
    assert result.clarification_required is False
    assert result.margin == 1.0


def test_tied_candidates_require_clarification():
    candidates = [
        Candidate(value="ledger_balance", confidence=1.0, matched_alias="balance"),
        Candidate(value="available_balance", confidence=1.0, matched_alias="balance"),
        Candidate(value="settlement_balance", confidence=1.0, matched_alias="balance"),
    ]
    result = evaluate_ambiguity(candidates)
    assert result.margin == 0.0
    assert result.confident is False
    assert result.clarification_required is True


def test_margin_just_below_threshold_requires_clarification():
    candidates = [
        Candidate(value="a", confidence=READ_QUERY_THRESHOLD + 0.05, matched_alias="a"),
        Candidate(value="b", confidence=READ_QUERY_THRESHOLD + 0.05 - (AMBIGUITY_MARGIN - 0.01), matched_alias="b"),
    ]
    result = evaluate_ambiguity(candidates)
    assert result.confident is False


def test_no_candidates_requires_clarification():
    result = evaluate_ambiguity([])
    assert result.clarification_required is True
    assert result.top_confidence == 0.0


def test_high_confidence_with_sufficient_margin_is_confident():
    candidates = [
        Candidate(value="a", confidence=0.97, matched_alias="a"),
        Candidate(value="b", confidence=0.5, matched_alias="b"),
    ]
    result = evaluate_ambiguity(candidates)
    assert result.confident is True
