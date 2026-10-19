"""What a parsed import file means, before anything is written.

Every reader (CSV, SQL, Markdown, MDC export file) produces the same
`ImportPlan`, and `apply.prepare_import` validates and applies it - so
the rules about conflicts, types, permissions and atomicity live in one
place instead of once per file format.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DATATYPES = ("string", "decimal", "integer", "boolean", "datetime")
RECORD_ID = "record_id"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PortabilityError(Exception):
    """Base class for import/export errors."""


class ImportFormatError(PortabilityError):
    """The file is not a valid file of the format it claims to be (HTTP 422)."""


class ImportDataError(PortabilityError):
    """The file parses, but some values do not fit the declared types (HTTP 422).
    `problems` lists them - nothing is written when there are any."""

    def __init__(self, problems: list[str]):
        shown = problems[:20]
        more = f" (and {len(problems) - 20} more)" if len(problems) > 20 else ""
        super().__init__("; ".join(shown) + more)
        self.problems = problems


class ImportConflictError(PortabilityError):
    """The import would overwrite or duplicate something that already exists (HTTP 409)."""

    def __init__(self, conflicts: list[str]):
        super().__init__("; ".join(conflicts))
        self.conflicts = conflicts


class ExportOptionError(PortabilityError):
    """An unknown or unsupported export format/scope combination (HTTP 422)."""


@dataclass
class ColumnSpec:
    name: str
    datatype: str = "string"
    required: bool = False


@dataclass
class RowSpec:
    values: dict[str, Any]
    where: str  # "row 5" / "INSERT #3, row 2" - for error messages
    record_id: str | None = None


@dataclass
class TableSpec:
    name: str
    # None means the file carries rows for this table but not its structure
    # (an MDC data-only export, an INSERT into a table the script never
    # creates): the table must already exist where it is imported.
    columns: list[ColumnSpec] | None = None
    rows: list[RowSpec] = field(default_factory=list)
    if_not_exists: bool = False


@dataclass
class ImportPlan:
    tables: list[TableSpec]
    database: str | None = None  # the name the file itself declares, if any
    warnings: list[str] = field(default_factory=list)
    # How a database name is chosen when the caller gives none: "file" =
    # the file's own name for itself (or its filename) - a design doc / SQL
    # script / export file *is* a database; "session" = a bare CSV is one
    # table, which belongs in whatever database the caller is working in.
    target: str = "file"


def safe_identifier(raw: str, used: set[str] | None = None) -> tuple[str, bool]:
    """Turn a header/identifier from a file into a name MDC accepts.
    Returns (name, changed). With `used`, also de-duplicates (`name_2`)."""
    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(raw).strip()).strip("_").lower()
    if not text:
        text = "column"
    if text[0].isdigit():
        text = "_" + text
    name = text
    if used is not None:
        n = 2
        while name in used:
            name = f"{text}_{n}"
            n += 1
        used.add(name)
    return name, name != str(raw).strip().lower()  # lowercasing alone is not worth a warning


def is_valid_identifier(name: str) -> bool:
    return bool(_IDENT_RE.match(name))


def sql_type_to_datatype(type_text: str) -> str:
    """Map an SQL (or hand-written) column type to MDC's five datatypes.
    Anything unrecognized - VARCHAR, TEXT, UUID, JSON, ... - is a string."""
    text = type_text.strip().lower()
    base = re.split(r"[\s(]", text, maxsplit=1)[0] if text else ""
    if text in DATATYPES:
        return text
    if base in {"int", "integer", "bigint", "smallint", "tinyint", "mediumint", "serial", "bigserial", "smallserial", "int2", "int4", "int8"}:
        return "integer"
    if base in {"decimal", "numeric", "float", "double", "real", "money", "number", "float4", "float8", "dec", "fixed"}:
        return "decimal"
    if base in {"bool", "boolean", "bit"}:
        return "boolean"
    if base in {"date", "datetime", "timestamp", "timestamptz", "datetime2", "time", "smalldatetime"}:
        return "datetime"
    return "string"
