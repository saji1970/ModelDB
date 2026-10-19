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
    EXPORT = "EXPORT"
    TRANSFER_HELP = "TRANSFER_HELP"
    DESIGN_SCHEMA = "DESIGN_SCHEMA"
    STREAM_LIST = "STREAM_LIST"
    STREAM_CREATE = "STREAM_CREATE"
    STREAM_UPDATE = "STREAM_UPDATE"
    STREAM_SHOW = "STREAM_SHOW"
    STREAM_HISTORY = "STREAM_HISTORY"


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
    export_format: str | None = None
    export_scope: str | None = None
    design_plan: Any = None  # an ImportPlan proposed by a prompt / ER diagram
    design_error: str | None = None
    stream_id: str | None = None
    stream_values: dict[str, Any] = field(default_factory=dict)  # create: the starting state; update: fields to set
    stream_unset: tuple[str, ...] = ()  # update: fields to remove
    stream_at: str | None = None  # show: the "as of" time, as typed
    stream_seq: int | None = None  # show: "after event N"
    stream_error: str | None = None  # a problem found while parsing (bad JSON, ...)
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


_EXPORT_FORMAT_WORDS = {
    "csv": "csv", "sql": "sql", "script": "sql", "xlsx": "xlsx", "xls": "xlsx", "excel": "xlsx", "spreadsheet": "xlsx",
    "md": "md", "markdown": "md", "er": "er", "erd": "er", "diagram": "er", "mermaid": "er",
    "json": "dnadb", "dnadb": "dnadb", "dump": "dnadb", "backup": "dnadb",
}
_STRUCTURE_WORDS = {"schema", "structure", "design", "ddl"}
_EVERYTHING_WORDS = {"everything", "all", "full", "complete", "backup", "dump"}
_NOT_A_NAME = (
    {"as", "to", "in", "with", "and", "only", "the", "a", "an", "of", "for", "into", "my", "me", "please", "file", "document",
     "doc", "data", "rows", "records", "table", "database", "db", "export", "download"}
    | set(_EXPORT_FORMAT_WORDS) | _STRUCTURE_WORDS | _EVERYTHING_WORDS
)


def _name_after(tokens: list[str], lowered: list[str], *keywords: str) -> str | None:
    for i, word in enumerate(lowered[:-1]):
        if word in keywords and lowered[i + 1] not in _NOT_A_NAME:
            return tokens[i + 1]
    return None


def _export(m: re.Match, raw: str) -> DatabaseCommand | None:
    """`export` / `download` plus any of: a format (csv, sql, excel, markdown,
    er diagram, ...), a scope (schema/structure, data, everything), `table
    <name>`, `database <name>`. Needs at least one of those so a stray
    "download" in some other sentence is never taken for an export."""
    tokens = re.findall(r"[A-Za-z0-9_.-]+", m.group(1))
    lowered = [t.lower() for t in tokens]
    fmt = next((_EXPORT_FORMAT_WORDS[w] for w in lowered if w in _EXPORT_FORMAT_WORDS), None)
    if fmt is None and "design" in lowered:
        fmt = "md"
    structure = any(w in _STRUCTURE_WORDS for w in lowered)
    data = any(w in ("data", "rows", "records") for w in lowered)
    everything = any(w in _EVERYTHING_WORDS for w in lowered)
    # "export the database WITH data" means the whole thing, structure included.
    everything = everything or any(a == "with" and b in ("data", "rows", "records") for a, b in zip(lowered, lowered[1:]))
    scope = "all" if (structure and data) or everything else "schema" if structure else "data" if data else None
    table = _name_after(tokens, lowered, "table", "collection")
    database = _name_after(tokens, lowered, "database", "db")
    if not (fmt or scope or table or database or "database" in lowered or "table" in lowered):
        return None
    return DatabaseCommand(
        DatabaseIntent.EXPORT, database_name=database, table_name=table, export_format=fmt, export_scope=scope, raw_text=raw
    )


def _design(m: re.Match, raw: str) -> DatabaseCommand | None:
    from mdc.portability.design import parse_design_prompt

    plan = parse_design_prompt(raw)
    return None if plan is None else DatabaseCommand(DatabaseIntent.DESIGN_SCHEMA, design_plan=plan, raw_text=raw)


_ER_START = re.compile(r"^[ \t]*erDiagram\b", re.IGNORECASE | re.MULTILINE)


def _design_from_diagram(text: str) -> DatabaseCommand | None:
    """A message that contains a Mermaid `erDiagram` is a schema to build."""
    from mdc.portability.plan import ImportFormatError
    from mdc.portability.readers import read_mermaid

    start = _ER_START.search(text)
    if start is None:
        return None
    named = re.search(rf"\bdatabase\s+(?:called\s+|named\s+)?({_NAME_TOKEN})", text[: start.start()], re.IGNORECASE)
    diagram = text[start.start():].split("```")[0]
    try:
        plan = read_mermaid(diagram, target="file" if named else "session")
    except ImportFormatError as exc:
        return DatabaseCommand(DatabaseIntent.DESIGN_SCHEMA, design_error=str(exc), raw_text=text)
    if named:
        plan.database = named.group(1)
    return DatabaseCommand(DatabaseIntent.DESIGN_SCHEMA, design_plan=plan, raw_text=text)


_STREAM_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"


def _split_outside_quotes(text: str) -> list[str]:
    """Split on commas that are not inside quotes, brackets or braces, so a
    value like ["a", "b"] or "Smith, Jo" stays in one piece."""
    parts, current, depth, quote = [], [], 0, ""
    for ch in text:
        if quote:
            current.append(ch)
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch in "[{":
            depth += 1
            current.append(ch)
        elif ch in "]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _loose_value(text: str) -> Any:
    """`36` -> 36, `true` -> True, `null` -> None, `"calm"` -> "calm", `[1, 2]` -> a
    list; anything that is not JSON is taken as plain text."""
    import json

    try:
        return json.loads(text)
    except ValueError:
        return text.strip().strip("\"'")


