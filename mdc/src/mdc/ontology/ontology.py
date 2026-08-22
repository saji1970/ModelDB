"""Deterministic ontology resolution (CLAUDE.md sections 14-15, 38).

`Ontology.resolve_field` / `resolve_entity` implement the semantic rule
in section 15 structurally rather than as a special case: matches are
found by scanning for every alias phrase in the text, longest first. A
longer, more specific alias (e.g. "settlement balance") suppresses a
shorter alias for a *different* field that overlaps the same text span
(e.g. "balance" as a candidate for ledger_balance). But when two
different fields register the exact same alias text at the exact same
span - which is exactly what happens when the input is just "balance" -
neither suppresses the other, so both survive as tied candidates. That
tie (equal confidence, zero margin) is precisely what section 23's
ambiguity check is built to catch.

A fuzzy fallback (difflib) handles simple typos (section 38) when no
exact alias matches at all, at a reduced confidence.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

# Calibrated against real typos vs. coincidental word similarity:
# genuine one-edit typos ("balnce"/"balance", "merchnt type"/"merchant
# type") score 0.92-0.97; unrelated words that merely share a stem or
# prefix ("count"/"country" 0.83, "merchants in"/"merchant name" 0.80)
# top out around 0.83. 0.88 sits cleanly in the gap between them.
FUZZY_THRESHOLD = 0.88
TYPO_CONFIDENCE_PENALTY = 0.9


@dataclass(frozen=True)
class FieldDefinition:
    name: str
    aliases: tuple[str, ...]
    datatype: str
    source_table: str


@dataclass(frozen=True)
class EntityDefinition:
    name: str
    aliases: tuple[str, ...]
    fields: dict[str, FieldDefinition] = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    value: str
    confidence: float
    matched_alias: str
    is_typo: bool = False


def _find_alias_spans(text: str, owner_alias_pairs: list[tuple[str, str]]) -> list[tuple[str, str, int, int]]:
    """Return (owner, alias, start, end) for every word-boundary alias match."""
    matches: list[tuple[str, str, int, int]] = []
    for owner, alias in owner_alias_pairs:
        pattern = r"\b" + re.escape(alias) + r"\b"
        for m in re.finditer(pattern, text):
            matches.append((owner, alias, m.start(), m.end()))
    return matches


def _resolve_exact(text: str, owner_alias_pairs: list[tuple[str, str]]) -> list[Candidate]:
    matches = _find_alias_spans(text, owner_alias_pairs)
    return _accept_and_group(matches)


def _accept_and_group(matches: list[tuple[str, str, int, int]]) -> list[Candidate]:
    if not matches:
        return []
    matches = list(matches)

    # Longest phrase first, so more specific aliases get first refusal.
    matches.sort(key=lambda m: m[3] - m[2], reverse=True)

    accepted: list[tuple[str, str, int, int]] = []
    for owner, alias, start, end in matches:
        span_len = end - start
        suppressed = False
        for acc_owner, _, acc_start, acc_end in accepted:
            if acc_owner == owner:
                continue
            acc_len = acc_end - acc_start
            contains = acc_start <= start and acc_end >= end
            if contains and acc_len > span_len:
                suppressed = True
                break
        if suppressed:
            continue
        # Skip an exact duplicate (same owner, same span) from a second alias.
        if any(o == owner and s == start and e == end for o, _, s, e in accepted):
            continue
        accepted.append((owner, alias, start, end))

    by_owner: dict[str, tuple[str, int, int]] = {}
    for owner, alias, start, end in accepted:
        # Prefer the longest matched alias per owner if it matched more than once.
        current = by_owner.get(owner)
        if current is None or (end - start) > (current[2] - current[1]):
            by_owner[owner] = (alias, start, end)

    # Order by where each owner's match starts in the text (leftmost first).
    # Confidence is 1.0 for every exact match, so this is what breaks ties
    # deterministically and sensibly: e.g. in "merchants with settlement
    # balance", "merchant" (the entity/subject) should outrank "settlement"
    # (only present because it's embedded in a field phrase), and it is
    # mentioned first.
    ordered = sorted(by_owner.items(), key=lambda item: item[1][1])
    return [Candidate(value=owner, confidence=1.0, matched_alias=alias) for owner, (alias, _, _) in ordered]


def _resolve_fuzzy(text: str, owner_alias_pairs: list[tuple[str, str]]) -> list[Candidate]:
    words = text.split()

    best_per_owner: dict[str, Candidate] = {}
    for owner, alias in owner_alias_pairs:
        # Compare against n-grams of the same word count as the alias only.
        # Comparing e.g. the single word "merchants" against the two-word
        # alias "merchant name" scores deceptively high on shared stems
        # ("merchant...") even though it isn't a typo of that phrase at all.
        alias_word_count = len(alias.split())
        if alias_word_count > len(words):
            continue
        best_ratio = 0.0
        for i in range(len(words) - alias_word_count + 1):
            ngram = " ".join(words[i : i + alias_word_count])
            ratio = difflib.SequenceMatcher(None, ngram, alias).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
        if best_ratio >= FUZZY_THRESHOLD:
            confidence = round(best_ratio * TYPO_CONFIDENCE_PENALTY, 4)
            existing = best_per_owner.get(owner)
            if existing is None or confidence > existing.confidence:
                best_per_owner[owner] = Candidate(
                    value=owner, confidence=confidence, matched_alias=alias, is_typo=True
                )
    return list(best_per_owner.values())


class Ontology:
    def __init__(self, entities: dict[str, EntityDefinition]):
        self.entities = entities

    def resolve_entity(self, text: str) -> list[Candidate]:
        normalized = text.lower()
        pairs = [(name, alias) for name, defn in self.entities.items() for alias in defn.aliases]

        # Don't let a word that is only present as part of a *field* phrase
        # (e.g. "settlement" inside "settlement balance", a field alias
        # registered under `merchant`) also register as its own entity
        # candidate (the standalone `settlement` entity). Otherwise
        # "merchants with settlement balance" would spuriously tie
        # `merchant` against `settlement` as competing subjects.
        phrase_spans = self._field_phrase_spans(normalized)
        entity_matches = [
            m for m in _find_alias_spans(normalized, pairs)
            if not any(ps <= m[2] and pe >= m[3] and (pe - ps) > (m[3] - m[2]) for ps, pe in phrase_spans)
        ]

        exact = _accept_and_group(entity_matches)
        if exact:
            return sorted(exact, key=lambda c: c.confidence, reverse=True)
        return sorted(_resolve_fuzzy(normalized, pairs), key=lambda c: c.confidence, reverse=True)

    def _field_phrase_spans(self, normalized_text: str) -> list[tuple[int, int]]:
        phrase_pairs = [
            (field_defn.name, alias)
            for entity_defn in self.entities.values()
            for field_defn in entity_defn.fields.values()
            for alias in field_defn.aliases
            if len(alias.split()) > 1
        ]
        return [(start, end) for _, _, start, end in _find_alias_spans(normalized_text, phrase_pairs)]

    def resolve_field(self, entity: str, text: str) -> list[Candidate]:
        defn = self.entities.get(entity)
        if defn is None:
            raise KeyError(f"Unknown entity '{entity}'")
        normalized = text.lower()
        pairs = [(name, alias) for name, field_defn in defn.fields.items() for alias in field_defn.aliases]
        exact = _resolve_exact(normalized, pairs)
        if exact:
            return sorted(exact, key=lambda c: c.confidence, reverse=True)
        return sorted(_resolve_fuzzy(normalized, pairs), key=lambda c: c.confidence, reverse=True)

    def field_definition(self, entity: str, field_name: str) -> FieldDefinition:
        return self.entities[entity].fields[field_name]
