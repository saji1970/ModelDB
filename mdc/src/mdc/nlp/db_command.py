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


@dataclass(frozen=True)
class DatabaseCommand:
    intent: str
    database_name: str | None = None
    table_name: str | None = None
    fields: dict[str, FieldSchema] = field(default_factory=dict)
    values: dict[str, str] = field(default_factory=dict)
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
    return DatabaseCommand(DatabaseIntent.DESCRIBE_TABLE, table_name=m.group(1), raw_text=raw)


def _show_data(m: re.Match, raw: str) -> DatabaseCommand | None:
    return DatabaseCommand(DatabaseIntent.SHOW_DATA, table_name=m.group(1), raw_text=raw)


def _insert(m: re.Match, raw: str) -> DatabaseCommand | None:
    values = _parse_kv_list(m.group(2))
    if values is None:
        return None
    return DatabaseCommand(DatabaseIntent.INSERT, table_name=m.group(1), values=values, raw_text=raw)


_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(rf"^create\s+(?:a\s+|new\s+)?database\s+(?:named\s+|called\s+)?({_NAME_TOKEN})\s*$", re.IGNORECASE), _create_database),
    (re.compile(rf"^(?:use|switch(?:\s+to)?)\s+database\s+({_NAME_TOKEN})\s*$", re.IGNORECASE), _use_database),
    (re.compile(r"^(?:list|show)\s+databases\s*$", re.IGNORECASE), _list_databases),
    (re.compile(r"^(?:describe\s+database|show\s+database\s+structure|list\s+tables|show\s+tables)\s*$", re.IGNORECASE), _describe_database),
    (re.compile(rf"^create\s+(?:a\s+|new\s+)?table\s+({_NAME_TOKEN})\s+with\s+(.+)$", re.IGNORECASE), _create_table),
    (re.compile(rf"^describe\s+(?:table|collection)\s+({_NAME_TOKEN})\s*$", re.IGNORECASE), _describe_table),
    (re.compile(rf"^(?:show\s+data\s+in|show\s+rows\s+in|query)\s+({_NAME_TOKEN})\s*$", re.IGNORECASE), _show_data),
    (re.compile(rf"^insert\s+into\s+({_NAME_TOKEN})\s+(.+)$", re.IGNORECASE), _insert),
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
