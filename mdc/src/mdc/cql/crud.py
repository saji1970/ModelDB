"""Natural-language CRUD -> `MDCOperation` (CLAUDE.md sections 12, 15).

Deliberately simple, regex-based extraction rather than the ontology
condition-extraction pipeline (`mce/entities.py`): a CRUD sentence has
a different shape - an identifying reference plus an assignment or a
free-text name ("called ABC Store", "ABC Store balance to 15000") -
not a filter expression over known field aliases. Field *names*
("balance", "settlement balance", ...) still resolve through the same
`Ontology`/ambiguity machinery as reads (section 16), so "which
balance?" clarification behaves identically for an UPDATE as for a
FETCH.

Lives in `cql`, not `mce`, because its output is an `MDCOperation` -
the semantic layer's actual product per section 3 - not a
`QueryContext`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mdc.mce.entities import COUNTRY_NAMES, NUMBER_PATTERN
from mdc.model.operation import CreateOperation, DeleteOperation, Filter
from mdc.ontology.ontology import Candidate, Ontology

MERCHANTS_COLLECTION = "merchants"

_NAME_PATTERN = re.compile(r"\b(?:called|named)\s+(.+?)(?=\s+\b(?:in|with|located)\b|[.,]|$)", re.IGNORECASE)
_LEADING_VERB = re.compile(r"^\s*(?:change|update|set)\s+", re.IGNORECASE)
_LEADING_DELETE_VERB = re.compile(r"^\s*(?:delete|remove)\s+", re.IGNORECASE)
_LEADING_MERCHANT_WORD = re.compile(r"^\s*(?:the\s+)?merchants?\s+", re.IGNORECASE)
_TO_VALUE = re.compile(r"\bto\s+", re.IGNORECASE)


def _extract_country(text: str) -> str | None:
    normalized = text.lower()
    for name, code in sorted(COUNTRY_NAMES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if re.search(r"\b" + re.escape(name) + r"\b", normalized):
            return code
    return None


def extract_create_data(text: str) -> dict[str, str]:
    """"Create a merchant called ABC Store in India" -> {"name": "ABC Store", "country": "IN"}."""
    data: dict[str, str] = {}
    match = _NAME_PATTERN.search(text)
    if match:
        data["name"] = match.group(1).strip(" .")
    country = _extract_country(text)
    if country:
        data["country"] = country
    return data


def build_delete_operation(text: str, collection: str = MERCHANTS_COLLECTION) -> DeleteOperation:
    """"Delete merchant ABC Store" / "Delete ABC Store" -> DeleteOperation(filters=[name = "ABC Store"])."""
    remainder = _LEADING_DELETE_VERB.sub("", text)
    remainder = _LEADING_MERCHANT_WORD.sub("", remainder)
    name = remainder.strip(" .")
    return DeleteOperation(collection=collection, filters=[Filter(field="name", operator="=", value=name)])


@dataclass(frozen=True)
class UpdateRequest:
    name: str
    value: float | str


def extract_update_request(text: str, field_alias: str, alias_start: int) -> UpdateRequest:
    """Given the resolved field's alias and where it starts in `text`, split
    the sentence into the identifying name (before the alias) and the new
    value (the number after "to", following the alias)."""
    name_part = text[:alias_start]
    name_part = _LEADING_VERB.sub("", name_part)
    name = name_part.strip(" .")

    remainder = text[alias_start + len(field_alias):]
    to_match = _TO_VALUE.search(remainder)
    tail = remainder[to_match.end():] if to_match else remainder
    number_match = NUMBER_PATTERN.search(tail)
    value: float | str = float(number_match.group().replace(",", "")) if number_match else tail.strip(" .")
    return UpdateRequest(name=name, value=value)


def locate_alias(text: str, candidate: Candidate) -> int:
    """Find where `candidate.matched_alias` starts in `text` (case-insensitive)."""
    idx = text.lower().find(candidate.matched_alias.lower())
    return idx if idx >= 0 else 0
