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
from mdc.conversation.state import StorageConversationState
from mdc.databases.errors import DatabaseAlreadyExistsError, DatabaseNotFoundError, InvalidDatabaseNameError
from mdc.databases.manager import DatabaseManager
from mdc.model.operation import CreateOperation, ReadOperation
from mdc.nlp.db_command import DatabaseCommand, DatabaseIntent
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


def _handle_create_database(state: StorageConversationState, manager: DatabaseManager, command: DatabaseCommand) -> TurnResult:
    assert command.database_name is not None
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
}
