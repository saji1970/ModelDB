"""Parsers: file bytes -> `ImportPlan`. No reader touches a database."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from typing import Any

from mdc.portability.plan import (
    DATATYPES,
    RECORD_ID,
    ColumnSpec,
    ImportFormatError,
    ImportPlan,
    RowSpec,
    TableSpec,
    is_valid_identifier,
    safe_identifier,
    sql_type_to_datatype,
)

EXPORT_FORMAT_NAME = "mdc-export"
EXPORT_FORMAT_VERSION = 1

# Extension -> reader. ".dnadb.json" must be matched before ".json".
_KINDS = (
    (".dnadb.json", "native"), (".dnadb", "native"), (".json", "native"),
    (".csv", "csv"), (".tsv", "csv"),
    (".sql", "sql"),
    (".md", "markdown"), (".markdown", "markdown"),
    (".mmd", "mermaid"), (".mermaid", "mermaid"),
)


def detect_kind(filename: str) -> str:
    lower = filename.lower()
    for suffix, kind in _KINDS:
        if lower.endswith(suffix):
            return kind
    raise ImportFormatError(
        f"Don't know how to import {filename!r}. Supported: .md (design document or Mermaid ER diagram), "
        ".mmd (Mermaid ER diagram), .csv, .sql, .json / .dnadb.json (an MDC export file)."
    )


def stem_of(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    for suffix, _ in _KINDS:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def read_import(filename: str, content: bytes) -> ImportPlan:
    kind = detect_kind(filename)
    stem = stem_of(filename)
    text = _decode(content) if kind != "native" else None
    if kind == "csv":
        table, _ = safe_identifier(stem)
        return read_csv(text or "", table)
    if kind == "sql":
        return read_sql(text or "")
    if kind == "markdown":
        return read_markdown(text or "")
    if kind == "mermaid":
        return read_mermaid(text or "")
    return read_native(content)


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportFormatError("The file is not valid UTF-8 text. Save it as UTF-8 and try again.") from exc


# -- CSV ----------------------------------------------------------------------------

_INT_RE = re.compile(r"^[+-]?(0|[1-9][0-9]*)$")
_FLOAT_RE = re.compile(r"^[+-]?(0|[1-9][0-9]*)?(\.[0-9]+)?([eE][+-]?[0-9]+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def unguard_cell(value: str) -> str:
    """Undo `writers._guard_cell`: a leading ' in front of = + - @ was put
    there on export so a spreadsheet would not run it as a formula."""
    if len(value) > 1 and value[0] == "'" and value[1] in "=+-@":
        return value[1:]
    return value


def _infer_type(values: list[str]) -> str:
    present = [v for v in values if v != ""]
    if not present:
        return "string"
    if all(_INT_RE.match(v) for v in present):
        return "integer"
    if all(_FLOAT_RE.match(v) and any(ch.isdigit() for ch in v) for v in present):
        return "decimal"
    if all(v.lower() in ("true", "false") for v in present):
        return "boolean"
    if all(_DATE_RE.match(v) and _parses_as_datetime(v) for v in present):
        return "datetime"
    return "string"


def _parses_as_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def read_csv(text: str, table_name: str) -> ImportPlan:
    if not text.strip():
        raise ImportFormatError("The CSV file is empty.")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    csv.field_size_limit(10_000_000)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        raise ImportFormatError("The CSV file has no header row.") from None

    warnings: list[str] = []
    used: set[str] = set()
    names: list[str] = []
    for raw in header:
        if raw.strip().lower() == RECORD_ID:
            names.append(RECORD_ID)
            used.add(RECORD_ID)
            continue
        name, changed = safe_identifier(raw, used)
        if changed:
            warnings.append(f"Column {raw.strip()!r} was renamed to {name!r}.")
        names.append(name)
    if len(names) > 500:
        raise ImportFormatError(f"The CSV has {len(names)} columns; at most 500 are supported.")

    raw_rows: list[list[str]] = []
    for line_no, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        if len(cells) > len(names):
            raise ImportFormatError(f"Row {line_no} has {len(cells)} values but the header has {len(names)} columns.")
        cells = [unguard_cell(c) for c in cells] + [""] * (len(names) - len(cells))
        raw_rows.append(cells)
    if not raw_rows:
        warnings.append("The CSV has a header but no data rows - only the table structure will be created.")

    columns: list[ColumnSpec] = []
    for i, name in enumerate(names):
        if name == RECORD_ID:
            continue
        columns.append(ColumnSpec(name=name, datatype=_infer_type([r[i] for r in raw_rows])))
    if not columns:
        raise ImportFormatError("The CSV has no columns besides record_id.")

    types = {c.name: c.datatype for c in columns}
    rows: list[RowSpec] = []
    for line_no, cells in enumerate(raw_rows, start=1):
        values: dict[str, Any] = {}
        record_id: str | None = None
        for name, cell in zip(names, cells):
            if name == RECORD_ID:
                record_id = cell.strip() or None
            elif cell != "":
                values[name] = _csv_value(cell, types[name])
        rows.append(RowSpec(values=values, where=f"row {line_no}", record_id=record_id))
    return ImportPlan(tables=[TableSpec(name=table_name, columns=columns, rows=rows)], warnings=warnings, target="session")


def _csv_value(cell: str, datatype: str) -> Any:
    if datatype == "integer":
        return int(cell)
    if datatype == "decimal":
        return float(cell)
    if datatype == "boolean":
        return cell.lower() == "true"
    return cell


# -- SQL script (parsed, never executed) ---------------------------------------------

Token = tuple[str, Any]  # (kind, value): word | qident | string | number | punct | op

_SQL_COLUMN_STOP = {
    "NOT", "NULL", "PRIMARY", "DEFAULT", "REFERENCES", "UNIQUE", "CHECK", "AUTO_INCREMENT", "AUTOINCREMENT",
    "GENERATED", "COLLATE", "COMMENT", "CONSTRAINT", "IDENTITY", "ON",
}
_SQL_TABLE_CONSTRAINT = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "KEY", "INDEX", "FULLTEXT", "SPATIAL", "LIKE", "EXCLUDE"}


def _tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j == -1 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j == -1:
                raise ImportFormatError("Unterminated /* comment in the SQL script.")
            i = j + 2
        elif ch == "'":
            j, out = i + 1, []
            while True:
                if j >= n:
                    raise ImportFormatError("Unterminated string literal in the SQL script.")
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        out.append("'")
                        j += 2
                        continue
                    break
                out.append(text[j])
                j += 1
            tokens.append(("string", "".join(out)))
            i = j + 1
        elif ch in '"`[':
            close = {'"': '"', "`": "`", "[": "]"}[ch]
            j = text.find(close, i + 1)
            if j == -1:
                raise ImportFormatError("Unterminated quoted identifier in the SQL script.")
            tokens.append(("qident", text[i + 1 : j]))
            i = j + 1
        elif ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            m = re.compile(r"\d*\.?\d+(?:[eE][+-]?\d+)?|\d+\.").match(text, i)
            assert m is not None
            tokens.append(("number", m.group(0)))
            i = m.end()
        elif ch.isalpha() or ch == "_":
            m = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*").match(text, i)
            assert m is not None
            tokens.append(("word", m.group(0)))
            i = m.end()
        elif ch in "(),;.":
            tokens.append(("punct", ch))
            i += 1
        else:
            tokens.append(("op", ch))
            i += 1
    return tokens


_KEYWORD_SECOND = {"TABLE", "FROM", "INTO", "INDEX", "VIEW", "DATABASE", "SCHEMA", "TRIGGER", "SEQUENCE", "TRANSACTION"}


def _statement_kind(st: list[Token]) -> str:
    """"DROP TABLE" / "UPDATE" - the keywords only, never a table name."""
    kind = _upper(st[0]) or "statement"
    second = _upper(st[1] if len(st) > 1 else None)
    return f"{kind} {second}" if second in _KEYWORD_SECOND else kind


def _split_statements(tokens: list[Token]) -> list[list[Token]]:
    statements: list[list[Token]] = []
    current: list[Token] = []
    for token in tokens:
        if token == ("punct", ";"):
            if current:
                statements.append(current)
            current = []
        else:
            current.append(token)
    if current:
        statements.append(current)
    return statements


def _upper(token: Token | None) -> str:
    return str(token[1]).upper() if token is not None and token[0] == "word" else ""


def _is_ident(token: Token | None) -> bool:
    return token is not None and token[0] in ("word", "qident")


def _read_qualified_name(tokens: list[Token], pos: int) -> tuple[str, int]:
    """`name`, `schema.name` or `"schema"."name"` -> the last part."""
    if pos >= len(tokens) or not _is_ident(tokens[pos]):
        raise ImportFormatError("Expected a table name.")
    name = str(tokens[pos][1])
    pos += 1
    while pos + 1 < len(tokens) and tokens[pos] == ("punct", ".") and _is_ident(tokens[pos + 1]):
        name = str(tokens[pos + 1][1])
        pos += 2
    return name, pos


def _group(tokens: list[Token], pos: int) -> tuple[list[Token], int]:
    """tokens[pos] is '(' -> the tokens inside its matching ')', and the position after it."""
    assert tokens[pos] == ("punct", "(")
    depth, start = 0, pos + 1
    for j in range(pos, len(tokens)):
        if tokens[j] == ("punct", "("):
            depth += 1
        elif tokens[j] == ("punct", ")"):
            depth -= 1
            if depth == 0:
                return tokens[start:j], j + 1
    raise ImportFormatError("Unbalanced parentheses in the SQL script.")


def _split_top_level(tokens: list[Token]) -> list[list[Token]]:
    parts: list[list[Token]] = []
    current: list[Token] = []
    depth = 0
    for token in tokens:
        if token == ("punct", "("):
            depth += 1
        elif token == ("punct", ")"):
            depth -= 1
        if token == ("punct", ",") and depth == 0:
            parts.append(current)
            current = []
        else:
            current.append(token)
    if current:
        parts.append(current)
    return parts


class _SqlTable:
    def __init__(self, spec: TableSpec, order: list[str]):
        self.spec = spec
        self.order = order  # every column in declared order, record_id included


def read_sql(text: str) -> ImportPlan:
    header = re.search(r"^--\s*mdc-database:\s*([A-Za-z][A-Za-z0-9_-]*)", text, re.MULTILINE)
    database: str | None = header.group(1) if header else None
    tables: dict[str, _SqlTable] = {}
    warnings: list[str] = []
    skipped: list[str] = []
    statements = _split_statements(_tokenize(text))
    if not statements:
        raise ImportFormatError("The SQL script contains no statements.")

    for number, st in enumerate(statements, start=1):
        first, second = _upper(st[0]), _upper(st[1] if len(st) > 1 else None)
        try:
            if first == "CREATE" and second in ("DATABASE", "SCHEMA"):
                pos = 2
                if _upper(st[pos]) == "IF":
                    pos += 3
                declared = str(st[pos][1])
                if database is None:
                    database = declared
                elif database != declared:
                    warnings.append(f"Statement {number}: only one database per script is imported; ignoring {declared!r}.")
            elif first == "USE" and len(st) > 1 and _is_ident(st[1]):
                if database is None:
                    database = str(st[1][1])
            elif first == "CREATE" and (second == "TABLE" or (second in ("TEMP", "TEMPORARY", "UNLOGGED") and _upper(st[2]) == "TABLE")):
                _sql_create_table(st, number, tables, warnings)
            elif first == "INSERT":
                _sql_insert(st, number, tables)
            else:
                skipped.append(f"#{number} {_statement_kind(st)}")
        except IndexError:
            raise ImportFormatError(f"Statement {number} ends unexpectedly.") from None

    if skipped:
        shown = ", ".join(skipped[:8]) + (" ..." if len(skipped) > 8 else "")
        warnings.append(f"Skipped {len(skipped)} statement(s) that only define or change structure/data MDC does not import this way: {shown}. Nothing in the script is ever executed.")
    if not tables:
        raise ImportFormatError("The SQL script has no CREATE TABLE or INSERT statements MDC can import.")
    return ImportPlan(tables=[t.spec for t in tables.values()], database=database, warnings=warnings, target="file")


def _sql_create_table(st: list[Token], number: int, tables: dict[str, _SqlTable], warnings: list[str]) -> None:
    pos = 2 if _upper(st[1]) == "TABLE" else 3
    if_not_exists = False
    if _upper(st[pos]) == "IF":
        if_not_exists = True
        pos += 3
    raw_name, pos = _read_qualified_name(st, pos)
    name, changed = safe_identifier(raw_name)
    if changed:
        warnings.append(f"Table {raw_name!r} was renamed to {name!r}.")
    if pos >= len(st) or st[pos] != ("punct", "("):
        raise ImportFormatError(f"Statement {number}: CREATE TABLE {raw_name} has no column list (CREATE TABLE ... AS SELECT is not supported).")
    inner, _ = _group(st, pos)

    columns: list[ColumnSpec] = []
    order: list[str] = []
    primary_keys: list[str] = []
    used: set[str] = set()
    for part in _split_top_level(inner):
        if not part:
            continue
        lead = _upper(part[0])
        if lead in _SQL_TABLE_CONSTRAINT and part[0][0] == "word":
            if lead == "PRIMARY" or (lead == "CONSTRAINT" and "PRIMARY" in [_upper(t) for t in part]):
                at = next((i for i, t in enumerate(part) if t == ("punct", "(")), None)
                if at is not None:
                    cols, _ = _group(part, at)
                    primary_keys += [str(t[1]) for t in cols if _is_ident(t)]
            continue
        col_raw = str(part[0][1])
        type_words: list[str] = []
        i = 1
        while i < len(part):
            if part[i] == ("punct", "("):
                _, i = _group(part, i)
                continue
            if part[i][0] != "word" or _upper(part[i]) in _SQL_COLUMN_STOP:
                break
            type_words.append(str(part[i][1]))
            i += 1
        rest = [_upper(t) for t in part[i:]]
        required = ("NOT" in rest and "NULL" in rest) or ("PRIMARY" in rest and "KEY" in rest)
        if col_raw.lower() == RECORD_ID:
            order.append(RECORD_ID)
            continue
        col_name, col_changed = safe_identifier(col_raw, used)
        if col_changed:
            warnings.append(f"Column {raw_name}.{col_raw} was renamed to {col_name!r}.")
        datatype = sql_type_to_datatype(" ".join(type_words))
        columns.append(ColumnSpec(name=col_name, datatype=datatype, required=required))
        order.append(col_name)
    for key in primary_keys:
        for c in columns:
            if c.name == key.lower():
                c.required = True
    if not columns:
        raise ImportFormatError(f"Statement {number}: table {raw_name!r} has no usable columns.")
    spec = TableSpec(name=name, columns=columns, if_not_exists=if_not_exists)
    tables[name] = _SqlTable(spec, order)


def _sql_literal(tokens: list[Token], number: int) -> Any:
    sign = 1
    if tokens and tokens[0] in (("op", "-"), ("op", "+")):
        sign = -1 if tokens[0][1] == "-" else 1
        tokens = tokens[1:]
    if len(tokens) == 1:
        kind, value = tokens[0]
        if kind == "string":
            return value
        if kind == "number":
            number_value = float(value) if re.search(r"[.eE]", value) else int(value)
            return sign * number_value
        if kind == "word" and value.upper() == "NULL":
            return None
        if kind == "word" and value.upper() in ("TRUE", "FALSE"):
            return value.upper() == "TRUE"
        if kind == "word" and value.upper() == "DEFAULT":
            return None
    shown = " ".join(str(t[1]) for t in tokens)[:40]
    raise ImportFormatError(f"Statement {number}: unsupported value {shown!r} - only plain strings, numbers, TRUE/FALSE and NULL can be imported (no functions or expressions).")


def _sql_insert(st: list[Token], number: int, tables: dict[str, _SqlTable]) -> None:
    pos = 1
    if _upper(st[pos]) == "INTO":
        pos += 1
    raw_name, pos = _read_qualified_name(st, pos)
    name, _ = safe_identifier(raw_name)
    columns: list[str] | None = None
    if st[pos] == ("punct", "("):
        inner, pos = _group(st, pos)
        columns = [RECORD_ID if str(t[1]).lower() == RECORD_ID else safe_identifier(str(t[1]))[0] for t in inner if _is_ident(t)]
    if _upper(st[pos]) != "VALUES":
        raise ImportFormatError(f"Statement {number}: only INSERT ... VALUES is supported (not INSERT ... SELECT).")
    pos += 1

    table = tables.get(name)
    if columns is None:
        if table is None:
            raise ImportFormatError(f"Statement {number}: INSERT INTO {raw_name} has no column list and the script never creates that table.")
        columns = table.order
    if table is None:
        table = _SqlTable(TableSpec(name=name, columns=None), [])
        tables[name] = table

    row_no = 0
    while pos < len(st):
        if st[pos] == ("punct", ","):
            pos += 1
            continue
        if st[pos] != ("punct", "("):
            raise ImportFormatError(f"Statement {number}: expected a (value, ...) group.")
        inner, pos = _group(st, pos)
        row_no += 1
        cells = _split_top_level(inner)
        if len(cells) != len(columns):
            raise ImportFormatError(f"Statement {number}, row {row_no}: {len(cells)} values for {len(columns)} columns.")
        values: dict[str, Any] = {}
        record_id: str | None = None
        for col, cell in zip(columns, cells):
            value = _sql_literal(cell, number)
            if col == RECORD_ID:
                record_id = None if value is None else str(value)
            elif value is not None:
                values[col] = value
        table.spec.rows.append(RowSpec(values=values, where=f"INSERT #{number}, row {row_no}", record_id=record_id))


# -- Markdown design document --------------------------------------------------------

_MD_DB_RE = re.compile(r"^#\s+Database:\s*`?([A-Za-z][A-Za-z0-9_-]*)`?\s*$", re.IGNORECASE)
_MD_TABLE_RE = re.compile(r"^#{2,6}\s+Table:\s*`?([^`\s]+)`?\s*$", re.IGNORECASE)
_TRUE_WORDS = {"yes", "y", "true", "required", "not null", "x", "✓", "✔"}


def _md_cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip().strip("`").strip() for c in line.split("|")]


def read_markdown(text: str) -> ImportPlan:
    lines = text.splitlines()
    database: str | None = None
    warnings: list[str] = []
    tables: list[TableSpec] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            i += 1
            continue
        db_match = _MD_DB_RE.match(line)
        if db_match and database is None:
            database = db_match.group(1)
        table_match = _MD_TABLE_RE.match(line)
        if table_match:
            raw_name = table_match.group(1)
            name, changed = safe_identifier(raw_name)
            if changed:
                warnings.append(f"Table {raw_name!r} was renamed to {name!r}.")
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("|"):
                if _MD_TABLE_RE.match(lines[j]) or lines[j].startswith("#"):
                    break
                j += 1
            block: list[str] = []
            while j < len(lines) and lines[j].strip().startswith("|"):
                block.append(lines[j])
                j += 1
            if not block:
                raise ImportFormatError(f"Table {raw_name!r} has no Field/Type table under its heading.")
            tables.append(_md_table(name, raw_name, block, warnings))
            i = j
            continue
        i += 1
    if not tables:
        diagram = _mermaid_fence(lines)
        if diagram is not None:
            plan = read_mermaid(diagram)
            plan.database = database
            return plan
        raise ImportFormatError(
            "No tables found. Under a heading like '## Table: customers', add a table with 'Field' and 'Type' "
            "columns (and optionally 'Required') - or include a ```mermaid erDiagram block. "
            "Types: string, decimal, integer, boolean, datetime."
        )
    return ImportPlan(tables=tables, database=database, warnings=warnings, target="file")


def _mermaid_fence(lines: list[str]) -> str | None:
    inside, block = False, []
    for line in lines:
        if line.strip().startswith("```"):
            if inside:
                if block and block[0].strip().lower().startswith("erdiagram"):
                    return "\n".join(block)
                block = []
            inside = not inside
        elif inside:
            block.append(line)
    return None


def _md_table(name: str, raw_name: str, block: list[str], warnings: list[str]) -> TableSpec:
    header = [c.lower() for c in _md_cells(block[0])]
    field_col = next((k for k, h in enumerate(header) if h in ("field", "column", "name", "attribute")), None)
    type_col = next((k for k, h in enumerate(header) if h in ("type", "datatype", "data type")), None)
    required_col = next((k for k, h in enumerate(header) if h in ("required", "not null", "mandatory")), None)
    nullable_col = next((k for k, h in enumerate(header) if h == "nullable"), None)
    if field_col is None or type_col is None:
        raise ImportFormatError(f"Table {raw_name!r}: the table needs a 'Field' column and a 'Type' column.")
    columns: list[ColumnSpec] = []
    used: set[str] = set()
    for row in block[1:]:
        cells = _md_cells(row)
        if all(re.fullmatch(r":?-{2,}:?", c) or c == "" for c in cells):
            continue  # the |---|---| separator
        if len(cells) <= max(field_col, type_col):
            continue
        raw_field = cells[field_col]
        if not raw_field or raw_field.lower() == RECORD_ID:
            continue
        field_name, changed = safe_identifier(raw_field, used)
        if changed:
            warnings.append(f"Field {raw_name}.{raw_field} was renamed to {field_name!r}.")
        type_text = cells[type_col]
        datatype = sql_type_to_datatype(type_text)
        if type_text.lower() not in DATATYPES and type_text:
            warnings.append(f"{raw_name}.{raw_field}: type {type_text!r} was read as {datatype!r}.")
        required = False
        if required_col is not None and required_col < len(cells):
            required = cells[required_col].lower() in _TRUE_WORDS
        if nullable_col is not None and nullable_col < len(cells):
            required = cells[nullable_col].lower() in {"no", "n", "false"}
        columns.append(ColumnSpec(name=field_name, datatype=datatype, required=required))
    if not columns:
        raise ImportFormatError(f"Table {raw_name!r} lists no fields.")
    return TableSpec(name=name, columns=columns)


# -- Mermaid ER diagram --------------------------------------------------------------

_ER_ENTITY_OPEN = re.compile(r'^("[^"]+"|[A-Za-z_][\w-]*)(?:\s*\[[^\]]*\])?\s*\{\s*$')
_ER_ENTITY_ONE_LINE = re.compile(r'^("[^"]+"|[A-Za-z_][\w-]*)(?:\s*\[[^\]]*\])?\s*\{(.*)\}\s*$')
_ER_ATTRIBUTE = re.compile(r'^([\w()\[\],-]+)\s+([A-Za-z_][\w-]*)((?:\s+(?:PK|FK|UK)(?:\s*,\s*(?:PK|FK|UK))*)?)(?:\s+"([^"]*)")?\s*$', re.IGNORECASE)
_ER_RELATION = re.compile(r'^("[^"]+"|[A-Za-z_][\w-]*)\s+([|}o][|o]--[|o][|{o])\s+("[^"]+"|[A-Za-z_][\w-]*)\s*(?::\s*(.*))?$')


def _singular(name: str) -> str:
    if name.endswith("ies") and len(name) > 4:
        return name[:-3] + "y"
    if name.endswith("s") and not name.endswith(("ss", "us", "is")) and len(name) > 3:
        return name[:-1]
    return name


def read_mermaid(text: str, target: str = "file") -> ImportPlan:
    """A Mermaid `erDiagram`: each entity block becomes a table. MDC stores
    no foreign keys, so a relationship becomes a `<parent>_id` field on the
    "many" side - the same convention the ER diagram export infers back."""
    tables: dict[str, list[ColumnSpec]] = {}
    order: list[str] = []
    warnings: list[str] = []
    relations: list[tuple[str, str, str, str]] = []
    current: str | None = None
    seen_header = False

    def table_name(raw: str) -> str:
        name, changed = safe_identifier(raw.strip('"'))
        if changed:
            warnings.append(f"Entity {raw.strip(chr(34))!r} was renamed to {name!r}.")
        return name

    def add_attribute(table: str, line: str, number: int) -> None:
        attr = _ER_ATTRIBUTE.match(line)
        if attr is None:
            raise ImportFormatError(f"Line {number}: cannot read the attribute {line!r} (expected: type name [PK|FK|UK] [\"comment\"]).")
        type_text, raw_name, keys, comment = attr.group(1), attr.group(2), attr.group(3).upper(), attr.group(4) or ""
        if raw_name.lower() == RECORD_ID:
            return
        name, changed = safe_identifier(raw_name)
        if changed:
            warnings.append(f"Attribute {table}.{raw_name} was renamed to {name!r}.")
        if any(c.name == name for c in tables[table]):
            return
        required = "PK" in keys or "required" in comment.lower() or "not null" in comment.lower()
        tables[table].append(ColumnSpec(name=name, datatype=sql_type_to_datatype(type_text), required=required))

    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("%%", 1)[0].strip()
        if not line:
            continue
        if not seen_header:
            if line.lower().startswith("erdiagram"):
                seen_header = True
                continue
            raise ImportFormatError("A Mermaid ER diagram must start with 'erDiagram'.")
        if current is not None:
            if line == "}":
                current = None
                continue
            add_attribute(current, line, number)
            continue
        one_line = _ER_ENTITY_ONE_LINE.match(line)
        if one_line:
            current_name = table_name(one_line.group(1))
            if current_name not in tables:
                tables[current_name] = []
                order.append(current_name)
            if one_line.group(2).strip():
                add_attribute(current_name, one_line.group(2).strip(), number)
            continue
        opened = _ER_ENTITY_OPEN.match(line)
        if opened:
            current = table_name(opened.group(1))
            if current not in tables:
                tables[current] = []
                order.append(current)
            continue
        relation = _ER_RELATION.match(line)
        if relation:
            relations.append((table_name(relation.group(1)), relation.group(2), table_name(relation.group(3)), (relation.group(4) or "").strip().strip('"')))
            continue
        if re.fullmatch(r'("[^"]+"|[A-Za-z_][\w-]*)', line):  # an entity named on its own line, no attributes
            name = table_name(line)
            if name not in tables:
                tables[name] = []
                order.append(name)
            continue
        raise ImportFormatError(f"Line {number}: cannot read {line!r} as an entity block or a relationship.")
    if not seen_header:
        raise ImportFormatError("A Mermaid ER diagram must start with 'erDiagram'.")
    if current is not None:
        raise ImportFormatError(f"The entity {current!r} is never closed with '}}'.")

    for left, symbol, right, label in relations:
        left_many, right_many = symbol[0] == "}", symbol[-1] == "{"
        if left not in tables or right not in tables:
            warnings.append(f"Relationship {left} - {right} refers to an entity that is not defined, so no field was added for it.")
            continue
        if left_many and right_many:
            warnings.append(f"{left} and {right} are many-to-many: add a table that holds both ids - MDC has no join tables to infer.")
            continue
        parent, child = (right, left) if left_many else (left, right)
        label_name, _ = safe_identifier(label) if label else ("", False)
        field_name = label_name if label_name.endswith("_id") and label_name != RECORD_ID else f"{_singular(parent)}_id"
        if not any(c.name == field_name for c in tables[child]):
            tables[child].append(ColumnSpec(name=field_name, datatype="string"))
            warnings.append(f"Relationship {parent} -> {child}: added field {child}.{field_name} (MDC stores no foreign keys).")

    specs: list[TableSpec] = []
    for name in order:
        if not tables[name]:
            warnings.append(f"Entity {name!r} has no attributes, so no table was created for it.")
            continue
        specs.append(TableSpec(name=name, columns=tables[name]))
    if not specs:
        raise ImportFormatError("The ER diagram defines no entities with attributes.")
    return ImportPlan(tables=specs, warnings=warnings, target=target)



# -- MDC export file -----------------------------------------------------------------

def read_native(content: bytes) -> ImportPlan:
    try:
        bundle = json.loads(_decode(content))
    except json.JSONDecodeError as exc:
        raise ImportFormatError(f"Not valid JSON: {exc.msg} (line {exc.lineno}).") from exc
    if not isinstance(bundle, dict) or bundle.get("format") != EXPORT_FORMAT_NAME:
        raise ImportFormatError("This JSON file is not an MDC export file (it has no \"format\": \"mdc-export\").")
    version = bundle.get("version")
    if version != EXPORT_FORMAT_VERSION:
        raise ImportFormatError(f"This MDC export file is version {version!r}; this server reads version {EXPORT_FORMAT_VERSION}.")
    raw_tables = bundle.get("tables")
    if not isinstance(raw_tables, dict) or not raw_tables:
        raise ImportFormatError("The export file contains no tables.")

    tables: list[TableSpec] = []
    for name, body in raw_tables.items():
        if not is_valid_identifier(name):
            raise ImportFormatError(f"Invalid table name {name!r} in the export file.")
        if not isinstance(body, dict):
            raise ImportFormatError(f"Table {name!r} is malformed.")
        columns: list[ColumnSpec] | None = None
        if "fields" in body:
            columns = []
            for field_name, info in (body["fields"] or {}).items():
                if not is_valid_identifier(field_name) or field_name == RECORD_ID:
                    raise ImportFormatError(f"Invalid field name {field_name!r} in table {name!r}.")
                datatype = (info or {}).get("datatype")
                if datatype not in DATATYPES:
                    raise ImportFormatError(f"Field {name}.{field_name} has unknown datatype {datatype!r}.")
                columns.append(ColumnSpec(field_name, datatype, bool((info or {}).get("required", False))))
        rows: list[RowSpec] = []
        for k, row in enumerate(body.get("rows") or [], start=1):
            if not isinstance(row, dict) or not isinstance(row.get("fields"), dict):
                raise ImportFormatError(f"Row {k} of table {name!r} is malformed.")
            rows.append(RowSpec(values=dict(row["fields"]), where=f"{name} row {k}", record_id=row.get("record_id")))
        tables.append(TableSpec(name=name, columns=columns, rows=rows))
    source = bundle.get("source_database")
    return ImportPlan(tables=tables, database=source if isinstance(source, str) else None, target="file")
