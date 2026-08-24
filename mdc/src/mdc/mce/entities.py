"""Entity/field/condition extraction (CLAUDE.md sections 20-21, 60 partial).

Entity and field resolution is fully deterministic (`Ontology`, see
mdc/ontology/ontology.py) - this module adds the lighter-weight pattern
matching needed to turn "above 10000 USD" / "in India" / "top 20" into
`Condition` / `OrderBy` / `limit` values on top of that. Only the literal
numeric forms the section 57 acceptance tests actually require are
handled; the fuller "10K" / "$10,000" / "10 thousand" normalization in
section 60 is not required by any of those tests and is left for a
later phase (see README "known limitations").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mdc.mce.context import Condition, OrderBy
from mdc.ontology.ontology import Ontology

CURRENCY_CODES = {"USD", "INR", "GBP", "EUR", "SGD", "AUD", "CAD", "JPY", "BRL"}

COUNTRY_NAMES: dict[str, str] = {
    "united states": "US", "usa": "US", "us": "US",
    "united kingdom": "GB", "uk": "GB",
    "india": "IN",
    "germany": "DE",
    "singapore": "SG",
    "australia": "AU",
    "canada": "CA",
    "france": "FR",
    "japan": "JP",
    "brazil": "BR",
}

# Longest phrase first so "greater than or equal to" wins over "greater than".
OPERATOR_PHRASES: list[tuple[str, str]] = sorted(
    [
        ("greater than or equal to", ">="),
        ("less than or equal to", "<="),
        ("at least", ">="),
        ("at most", "<="),
        ("greater than", ">"),
        ("more than", ">"),
        ("less than", "<"),
        ("exceeds", ">"),
        ("above", ">"),
        ("over", ">"),
        ("below", "<"),
        ("under", "<"),
        ("equal to", "="),
        ("equals", "="),
    ],
    key=lambda pair: len(pair[0]),
    reverse=True,
)

_SYMBOL_OPERATORS: list[tuple[str, str]] = [(">=", ">="), ("<=", "<="), (">", ">"), ("<", "<"), ("=", "=")]

NUMBER_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?")
TOP_N_PATTERN = re.compile(r"\btop\s+(\d+)\b")
BY_FIELD_PATTERN = re.compile(r"\bby\s+([a-z][a-z ]*)")


@dataclass
class ExtractionResult:
    conditions: list[Condition] = field(default_factory=list)
    order_by: list[OrderBy] = field(default_factory=list)
    limit: int | None = None


def resolve_entity(text: str, ontology: Ontology) -> list:
    return ontology.resolve_entity(text)


def _extract_amount_condition(normalized: str, field_name: str, datatype: str) -> tuple[Condition | None, str | None]:
    for phrase, operator in OPERATOR_PHRASES:
        match = re.search(r"\b" + re.escape(phrase) + r"\b", normalized)
        if not match:
            continue
        remainder = normalized[match.end():]
        number_match = NUMBER_PATTERN.search(remainder)
        if not number_match:
            continue
        value = float(number_match.group().replace(",", ""))
        tail = remainder[number_match.end(): number_match.end() + 15]
        currency = None
        currency_match = re.search(r"\b([a-z]{3})\b", tail)
        if currency_match and currency_match.group(1).upper() in CURRENCY_CODES:
            currency = currency_match.group(1).upper()
        condition = Condition(
            field=field_name, operator=operator, value=value,
            datatype=datatype, confidence=1.0, source="user_input",
        )
        return condition, currency

    # Symbol operators ("balance > 6000") - checked only after every
    # textual phrase above has failed to match, ">="/"<=" ahead of their
    # single-character prefixes so "balance >= 6000" resolves to ">=" (the
    # ">" pattern can never falsely match it either way: the character
    # right after ">" would be "=", not whitespace/a digit, so its own
    # `\s*\d` requirement already fails).
    for symbol, operator in _SYMBOL_OPERATORS:
        match = re.search(re.escape(symbol) + r"\s*(\d[\d,]*(?:\.\d+)?)", normalized)
        if not match:
            continue
        value = float(match.group(1).replace(",", ""))
        condition = Condition(
            field=field_name, operator=operator, value=value,
            datatype=datatype, confidence=1.0, source="user_input",
        )
        return condition, None

    return None, None


def _extract_country_condition(normalized: str) -> Condition | None:
    for name, code in sorted(COUNTRY_NAMES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if re.search(r"\bin\s+" + re.escape(name) + r"\b", normalized) or re.search(
            r"\b" + re.escape(name) + r"\b", normalized
        ):
            return Condition(
                field="country", operator="=", value=code,
                datatype="string", confidence=1.0, source="user_input",
            )
    return None


def extract_conditions(
    text: str,
    entity: str,
    ontology: Ontology,
    top_field_candidate: str | None,
) -> ExtractionResult:
    normalized = text.lower()
    result = ExtractionResult()

    if top_field_candidate is not None:
        field_defn = ontology.field_definition(entity, top_field_candidate)
        amount_condition, currency = _extract_amount_condition(
            normalized, top_field_candidate, field_defn.datatype
        )
        if amount_condition is not None:
            result.conditions.append(amount_condition)
            if currency is not None:
                result.conditions.append(
                    Condition(
                        field="currency", operator="=", value=currency,
                        datatype="string", confidence=1.0, source="user_input",
                    )
                )

    country_condition = _extract_country_condition(normalized)
    if country_condition is not None:
        result.conditions.append(country_condition)

    top_match = TOP_N_PATTERN.search(normalized)
    if top_match:
        result.limit = int(top_match.group(1))

    order_field = top_field_candidate
    by_match = BY_FIELD_PATTERN.search(normalized)
    if by_match:
        candidates = ontology.resolve_field(entity, by_match.group(1).strip())
        if candidates:
            order_field = candidates[0].value

    wants_sort = top_match is not None or "sort" in normalized or "highest" in normalized or "lowest" in normalized
    if wants_sort and order_field is not None:
        direction = "ASC" if any(w in normalized for w in ("lowest", "ascending", "asc")) else "DESC"
        result.order_by.append(OrderBy(field=order_field, direction=direction))

    return result
