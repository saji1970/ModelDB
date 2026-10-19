"""Database/table administration turn handling: create/use/list
databases, create/describe tables (SchemaRegistry collections), and
browse/insert data - all driven from chat/CLI text.

Checked before both the object-storage NLP (`conversation.interpreter`)
and the merchants interpreter (`cql.interpreter`) - see
`nlp.db_command`'s module docstring for the collision-safety argument.
Table creation only ever produces a typed `SchemaRegistry` collection,
never raw SQL DDL - free-text chat input driving arbitrary SQL would be
a real injection surface; a validated field list has no such risk.
"""

from __future__ import annotations

from mdc.conversation.result import TurnResult
from mdc.conversation.state import PendingSchema, StorageConversationState
from mdc.databases.errors import DatabaseAlreadyExistsError, DatabaseNotFoundError, InvalidDatabaseNameError
from mdc.databases.manager import DatabaseManager
from mdc.model.operation import CreateOperation, ReadOperation
from urllib.parse import quote

import json

from mdc.events import EventStoreError
from mdc.events.timeparse import parse_time
from mdc.nlp.db_command import DatabaseCommand, DatabaseIntent
from mdc.portability.apply import prepare_import, resolve_database_name
from mdc.portability.plan import ExportOptionError, ImportConflictError, ImportDataError, ImportFormatError, ImportPlan
from mdc.portability.writers import export_filename, resolve_export_options
from mdc.security.roles import Permission, role_has
from mdc.schema.registry import CollectionNotFoundError, SchemaError

_SHOW_DATA_LIMIT = 20


