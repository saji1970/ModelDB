"""Natural-language -> DatabaseCommand: create/use/list databases,
create/describe tables (SchemaRegistry collections), and browse/insert
data - all from the chat prompt.

Checked *before* both `nlp.command` (object-storage intents) and the
merchants-CRUD interpreter, since "describe"/"create"/"show" are
generic verbs those two also use - every pattern here requires the
literal word "database"/"table"/"collection" to disambiguate, so a
database-admin command can never be mistaken for either of the other
two, and vice versa (verified against both domains' test phrasing).

Table creation is deliberately schema-registry-only, not raw SQL DDL -
letting free-text chat input drive arbitrary SQL would be a real
injection surface; a typed field list validated by `SchemaRegistry`
has no such risk.

`show/list data in <table>` and `describe table <table>` accept an
optional trailing `in database <name>` qualifier so a session can read
another database's table without first switching `current_database` -
still gated behind the literal word "database" so it can't collide
with the merchants-analytics "Show merchants in India" shape (bare
"<name> in <name>", no disambiguating keyword, is deliberately never
matched here - see the collision-safety note above). Without the
qualifier, both default to whatever `StorageConversationState.
current_database` currently is.

`find <query>` is a universal search: unlike everything else here, its
handler (`conversation.db_interpreter._handle_find`) fans out across
every database and every table `DatabaseManager` knows about (plus
document full-text search), rather than the current database alone.
The trigger word is bare "find" (not "find text ..." like
`nlp.command`'s object-storage search, which this intercepts first and
functionally supersedes - no existing caller depended on the more
specific phrasing). Everything after "find" is parsed left-to-right
for optional clauses - `under`/`below`/`less than <N>[k|m]`,
`over`/`above`/`more than <N>[k|m]`, `in table <name>`, `in database
<name>` - each removed from the text as it's found; whatever plain
words are left over become the free-text search term(s). This is
still deterministic clause-extraction, not fuzzy NLU - see
`_find`/`_split_find_query`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from mdc.schema.registry import FieldSchema

VALID_FIELD_TYPES = {"string", "decimal", "integer", "boolean", "datetime"}
_NAME_TOKEN = r"[A-Za-z][A-Za-z0-9_-]*"


class DatabaseIntent:
    CREATE_DATABASE = "CREATE_DATABASE"
    USE_DATABASE = "USE_DATABASE"
    LIST_DATABASES = "LIST_DATABASES"
    DESCRIBE_DATABASE = "DESCRIBE_DATABASE"
    CREATE_TABLE = "CREATE_TABLE"
    DESCRIBE_TABLE = "DESCRIBE_TABLE"
    SHOW_DATA = "SHOW_DATA"
    INSERT = "INSERT"
    FIND = "FIND"


@dataclass(frozen=True)
class DatabaseCommand:
    intent: str
    database_name: str | None = None
    table_name: str | None = None
    fields: dict[str, FieldSchema] = field(default_factory=dict)
    values: dict[str, str] = field(default_factory=dict)
    search_term: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    raw_text: str = ""


def _parse_field_list(text: str) -> dict[str, FieldSchema] | None:
    fields: dict[str, FieldSchema] = {}
    for segment in text.split(","):
        parts = segment.strip().split()
        if len(parts) != 2:
            return None
        name, type_word = parts[0], parts[1].lower()
        if type_word not in VALID_FIELD_TYPES:
            return None
        fields[name] = FieldSchema(name=name, datatype=type_word, required=False)
    return fields or None


def _parse_kv_list(text: str) -> dict[str, str] | None:
    values: dict[str, str] = {}
    for segment in text.split(","):
        if "=" not in segment:
            return None
        key, _, value = segment.partition("=")
        key = key.strip()
        if not key:
            return None
        values[key] = value.strip()
    return values or None


def _create_database(m: re.Match, raw: str) -> DatabaseCommand | None:
    return DatabaseCommand(DatabaseIntent.CREATE_DATABASE, database_name=m.group(1), raw_text=raw)


def _use_database(m: re.Match, raw: str) -> DatabaseCommand | None:
    return DatabaseCommand(DatabaseIntent.USE_DATABASE, database_name=m.group(1), raw_text=raw)


def _list_databases(m: re.Match, raw: str) -> DatabaseCommand | None:
    return DatabaseCommand(DatabaseIntent.LIST_DATABASES, raw_text=raw)


def _describe_database(m: re.Match, raw: str) -> DatabaseCommand | None:
    return DatabaseCommand(DatabaseIntent.DESCRIBE_DATABASE, raw_text=raw)


def _create_table(m: re.Match, raw: str) -> DatabaseCommand | None:
    fields = _parse_field_list(m.group(2))
    if fields is None:
        return None
    return DatabaseCommand(DatabaseIntent.CREATE_TABLE, table_name=m.group(1), fields=fields, raw_text=raw)


def _describe_table(m: re.Match, raw: str) -> DatabaseCommand | None:
    table_name = m.group(1) or m.group(2)
    return DatabaseCommand(DatabaseIntent.DESCRIBE_TABLE, table_name=table_name, database_name=m.group(3) or m.group(4), raw_text=raw)


def _show_data(m: re.Match, raw: str) -> DatabaseCommand | None:
    return DatabaseCommand(DatabaseIntent.SHOW_DATA, table_name=m.group(1), database_name=m.group(2) or m.group(3), raw_text=raw)


def _insert(m: re.Match, raw: str) -> DatabaseCommand | None:
    values = _parse_kv_list(m.group(2))
    if values is None:
        return None
    return DatabaseCommand(DatabaseIntent.INSERT, table_name=m.group(1), values=values, raw_text=raw)


_AMOUNT_UNITS = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000}
_UNDER_RE = re.compile(r"\b(?:under|below|less\s+than)\s+([\d,]+(?:\.\d+)?)\s*(k|m|thousand|million)?\b", re.IGNORECASE)
_OVER_RE = re.compile(r"\b(?:over|above|more\s+than)\s+([\d,]+(?:\.\d+)?)\s*(k|m|thousand|million)?\b", re.IGNORECASE)
_FIND_IN_TABLE_RE = re.compile(rf"\bin\s+table\s+({_NAME_TOKEN})\b", re.IGNORECASE)
_FIND_IN_DB_RE = re.compile(rf"\bin\s+database\s+({_NAME_TOKEN})\b", re.IGNORECASE)


def _parse_amount(number: str, unit: str | None) -> float:
    value = float(number.replace(",", ""))
    if unit:
        value *= _AMOUNT_UNITS.get(unit.lower(), 1)
    return value


def _extract(pattern: re.Pattern[str], text: str) -> tuple[re.Match[str] | None, str]:
    """Pull the first match of `pattern` out of `text`, returning the
    match (or None) and `text` with that span removed - so later clause
    extractions never trip over words a prior clause already consumed."""
    match = pattern.search(text)
    if match is None:
        return None, text
    return match, text[: match.start()] + text[match.end() :]


def _split_find_query(remainder: str) -> tuple[str | None, float | None, float | None, str | None, str | None]:
    """Deterministic clause-extraction (not fuzzy NLU): pull out amount
    constraints and explicit table/database scoping in a fixed order,
    each removed from the text as it's found, and return whatever plain
    words are left as the free-text search term."""
    remainder = re.sub(r"^text\s+", "", remainder.strip(), flags=re.IGNORECASE)

    max_value = None
    under, remainder = _extract(_UNDER_RE, remainder)
    if under:
        max_value = _parse_amount(under.group(1), under.group(2))

    min_value = None
    over, remainder = _extract(_OVER_RE, remainder)
    if over:
        min_value = _parse_amount(over.group(1), over.group(2))

    table_name = None
    in_table, remainder = _extract(_FIND_IN_TABLE_RE, remainder)
    if in_table:
        table_name = in_table.group(1)

    database_name = None
    in_db, remainder = _extract(_FIND_IN_DB_RE, remainder)
    if in_db:
        database_name = in_db.group(1)

    term = re.sub(r"\s+", " ", remainder).strip() or None
    return term, min_value, max_value, table_name, database_name


def _find(m: re.Match, raw: str) -> DatabaseCommand | None:
    term, min_value, max_value, table_name, database_name = _split_find_query(m.group(1))
    if term is None and min_value is None and max_value is None:
        return None
    return DatabaseCommand(
        DatabaseIntent.FIND,
        table_name=table_name,
        database_name=database_name,
        search_term=term,
        min_value=min_value,
        max_value=max_value,
        raw_text=raw,
    )


# A trailing "in database <name>" / "in <name> database" clause - both
# word orders, since real phrasing goes both ways ("in database default"
# vs. "in the default database") - always gated behind the literal word
# "database" so it stays disambiguated from bare "<name> in <name>"
# (see the collision-safety note above). Two alternate capture groups
# (one per order) - builders pick whichever one is not None.
_DB_QUALIFIER = rf"(?:\s+in\s+(?:the\s+)?(?:database\s+({_NAME_TOKEN})|({_NAME_TOKEN})\s+database))?"

_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(rf"^create\s+(?:a\s+|new\s+)?database\s+(?:named\s+|called\s+)?({_NAME_TOKEN})\s*$", re.IGNORECASE), _create_database),
    (re.compile(rf"^(?:use|switch(?:\s+to)?)\s+database\s+({_NAME_TOKEN})\s*$", re.IGNORECASE), _use_database),
    (re.compile(r"^(?:list|show)\s+databases\s*$", re.IGNORECASE), _list_databases),
    (re.compile(r"^(?:describe\s+database|show\s+database\s+structure|list\s+tables|show\s+tables)\s*$", re.IGNORECASE), _describe_database),
    (re.compile(rf"^create\s+(?:a\s+|new\s+)?table\s+({_NAME_TOKEN})\s+with\s+(.+)$", re.IGNORECASE), _create_table),
    (
        re.compile(
            rf"^describe\s+(?:the\s+)?(?:(?:table|collection)\s+({_NAME_TOKEN})|({_NAME_TOKEN})\s+(?:table|collection)){_DB_QUALIFIER}\s*$",
            re.IGNORECASE,
        ),
        _describe_table,
    ),
    (
        re.compile(
            rf"^(?:(?:show|list)\s+(?:data\s+in|rows\s+in)|query)\s+(?:the\s+)?({_NAME_TOKEN})(?:\s+table)?{_DB_QUALIFIER}\s*$",
            re.IGNORECASE,
        ),
        _show_data,
    ),
    (re.compile(rf"^insert\s+into\s+({_NAME_TOKEN})\s+(.+)$", re.IGNORECASE), _insert),
    (re.compile(r"^(?:i\s+want\s+to\s+|please\s+|can\s+you\s+|could\s+you\s+)*find\s+(.+)$", re.IGNORECASE), _find),
]


def parse_database_command(text: str) -> DatabaseCommand | None:
    stripped = text.strip()
    if not stripped:
        return None
    for pattern, builder in _PATTERNS:
        match = pattern.match(stripped)
        if not match:
            continue
        command = builder(match, stripped)
        if command is not None:
            return command
    return None