def _stream_values(text: str) -> tuple[dict[str, Any], str | None]:
    """`{"mood": "calm"}` or `mood=calm, level=3` -> a dict (or an error message)."""
    import json

    text = text.strip()
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except ValueError as exc:
            return {}, f"That is not valid JSON ({exc})."
        return (value, None) if isinstance(value, dict) else ({}, "The state has to be a JSON object, like {\"mood\": \"calm\"}.")
    values: dict[str, Any] = {}
    for segment in _split_outside_quotes(text):
        key, equals, raw = segment.partition("=")
        key = key.strip()
        if not equals or not re.fullmatch(r"[A-Za-z_][\w.-]*", key):
            return {}, f"I expected field=value pairs (like mood=calm, level=3), but got {segment!r}."
        values[key] = _loose_value(raw)
    return (values, None) if values else ({}, "I found no fields to record.")


def _stream_list(m: re.Match, raw: str) -> DatabaseCommand | None:
    return DatabaseCommand(DatabaseIntent.STREAM_LIST, raw_text=raw)


def _stream_history(m: re.Match, raw: str) -> DatabaseCommand | None:
    return DatabaseCommand(DatabaseIntent.STREAM_HISTORY, stream_id=m.group(1) or m.group(2), raw_text=raw)


def _stream_show(m: re.Match, raw: str) -> DatabaseCommand | None:
    when = (m.group(2) or "").strip()
    event = re.fullmatch(r"(?:event\s+)?#?(\d+)", when, re.IGNORECASE) if when.lower().startswith("event") else None
    return DatabaseCommand(
        DatabaseIntent.STREAM_SHOW, stream_id=m.group(1), stream_at=None if event or not when else when,
        stream_seq=int(event.group(1)) if event else None, raw_text=raw,
    )


def _stream_create(m: re.Match, raw: str) -> DatabaseCommand | None:
    values, error = _stream_values(m.group(2))
    return DatabaseCommand(DatabaseIntent.STREAM_CREATE, stream_id=m.group(1), stream_values=values, stream_error=error, raw_text=raw)


def _stream_update(m: re.Match, raw: str) -> DatabaseCommand | None:
    rest = m.group(2).strip()
    removal = re.match(r"^(?:remove|unset|delete|clear)\s+(.+)$", rest, re.IGNORECASE)
    if removal:
        keys = tuple(k for k in re.split(r"\s*(?:,|\band\b)\s*", removal.group(1).strip()) if k)
        if not keys or any(not re.fullmatch(r"[A-Za-z_][\w.-]*", k) for k in keys):
            return DatabaseCommand(DatabaseIntent.STREAM_UPDATE, stream_id=m.group(1), stream_error="Say which fields to remove, e.g. remove goal, mood.", raw_text=raw)
        return DatabaseCommand(DatabaseIntent.STREAM_UPDATE, stream_id=m.group(1), stream_unset=keys, raw_text=raw)
    values, error = _stream_values(re.sub(r"^(?:set|with)\s+", "", rest, flags=re.IGNORECASE))
    return DatabaseCommand(DatabaseIntent.STREAM_UPDATE, stream_id=m.group(1), stream_values=values, stream_error=error, raw_text=raw)


def _transfer_help(m: re.Match, raw: str) -> DatabaseCommand | None:
    return DatabaseCommand(DatabaseIntent.TRANSFER_HELP, raw_text=raw)


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
    (re.compile(r"^(?:list|show)\s+(?:all\s+)?streams\s*$", re.IGNORECASE), _stream_list),
    (
        re.compile(rf"^(?:(?:show|get|read|list)\s+)?(?:the\s+)?history\s+of\s+stream\s+({_STREAM_ID})\s*$|^(?:show|get)\s+stream\s+({_STREAM_ID})\s+history\s*$", re.IGNORECASE),
        _stream_history,
    ),
    (re.compile(rf"^(?:show|get|read|fetch|rebuild)\s+(?:the\s+)?(?:state\s+of\s+)?stream\s+({_STREAM_ID})(?:\s+(?:as\s+of|at|after)\s+(.+?))?\s*$", re.IGNORECASE), _stream_show),
    (re.compile(rf"^create\s+(?:a\s+|new\s+)?stream\s+({_STREAM_ID})\s+(?:with|as|from)\s+(.+)$", re.IGNORECASE | re.DOTALL), _stream_create),
    (re.compile(rf"^(?:update|change|record)\s+stream\s+({_STREAM_ID})\s+(.+)$", re.IGNORECASE | re.DOTALL), _stream_update),
    (re.compile(r"^(?:export|download)\s*$|^import(?:\s+.*)?$", re.IGNORECASE), _transfer_help),
    (re.compile(r"^(?:please\s+)?(?:export|download)\s+(.+)$", re.IGNORECASE), _export),
    # Last, so every specific pattern above wins: "create database <name>" and
    # "create table <name> with ..." never reach the design grammar.
    (re.compile(r"^.+$", re.DOTALL), _design),
]


def parse_database_command(text: str) -> DatabaseCommand | None:
    stripped = text.strip()
    if not stripped:
        return None
    diagram = _design_from_diagram(stripped)
    if diagram is not None:
        return diagram
    for pattern, builder in _PATTERNS:
        match = pattern.match(stripped)
        if not match:
            continue
        command = builder(match, stripped)
        if command is not None:
            return command
    return None
