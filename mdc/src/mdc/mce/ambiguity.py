"""Clarification engine (CLAUDE.md section 24).

`mce/confidence.py` decides *whether* clarification is needed. This
module turns a set of tied/low-confidence `Candidate`s into a
human-readable multiple-choice question, and parses the user's answer
back into a resolved field/entity value - either a numeric choice
("3") or the value's own name/alias ("settlement balance"), per
section 24's requirement that both forms work. Presentation (how the
question is printed in the CLI) lives in `mdc.cql.dialogue`; this
module is pure text-in/value-out logic.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from mdc.ontology.ontology import Candidate

# An answer typo ("settlment balance") should still resolve - calibrated
# looser than the ontology's own FUZZY_THRESHOLD (0.88) because answers
# are short and users are actively trying to match one of the options
# just read to them, not writing free text that happens to collide.
ANSWER_FUZZY_THRESHOLD = 0.75


@dataclass(frozen=True)
class ClarificationOption:
    index: int
    value: str
    label: str


@dataclass(frozen=True)
class ClarificationQuestion:
    subject: str
    options: tuple[ClarificationOption, ...]


def _label(value: str) -> str:
    return value.replace("_", " ").capitalize()


def _subject(candidates: list[Candidate]) -> str:
    aliases = {c.matched_alias for c in candidates}
    if len(aliases) == 1:
        return next(iter(aliases))
    return max(candidates, key=lambda c: c.confidence).matched_alias


def build_question(candidates: list[Candidate]) -> ClarificationQuestion:
    options: list[ClarificationOption] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.value in seen:
            continue
        seen.add(candidate.value)
        options.append(ClarificationOption(index=len(options) + 1, value=candidate.value, label=_label(candidate.value)))
    return ClarificationQuestion(subject=_subject(candidates), options=tuple(options))


def resolve_answer(question: ClarificationQuestion, answer: str) -> str | None:
    text = answer.strip()
    if not text:
        return None

    if text.isdigit():
        chosen = int(text)
        for option in question.options:
            if option.index == chosen:
                return option.value
        return None

    normalized = text.lower()
    for option in question.options:
        if normalized == option.value.lower() or normalized == option.label.lower():
            return option.value

    best_value: str | None = None
    best_ratio = 0.0
    for option in question.options:
        ratio = difflib.SequenceMatcher(None, normalized, option.label.lower()).ratio()
        if ratio > best_ratio:
            best_ratio, best_value = ratio, option.value
    return best_value if best_ratio >= ANSWER_FUZZY_THRESHOLD else None