def handle_database_command(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    handler = _HANDLERS[command.intent]
    return handler(state, manager, command)


def _switch_to(state: StorageConversationState, name: str) -> None:
    # Object references from a different database are meaningless here -
    # rather than let a stale "it" silently resolve against the wrong
    # database's index, the pointer (and any pending confirmation) is
    # cleared on every database switch.
    state.current_database = name
    state.last_object_id = None
    state.pending_delete = None


def _lacks(state: StorageConversationState, permission: Permission) -> str | None:
    """A refusal message if this turn's caller (state.role) may not do
    `permission`; None when allowed - or when there is no role (CLI, tests)."""
    if state.role is not None and not role_has(state.role, permission):
        return f"Your role ({state.role.value}) doesn't include the {permission.value} permission, so I can't do that."
    return None


def _handle_create_database(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    assert command.database_name is not None
    if (refusal := _lacks(state, Permission.CREATE_DATABASE)) is not None:
        return TurnResult(refusal)
    try:
        manager.create(command.database_name)
    except (DatabaseAlreadyExistsError, InvalidDatabaseNameError) as exc:
        return TurnResult(str(exc))
    _switch_to(state, command.database_name)
    return TurnResult(f'Created database "{command.database_name}" and switched to it. It starts empty - use "create table ..." to add one.')


def _handle_use_database(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    assert command.database_name is not None
    try:
        manager.get(command.database_name)
    except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
        return TurnResult(str(exc))
    _switch_to(state, command.database_name)
    return TurnResult(f'Switched to database "{command.database_name}".')


def _handle_list_databases(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    names = manager.list_names()
    rows = [{"database": name, "current": name == state.current_database} for name in names]
    return TurnResult(f"{len(names)} database(s): {', '.join(names)}. Current: {state.current_database}.", data=rows)


def _handle_describe_database(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    handle = manager.get(state.current_database)
    tables = handle.schema_registry.list_collections()
    if not tables:
        return TurnResult(f'Database "{state.current_database}" has no tables yet. Try "create table <name> with <field> <type>, ...".')
    rows = [{"table": name, "fields": len(handle.schema_registry.get_collection(name).fields)} for name in tables]
    return TurnResult(f'{len(tables)} table(s) in "{state.current_database}": {", ".join(tables)}.', data=rows)


def _handle_create_table(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    if (refusal := _lacks(state, Permission.CREATE_TABLE)) is not None:
        return TurnResult(refusal)
    assert command.table_name is not None
    handle = manager.get(state.current_database)
    if handle.schema_registry.has_collection(command.table_name):
        return TurnResult(f'Table "{command.table_name}" already exists in "{state.current_database}".')
    handle.schema_registry.create_collection(command.table_name, command.fields)
    manager.persist_schema(state.current_database)
    field_summary = ", ".join(f"{f.name} ({f.datatype})" for f in command.fields.values())
    return TurnResult(f'Created table "{command.table_name}" in "{state.current_database}" with fields: {field_summary}.')


def _handle_describe_table(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    assert command.table_name is not None
    db_name = command.database_name or state.current_database
    try:
        handle = manager.get(db_name)
    except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
        return TurnResult(str(exc))
    try:
        collection = handle.schema_registry.get_collection(command.table_name)
    except CollectionNotFoundError as exc:
        return TurnResult(str(exc))
    rows = [{"field": f.name, "type": f.datatype, "required": f.required} for f in collection.fields.values()]
    return TurnResult(f'Table "{command.table_name}" in "{db_name}": {len(rows)} field(s).', data=rows)


def _handle_show_data(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    assert command.table_name is not None
    db_name = command.database_name or state.current_database
    try:
        handle = manager.get(db_name)
    except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
        return TurnResult(str(exc))
    try:
        result = handle.engine.read(ReadOperation(collection=command.table_name, filters=[], limit=_SHOW_DATA_LIMIT))
    except CollectionNotFoundError as exc:
        return TurnResult(str(exc))
    if result.count == 0:
        return TurnResult(f'No rows in "{command.table_name}" ({db_name}).')
    rows = [{"record_id": record.record_id, **record.fields} for record in result.records]
    suffix = f" (showing first {_SHOW_DATA_LIMIT})" if result.count == _SHOW_DATA_LIMIT else ""
    return TurnResult(f'{result.count} row(s) in "{command.table_name}" ({db_name}){suffix}.', data=rows)


def _handle_insert(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    assert command.table_name is not None
    handle = manager.get(state.current_database)
    try:
        result = handle.engine.create(CreateOperation(collection=command.table_name, data=command.values))
    except (CollectionNotFoundError, SchemaError) as exc:
        return TurnResult(str(exc))
    record = result.records[0]
    return TurnResult(f'Inserted {record.record_id} into "{command.table_name}".', data={"record_id": record.record_id, **record.fields})


_FIND_LIMIT = 20
# Preferred fields for an amount constraint ("under 20K") when a row has
# more than one numeric field - falls back to any numeric field if none
# of these are present, since a table isn't guaranteed to name its
# "the" price field any particular way.
_AMOUNT_FIELD_NAMES = {"price", "cost", "amount", "balance", "value", "total", "settlement_balance"}


def _numeric_candidates(fields: dict) -> list[float]:
    numeric = {k: v for k, v in fields.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    preferred = [v for k, v in numeric.items() if k.lower() in _AMOUNT_FIELD_NAMES]
    return preferred or list(numeric.values())


def _row_matches(fields: dict, term: str | None, min_value: float | None, max_value: float | None) -> bool:
    if min_value is not None or max_value is not None:
        candidates = _numeric_candidates(fields)
        if not any((min_value is None or v >= min_value) and (max_value is None or v <= max_value) for v in candidates):
            return False
    if term:
        tokens = [t for t in term.lower().split() if len(t) >= 2]
        if tokens:
            haystack = " ".join(str(v).lower() for v in fields.values())
            if not any(token in haystack for token in tokens):
                return False
    return True


def _handle_find(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    if command.database_name is not None:
        try:
            manager.get(command.database_name)
        except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
            return TurnResult(str(exc))
        database_names = [command.database_name]
    else:
        database_names = manager.list_names()

    rows: list[dict] = []
    documents: list[dict] = []
    for db_name in database_names:
        handle = manager.get(db_name)
        table_names = [command.table_name] if command.table_name else handle.schema_registry.list_collections()
        for table_name in table_names:
            if not handle.schema_registry.has_collection(table_name):
                continue
            result = handle.engine.read(ReadOperation(collection=table_name, filters=[]))
            for record in result.records:
                if _row_matches(record.fields, command.search_term, command.min_value, command.max_value):
                    rows.append({"database": db_name, "table": table_name, "record_id": record.record_id, **record.fields})

        if command.search_term:
            for doc in handle.object_service.search_documents(command.search_term):
                documents.append({"database": db_name, **doc})

    total = len(rows) + len(documents)
    term_desc = f' "{command.search_term}"' if command.search_term else ""
    if total == 0:
        return TurnResult(f"No matches for{term_desc}." if term_desc else "No matches.")

    parts = []
    if rows:
        parts.append(f"{len(rows)} row(s)")
    if documents:
        parts.append(f"{len(documents)} document(s)")
    suffix = f" (showing first {_FIND_LIMIT})" if total > _FIND_LIMIT else ""
    return TurnResult(f"{' and '.join(parts)} matched{term_desc}{suffix}.", data=(rows + documents)[:_FIND_LIMIT])


_TRANSFER_HELP = (
    "Export - say what and how, e.g. \"export database as csv\", \"export database as sql\", "
    "\"export database as excel\", \"export schema as markdown\" (a design document with an ER diagram), "
    "\"export schema as er diagram\", \"export database with data\" (an MDC export file you can import into "
    "another instance), \"export table <name> as csv\", \"export database <name> as sql\". "
    "Import - click the paperclip next to the message box and pick a .md design document, .csv, .sql script, "
    "or an MDC export (.json / .dnadb.json). A .csv becomes a table in the current database; the others create a database."
)


def _handle_transfer_help(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    return TurnResult(_TRANSFER_HELP)


def _handle_export(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    name = command.database_name or state.current_database
    try:
        handle = manager.get(name)
    except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
        return TurnResult(str(exc))
    try:
        fmt, scope = resolve_export_options(command.export_format, command.export_scope, table=command.table_name)
    except ExportOptionError as exc:
        return TurnResult(str(exc))
    if command.table_name and not handle.schema_registry.has_collection(command.table_name):
        return TurnResult(f'Database "{name}" has no table "{command.table_name}".')
    if not handle.schema_registry.list_collections():
        return TurnResult(f'Database "{name}" has no tables yet, so there is nothing to export.')
    filename = export_filename(name, fmt, scope, command.table_name)
    url = f"/databases/{quote(name)}/export?format={fmt}&scope={scope}" + (f"&table={quote(command.table_name)}" if command.table_name else "")
    return TurnResult(f"Your export is ready: {filename}", download={"url": url, "filename": filename})



def _describe_plan(plan: ImportPlan, database: str, creating_database: bool) -> str:
    lines = [f'Here is the design - {"new " if creating_database else ""}database "{database}":']
    for table in plan.tables:
        assert table.columns is not None
        lines.append(f"  {table.name}: " + ", ".join(f"{c.name} {c.datatype}{'*' if c.required else ''}" for c in table.columns))
    if any(c.required for t in plan.tables for c in (t.columns or [])):
        lines.append("  (* required)")
    lines += [f"- {w}" for w in plan.warnings]
    lines.append("Create it? Reply yes to create it - anything else drops it.")
    return "\n".join(lines)


def _plan_refusal(exc: Exception) -> TurnResult:
    if isinstance(exc, ImportConflictError):
        return TurnResult("I can't create that: " + str(exc) + ".")
    return TurnResult(str(exc))


def _handle_design_schema(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    """A prompt or ER diagram described a schema: validate it, show it, and
    wait for "yes" - never create anything on the turn it is proposed."""
    if command.design_error is not None:
        return TurnResult(f"I couldn't read that ER diagram: {command.design_error}")
    plan = command.design_plan
    assert plan is not None
    database = resolve_database_name(plan, None, state.current_database, "schema")
    try:
        prepared = prepare_import(manager, plan, database)
    except (ImportFormatError, ImportDataError, ImportConflictError) as exc:
        return _plan_refusal(exc)
    for permission in sorted(prepared.required_permissions, key=lambda p: p.value):
        if (refusal := _lacks(state, permission)) is not None:
            return TurnResult(refusal)
    state.pending_schema = PendingSchema(plan=plan, database=database)
    return TurnResult(_describe_plan(plan, database, creating_database=not prepared.db_exists))


def apply_pending_schema(state: StorageConversationState, manager: DatabaseManager) -> TurnResult:
    """The "yes": validate again (the databases may have changed since the
    proposal) and create it."""
    pending, state.pending_schema = state.pending_schema, None
    assert pending is not None
    try:
        prepared = prepare_import(manager, pending.plan, pending.database)
    except (ImportFormatError, ImportDataError, ImportConflictError) as exc:
        return _plan_refusal(exc)
    for permission in sorted(prepared.required_permissions, key=lambda p: p.value):
        if (refusal := _lacks(state, permission)) is not None:
            return TurnResult(refusal)
    result = prepared.apply()
    if result.created_database:
        _switch_to(state, result.database)
    return TurnResult(result.summary())



def _compact(value: object) -> object:
    return value if isinstance(value, (str, int, float, bool)) or value is None else json.dumps(value, separators=(",", ":"))


def _change_summary(kind: str, payload: object) -> str:
    if kind == "base":
        return "starting state: " + (", ".join(payload) if isinstance(payload, dict) and payload else "(empty)")
    if isinstance(payload, list):
        parts = [f'{op.get("op", "?")} {".".join(op.get("path", []))}' for op in payload if isinstance(op, dict)]
        return "; ".join(parts) or "(no change)"
    return "(custom event)"


def _handle_stream(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    """Chat access to event-sourced agent-state streams in the current
    database. The same EventStore the REST API uses; state is only ever
    changed by appending an event, never edited in place."""
    assert command.stream_id is not None or command.intent == DatabaseIntent.STREAM_LIST
    if command.stream_error is not None:
        return TurnResult(command.stream_error)
    try:
        events = manager.events(state.current_database)
    except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
        return TurnResult(str(exc))
    database, stream = state.current_database, command.stream_id
    try:
        if command.intent == DatabaseIntent.STREAM_LIST:
            streams = events.list_streams()
            if not streams:
                return TurnResult(f'No streams in "{database}" yet. Try: create stream agent-1 with mood=calm, goal=idle')
            return TurnResult(f'{len(streams)} stream(s) in "{database}".', data=streams)
        assert stream is not None
        if command.intent == DatabaseIntent.STREAM_CREATE:
            view = events.create_stream(stream, command.stream_values, note="chat")
            return TurnResult(f'Created stream "{stream}" in "{database}" with {len(view.state)} field(s), at {view.ts.isoformat()}.')
        if command.intent == DatabaseIntent.STREAM_UPDATE:
            ops = [{"op": "set", "path": key.split("."), "value": value} for key, value in command.stream_values.items()]
            ops += [{"op": "unset", "path": key.split(".")} for key in command.stream_unset]
            event = events.append(stream, payload=ops, note="chat")
            return TurnResult(f'Recorded event {event.seq} on stream "{stream}" ({len(ops)} change(s)), at {event.ts.isoformat()}.')
        if command.intent == DatabaseIntent.STREAM_HISTORY:
            log = events.history(stream, limit=50)
            rows = [{"seq": e.seq, "time": e.ts.isoformat(), "kind": e.kind, "note": e.note or "", "change": _change_summary(e.kind, e.payload)} for e in log]
            return TurnResult(f'{len(log)} record(s) on stream "{stream}"' + (" (the latest 50)." if len(log) == 50 else "."), data=rows)
        at = parse_time(command.stream_at) if command.stream_at else None
        view = events.state_at(stream, at=at, seq=command.stream_seq)
    except ValueError as exc:  # an unreadable time
        return TurnResult(str(exc))
    except EventStoreError as exc:
        return TurnResult(str(exc))
    rows = [{"field": key, "value": _compact(value)} for key, value in view.state.items()]
    if at is not None:
        scope = f"as of {at.isoformat()}"
    elif command.stream_seq is not None:
        scope = f"after event {command.stream_seq}"
    else:
        scope = "latest state"
    detail = view.ts.isoformat() if command.stream_seq is not None else f"event {view.seq}, {view.ts.isoformat()}"
    head = f'Stream "{stream}" {scope} ({detail})'
    return TurnResult(f"{head}: {len(rows)} field(s)." if rows else f"{head}: the state is empty.", data=rows or None)



_HANDLERS = {
    DatabaseIntent.CREATE_DATABASE: _handle_create_database,
    DatabaseIntent.USE_DATABASE: _handle_use_database,
    DatabaseIntent.LIST_DATABASES: _handle_list_databases,
    DatabaseIntent.DESCRIBE_DATABASE: _handle_describe_database,
    DatabaseIntent.CREATE_TABLE: _handle_create_table,
    DatabaseIntent.DESCRIBE_TABLE: _handle_describe_table,
    DatabaseIntent.SHOW_DATA: _handle_show_data,
    DatabaseIntent.INSERT: _handle_insert,
    DatabaseIntent.FIND: _handle_find,
    DatabaseIntent.EXPORT: _handle_export,
    DatabaseIntent.TRANSFER_HELP: _handle_transfer_help,
    DatabaseIntent.DESIGN_SCHEMA: _handle_design_schema,
    DatabaseIntent.STREAM_LIST: _handle_stream,
    DatabaseIntent.STREAM_CREATE: _handle_stream,
    DatabaseIntent.STREAM_UPDATE: _handle_stream,
    DatabaseIntent.STREAM_SHOW: _handle_stream,
    DatabaseIntent.STREAM_HISTORY: _handle_stream,
}
