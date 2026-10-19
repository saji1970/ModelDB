"""Validate an `ImportPlan` against a target database, then apply it.

`prepare_import` does every check that can fail - names, conflicts,
value types, required fields, duplicate record ids - *before* anything
is written, and reports which permissions applying the plan needs. The
caller (REST) checks those against the caller's role, then calls
`apply()`. So a bad file, or a caller who may not create databases,
never leaves a half-imported database behind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mdc.databases.errors import InvalidDatabaseNameError
from mdc.databases.manager import DatabaseManager
from mdc.engine.data_engine import RECORD_ID_RE
from mdc.model.operation import CreateOperation
from mdc.portability.plan import (
    ImportConflictError,
    ImportDataError,
    ImportFormatError,
    ImportPlan,
    RowSpec,
    TableSpec,
    safe_identifier,
)
from mdc.schema.registry import FieldSchema, SchemaError, SchemaRegistry
from mdc.security.roles import Permission

_TRUE = {"true", "t", "yes", "y", "1"}
_FALSE = {"false", "f", "no", "n", "0"}


@dataclass
class ImportResult:
    database: str
    created_database: bool
    tables_created: list[str]
    rows_inserted: dict[str, int]
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        total = sum(self.rows_inserted.values())
        tables = ", ".join(self.tables_created) if self.tables_created else "no new tables"
        head = f'Imported into {"new " if self.created_database else ""}database "{self.database}": {tables} ({total} row(s) loaded).'
        return head + "".join(f"\n- {w}" for w in self.warnings)


def normalize_value(value: Any, datatype: str) -> Any:
    """Make a parsed file value fit a column's datatype, or raise ValueError.
    `bool("false")` is True in Python, so booleans are read by word, not cast."""
    if value is None:
        return None
    if datatype == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"{value!r} is not a boolean")
    if datatype == "integer":
        if isinstance(value, bool):
            raise ValueError(f"{value!r} is not an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
            return int(value)
        raise ValueError(f"{value!r} is not an integer")
    if datatype == "decimal":
        if isinstance(value, bool):
            raise ValueError(f"{value!r} is not a number")
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except ValueError:
            raise ValueError(f"{value!r} is not a number") from None
    if datatype == "datetime":
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).strip())
        except ValueError:
            raise ValueError(f"{value!r} is not an ISO date/time") from None
    return value if isinstance(value, str) else str(value)


class PreparedImport:
    def __init__(self, manager: DatabaseManager, plan: ImportPlan, database: str, db_exists: bool, create: list[TableSpec], skipped_existing: list[str]):
        self._manager = manager
        self._plan = plan
        self.database = database
        self.db_exists = db_exists
        self._create = create
        self._skipped_existing = skipped_existing

    @property
    def required_permissions(self) -> set[Permission]:
        needed: set[Permission] = set()
        if not self.db_exists:
            needed.add(Permission.CREATE_DATABASE)
        if self._create:
            needed.add(Permission.CREATE_TABLE)
        if any(t.rows for t in self._plan.tables):
            needed.add(Permission.WRITE)
        return needed or {Permission.WRITE}

    def apply(self) -> ImportResult:
        manager = self._manager
        handle = manager.create(self.database) if not self.db_exists else manager.get(self.database)
        for spec in self._create:
            assert spec.columns is not None
            handle.schema_registry.create_collection(
                spec.name, {c.name: FieldSchema(name=c.name, datatype=c.datatype, required=c.required) for c in spec.columns}
            )
        if self._create or not self.db_exists:
            manager.persist_schema(self.database)
        inserted: dict[str, int] = {}
        for spec in self._plan.tables:
            for row in spec.rows:
                handle.engine.create(CreateOperation(collection=spec.name, data=_clean(row, handle.schema_registry, spec.name), record_id=row.record_id))
            if spec.rows:
                inserted[spec.name] = len(spec.rows)
        warnings = list(self._plan.warnings) + [f'Table "{t}" already existed, so it was kept as it is and rows were added to it.' for t in self._skipped_existing]
        return ImportResult(self.database, not self.db_exists, [t.name for t in self._create], inserted, warnings)


def _clean(row: RowSpec, registry: SchemaRegistry, table: str) -> dict[str, Any]:
    fields = registry.get_collection(table).fields
    return {k: normalize_value(v, fields[k].datatype) for k, v in row.values.items()}


def resolve_database_name(plan: ImportPlan, explicit: str | None, session_database: str | None, filename_stem: str) -> str:
    if explicit:
        return explicit
    if plan.target == "session" and session_database:
        return session_database
    if plan.target == "session":
        return "default"
    if plan.database:
        return plan.database
    name, _ = safe_identifier(filename_stem)
    return name.replace("_", "-") if name[0].isalpha() else "db" + name


def prepare_import(manager: DatabaseManager, plan: ImportPlan, database: str) -> PreparedImport:
    try:
        db_exists = manager.exists(database)
    except InvalidDatabaseNameError as exc:
        raise ImportFormatError(str(exc)) from exc

    existing = manager.get(database).schema_registry if db_exists else SchemaRegistry()
    scratch = SchemaRegistry.from_dict(existing.to_dict())
    conflicts: list[str] = []
    create: list[TableSpec] = []
    skipped_existing: list[str] = []
    seen: set[str] = set()

    for spec in plan.tables:
        if spec.name in seen:
            raise ImportFormatError(f'The file defines table "{spec.name}" more than once.')
        seen.add(spec.name)
        if spec.columns is not None:
            if scratch.has_collection(spec.name):
                if spec.if_not_exists:
                    skipped_existing.append(spec.name)
                else:
                    conflicts.append(f'table "{spec.name}" already exists in database "{database}"')
                continue
            scratch.create_collection(spec.name, {c.name: FieldSchema(c.name, c.datatype, c.required) for c in spec.columns})
            create.append(spec)
        elif not scratch.has_collection(spec.name):
            raise ImportFormatError(
                f'The file has rows for table "{spec.name}" but no structure for it, and database "{database}" has no such table. '
                "Import a file that includes the structure (an MDC export with structure, a SQL script with CREATE TABLE, a CSV)."
            )
    if conflicts:
        raise ImportConflictError(conflicts)

    problems: list[str] = []
    for spec in plan.tables:
        fields = scratch.get_collection(spec.name).fields
        ids: set[str] = set()
        for row in spec.rows:
            label = f"{spec.name}, {row.where}"
            cleaned: dict[str, Any] = {}
            for key, value in row.values.items():
                if key not in fields:
                    problems.append(f'{label}: unknown field "{key}"')
                    continue
                try:
                    cleaned[key] = normalize_value(value, fields[key].datatype)
                except ValueError as exc:
                    problems.append(f'{label}: field "{key}" expects {fields[key].datatype}: {exc}')
            try:
                scratch.validate_record(spec.name, cleaned)
            except SchemaError as exc:
                problems.append(f"{label}: {exc}")
            if row.record_id is not None:
                if not RECORD_ID_RE.match(row.record_id):
                    problems.append(f'{label}: "{row.record_id}" is not a valid record_id (letters, digits, "_", ".", "-"; at most 64 characters)')
                if row.record_id in ids:
                    conflicts.append(f'{spec.name}: record_id "{row.record_id}" appears twice in the file')
                ids.add(row.record_id)
                if db_exists and existing.has_collection(spec.name) and manager.get(database).engine.has_record(spec.name, row.record_id):
                    conflicts.append(f'{spec.name}: record "{row.record_id}" already exists in database "{database}"')
    if problems:
        raise ImportDataError(problems)
    if conflicts:
        raise ImportConflictError(conflicts)
    return PreparedImport(manager, plan, database, db_exists, create, skipped_existing)

