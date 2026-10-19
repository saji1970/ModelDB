"""Chat prompt -> a proposed database design.

`parse_design_prompt` is a deterministic grammar, not a language model:

    create database shop with customers(name, email, joined),
        orders(customer_id, total decimal, paid boolean)

A field may carry a type (`total decimal`, `total: decimal`, plus
`required`); when it doesn't, the type is *guessed from the field's name*
(`..._id`/`email` -> string, `total`/`price` -> decimal, `age`/`count` ->
integer, `is_...`/`active` -> boolean, `..._at`/`date`/`joined` ->
datetime) and the proposal says which ones were guessed.

Anything it returns is only a proposal: the chat shows it and asks
before creating anything (CLAUDE.md section 19). Free-form English ("I
need a database for a school") is deliberately not handled here - that
needs a language model, and `SchemaDesigner` below is where one plugs in
- its output is just another `ImportPlan`, validated and confirmed by
exactly the same steps as this grammar's.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from mdc.portability.plan import ColumnSpec, ImportPlan, TableSpec, safe_identifier, sql_type_to_datatype

_LEAD = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+|i\s+(?:want|need)\s+(?:you\s+)?to\s+)*"
    r"(?:create|build|design|make|set\s*up|generate|define|add)\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_TABLE_SPEC = re.compile(r"([A-Za-z_][\w-]*)\s*\(([^()]*)\)")
_DB_AFTER = re.compile(r"\bdatabase\s+(?:called\s+|named\s+)?([A-Za-z][A-Za-z0-9_-]*)", re.IGNORECASE)
_DB_BEFORE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]*)\s+database\b", re.IGNORECASE)
_NOT_A_DB_NAME = {"a", "an", "the", "new", "my", "our", "this", "that", "with", "for", "called", "named", "and", "of", "in", "tables", "table", "schema", "empty"}

_EXPLICIT_TYPES = {"string", "text", "varchar", "char", "decimal", "number", "float", "double", "money", "integer", "int", "bigint",
                   "boolean", "bool", "datetime", "date", "timestamp", "time"}

_DECIMAL_HINTS = ("price", "amount", "total", "balance", "cost", "rate", "salary", "fee", "revenue", "score", "weight", "height", "percent", "tax", "discount", "credit")
_INTEGER_HINTS = ("count", "qty", "quantity", "age", "year", "number", "stock", "level", "rank", "size", "points", "pages", "duration")
_BOOLEAN_NAMES = {"active", "enabled", "verified", "paid", "deleted", "archived", "published", "approved", "completed", "done", "available"}
_DATETIME_NAMES = {"date", "time", "timestamp", "created", "updated", "joined", "since", "born", "dob", "birthday", "deadline", "due"}


def guess_type(field_name: str) -> str:
    name = field_name.lower()
    if name == "id" or name.endswith("_id"):
        return "string"
    if name.startswith(("is_", "has_", "can_")) or name in _BOOLEAN_NAMES:
        return "boolean"
    if name.endswith(("_at", "_date", "_on")) or name in _DATETIME_NAMES:
        return "datetime"
    if any(hint in name for hint in _DECIMAL_HINTS):
        return "decimal"
    if any(name == hint or name.endswith("_" + hint) for hint in _INTEGER_HINTS):
        return "integer"
    return "string"


def _parse_field(segment: str, table: str, guessed: list[str]) -> ColumnSpec | None:
    words = re.split(r"[\s:]+", segment.strip().strip(":"))
    words = [w for w in words if w]
    if not words:
        return None
    name, _ = safe_identifier(words[0])
    rest = [w.lower() for w in words[1:]]
    required = any(w in ("required", "mandatory") for w in rest) or ("not" in rest and "null" in rest)
    declared = next((w for w in rest if w in _EXPLICIT_TYPES), None)
    if declared is not None:
        datatype = sql_type_to_datatype(declared)
    else:
        datatype = guess_type(name)
        if datatype != "string":  # an untyped field is a string by default - only the other guesses are worth flagging
            guessed.append(f"{table}.{name} -> {datatype}")
    return ColumnSpec(name=name, datatype=datatype, required=required)


def parse_design_prompt(text: str) -> ImportPlan | None:
    """The proposal a prompt describes, or None if it isn't a design prompt
    (no `table(field, ...)` groups) - so it never claims ordinary chat."""
    lead = _LEAD.match(text.strip())
    if lead is None:
        return None
    body = lead.group(1)
    # "add a merchant called Foo (Bar)" has parentheses too; a design always says what it designs.
    if not re.search(r"\b(?:database|schema|tables?|data\s+model)\b", body, re.IGNORECASE):
        return None
    specs = _TABLE_SPEC.findall(body)
    if not specs:
        return None

    guessed: list[str] = []
    tables: list[TableSpec] = []
    seen: set[str] = set()
    for raw_table, raw_fields in specs:
        table, _ = safe_identifier(raw_table)
        if table in seen:
            continue
        seen.add(table)
        columns: list[ColumnSpec] = []
        names: set[str] = set()
        for segment in raw_fields.split(","):
            column = _parse_field(segment, table, guessed)
            if column is not None and column.name not in names and column.name != "record_id":
                names.add(column.name)
                columns.append(column)
        if columns:
            tables.append(TableSpec(name=table, columns=columns))
    if not tables:
        return None

    database = None
    outside = _TABLE_SPEC.sub(" ", body)
    for pattern in (_DB_AFTER, _DB_BEFORE):
        match = pattern.search(outside)
        if match and match.group(1).lower() not in _NOT_A_DB_NAME:
            database = match.group(1)
            break
    warnings = [f"Guessed from the field names: {', '.join(guessed)}. Fields with no type are strings; say the type (e.g. 'total decimal') to change one."] if guessed else []
    return ImportPlan(tables=tables, database=database, warnings=warnings, target="file" if database else "session")


class SchemaDesigner(ABC):
    """Extension point for a language model that turns a free-form request
    into a design. It only *proposes* an `ImportPlan`; the proposal is then
    validated, previewed and confirmed exactly like one from the grammar
    above, and no model output ever reaches storage directly."""

    @abstractmethod
    def propose(self, prompt: str) -> ImportPlan | None: ...
