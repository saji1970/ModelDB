"""The Universal Object API (CLAUDE-STORAGE.md sections 34-35, 41) as
a FastAPI app.

`POST /objects` accepts a raw body (not multipart - no extra
dependency needed for that) with an optional `?filename=` query
parameter used only for classification hints (extension) and
metadata; the bytes themselves are always inspected first (section 6).

Every route is a thin translation of `ObjectService` - no decision
logic lives here, only HTTP verbs/status codes/error mapping.

CORS is wide open (`allow_origins=["*"]`) so a third-party NLU
integration (a RASA custom action, a hand-rolled chat UI, anything
else) can call this API from a different origin without a browser
CORS rejection getting in the way first - see the module docstring on
`/nlp/*` for why this is genuinely the point of this API surface, not
an oversight. Open CORS was never itself a security boundary against a
non-browser client, so every route except the browser UI's own HTML
page now requires a bearer token (`security/tokens.py`) - the actual
access control a third-party caller is expected to hold, issued via
`mdc token issue <name>` or the `MDC_API_TOKENS` env var.

Every route additionally declares the `Permission` (`security/roles.py`)
its token must carry: plain reads need `READ`; creating a table or
writing rows needs `CREATE_TABLE`/`WRITE`; creating a whole new
database needs `CREATE_DATABASE`. A token issued with a lesser role
(`mdc token issue <name> --role viewer`) authenticates fine but gets a
403 from a route it isn't permitted to call, instead of either being
rejected outright or silently getting more access than its role
implies.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from mdc.api.chat import ChatEngine
from mdc.classification.data_type import DataType
from mdc.conversation.db_interpreter import handle_database_command
from mdc.conversation.state import StorageConversationState
from mdc.databases.errors import (
    CannotDropDefaultDatabaseError,
    DatabaseAlreadyExistsError,
    DatabaseNotFoundError,
    InvalidDatabaseNameError,
    ProtectedTableError,
)
from mdc.engine.errors import RecordNotFoundError
from mdc.databases.manager import DatabaseManager, DEFAULT_DATABASE
from mdc.events import (
    ConcurrencyError,
    EventStoreError,
    StateNotYetError,
    StreamCorruptError,
    StreamExistsError,
    StreamNotFoundError,
)
from mdc.model.operation import CreateOperation, ReadOperation
from mdc.models.errors import ModelNotFoundError, TensorNotFoundError
from mdc.nlp.db_command import VALID_FIELD_TYPES, DatabaseCommand, DatabaseIntent
from mdc.portability.apply import ImportResult, prepare_import, resolve_database_name
from mdc.portability.plan import ExportOptionError, ImportConflictError, ImportDataError, ImportFormatError
from mdc.portability.readers import read_import, stem_of
from mdc.portability.writers import export_database
from mdc.schema.registry import CollectionNotFoundError, FieldSchema, SchemaError
from mdc.security.roles import Permission, Role, role_has
from mdc.security.tokens import TokenStore
from mdc.storage_intelligence.analyzer import AccessProfile
from mdc.storage_intelligence.router import DataIntegrityError, ObjectNotFoundError
from mdc.storage_intelligence.strategy import StorageTier

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_MAX_IMPORT_BYTES = 25 * 1024 * 1024
_LOCAL_UI_TOKEN_NAME = "local-browser-ui"


class CreateStreamRequest(BaseModel):
    state: dict[str, Any]
    note: str | None = None
    at: datetime | None = None


class AppendEventRequest(BaseModel):
    # Exactly one of: the full new state (the server works out the change)
    # or a ready-made codec payload.
    state: dict[str, Any] | None = None
    payload: Any = None
    note: str | None = None
    at: datetime | None = None
    expected_seq: int | None = None


class MoveRequest(BaseModel):
    tier: StorageTier


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class CreateDatabaseRequest(BaseModel):
    name: str


class CreateTableRequest(BaseModel):
    # field name -> type word ("string" | "decimal" | "integer" | "boolean" | "datetime")
    fields: dict[str, str]


class InsertRowRequest(BaseModel):
    values: dict[str, str]


class OptimizeRequest(BaseModel):
    # No automatic access tracking exists anywhere in this system (Phase
    # C onward never recorded real read/write history) - re-tiering
    # without these defaults to "never accessed," which would silently
    # undo a manual move to HOT. A caller who has real access stats
    # reports them here; omitting the body just re-applies policy
    # defaults to the object's current profile.
    access_frequency: float = 0.0
    mutation_frequency: float = 0.0


def create_app(manager: DatabaseManager, token_store: TokenStore | None = None) -> FastAPI:
    # /objects, /models, and /chat's default session all operate on the
    # DEFAULT database for backward compatibility - `POST /databases` and
    # the chat panel are how a caller reaches any other one (chat is
    # database-aware per-session via `state.current_database`; see
    # `conversation.interpreter`'s module docstring).
    service = manager.get(DEFAULT_DATABASE).object_service
    chat_engine = ChatEngine(manager)
    token_store = token_store or TokenStore()
    # Re-issued every process start (revoking any previous one first) -
    # the built-in UI is templated fresh on every `GET /` anyway, so
    # there's no state to lose, and it means a leaked/stale copy of the
    # served HTML stops working the moment the server restarts.
    token_store.revoke(_LOCAL_UI_TOKEN_NAME)
    local_ui_token = token_store.issue(_LOCAL_UI_TOKEN_NAME)

    app = FastAPI(title="MDC Universal Object API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],  # so a cross-origin UI can read an export's filename
    )

    def _not_found(exc: Exception) -> HTTPException:
        return HTTPException(status_code=404, detail=str(exc))

    def _authenticate(authorization: str | None = Header(default=None)) -> Role:
        token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
        record = token_store.resolve(token or "")
        if record is None:
            raise HTTPException(status_code=401, detail="Missing or invalid API token. Issue one with `mdc token issue <name>` or set MDC_API_TOKENS.")
        return record.role

    def _require(permission: Permission):
        """A FastAPI dependency factory: authenticates the bearer token,
        then 403s if its role doesn't carry `permission`. Every mutating
        or database-shaping route declares exactly the permission it
        needs rather than sharing one blanket "any valid token" check,
        so a restricted-role token authenticates but is still turned
        away from routes its role doesn't cover."""

        def _check(role: Role = Depends(_authenticate)) -> None:
            if not role_has(role, permission):
                raise HTTPException(status_code=403, detail=f'Token role {role.value!r} lacks the {permission.value!r} permission required for this endpoint.')

        return _check

    router = APIRouter()

    @app.get("/")
    def browser_ui() -> HTMLResponse:
        html = (_STATIC_DIR / "index.html").read_text()
        return HTMLResponse(html.replace("__MDC_LOCAL_UI_TOKEN__", local_ui_token))

    @router.get("/databases", dependencies=[Depends(_require(Permission.READ))])
    def list_databases() -> dict[str, Any]:
        return {"databases": manager.list_names()}

    @router.post("/databases", status_code=201, dependencies=[Depends(_require(Permission.CREATE_DATABASE))])
    def create_database(body: CreateDatabaseRequest) -> dict[str, Any]:
        try:
            manager.create(body.name)
        except (DatabaseAlreadyExistsError, InvalidDatabaseNameError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"name": body.name}

    @router.get("/databases/{name}/tables", dependencies=[Depends(_require(Permission.READ))])
    def list_database_tables(name: str) -> dict[str, Any]:
        try:
            handle = manager.get(name)
        except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
            raise _not_found(exc) from exc
        tables = handle.schema_registry.list_collections()
        return {
            "database": name,
            "tables": [
                {"name": t, "fields": len(handle.schema_registry.get_collection(t).fields)} for t in tables
            ],
        }

    @router.post("/databases/{name}/tables/{table}", status_code=201, dependencies=[Depends(_require(Permission.CREATE_TABLE))])
    def create_table(name: str, table: str, body: CreateTableRequest) -> dict[str, Any]:
        """Structured table creation - the REST counterpart to chat's
        "create table X with sku string, price decimal", for a caller
        (an external NLU's action handler, a custom UI) that already has
        typed field data and would rather not reconstruct that sentence
        syntax. Still schema-registry-only, same as chat - never raw SQL
        DDL, regardless of which door a request came through."""
        try:
            handle = manager.get(name)
        except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
            raise _not_found(exc) from exc
        if handle.schema_registry.has_collection(table):
            raise HTTPException(status_code=409, detail=f'Table "{table}" already exists in "{name}".')
        for field_name, type_word in body.fields.items():
            if type_word not in VALID_FIELD_TYPES:
                raise HTTPException(status_code=422, detail=f"Field {field_name!r} has unknown type {type_word!r}. Valid types: {sorted(VALID_FIELD_TYPES)}")
        fields = {n: FieldSchema(name=n, datatype=t, required=False) for n, t in body.fields.items()}
        handle.schema_registry.create_collection(table, fields)
        manager.persist_schema(name)
        return {"database": name, "table": table, "fields": list(body.fields)}

    @router.post("/databases/{name}/tables/{table}/rows", status_code=201, dependencies=[Depends(_require(Permission.WRITE))])
    def insert_row(name: str, table: str, body: InsertRowRequest) -> dict[str, Any]:
        """Structured row insertion - the REST counterpart to chat's
        "insert into X sku=ABC123, price=9.99"."""
        try:
            handle = manager.get(name)
        except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
            raise _not_found(exc) from exc
        try:
            result = handle.engine.create(CreateOperation(collection=table, data=body.values))
        except CollectionNotFoundError as exc:
            raise _not_found(exc) from exc
        except SchemaError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        record = result.records[0]
        return {"database": name, "table": table, "record_id": record.record_id, **record.fields}

    @router.get("/find", dependencies=[Depends(_require(Permission.READ))])
    def find(
        q: str | None = None,
        min: float | None = None,
        max: float | None = None,
        database: str | None = None,
        table: str | None = None,
    ) -> dict[str, Any]:
        """The structured REST form of chat's universal `find` - searches
        every database's tables and documents at once (or a scoped
        subset, if `database`/`table` are given), with the exact same
        clause semantics `find <text> under/over <amount>` has in chat.
        Reuses that same handler rather than a second implementation -
        see `conversation.db_interpreter._handle_find`."""
        command = DatabaseCommand(
            DatabaseIntent.FIND, database_name=database, table_name=table, search_term=q, min_value=min, max_value=max
        )
        result = handle_database_command(StorageConversationState(), manager, command)
        return {"message": result.message, "results": result.data or []}

    @router.get("/databases/{name}/tables/{table}", dependencies=[Depends(_require(Permission.READ))])
    def get_database_table(name: str, table: str) -> dict[str, Any]:
        try:
            handle = manager.get(name)
        except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
            raise _not_found(exc) from exc
        try:
            collection = handle.schema_registry.get_collection(table)
        except CollectionNotFoundError as exc:
            raise _not_found(exc) from exc
        return {
            "database": name,
            "table": table,
            "fields": [{"name": f.name, "type": f.datatype, "required": f.required} for f in collection.fields.values()],
        }


    @router.delete("/databases/{name}", dependencies=[Depends(_require(Permission.DROP))])
    def drop_database(name: str, confirm: str | None = None) -> dict[str, Any]:
        """Permanently delete a database and everything in it - tables, rows,
        objects, streams. Irreversible, so the request must repeat the
        database's name as `?confirm=`. The default database is refused."""
        if name == DEFAULT_DATABASE:
            raise HTTPException(status_code=409, detail=str(CannotDropDefaultDatabaseError()))
        try:
            held = manager.summarize(name)
        except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
            raise _not_found(exc) from exc
        if confirm != name:
            raise HTTPException(
                status_code=422,
                detail=f'This permanently deletes database "{name}" ({len(held["tables"])} table(s), {held["rows"]} row(s), '
                f'{held["objects"]} object(s), {held["streams"]} stream(s)). Repeat the request with ?confirm={name} to proceed.',
            )
        manager.drop(name)
        return {"dropped": name, "tables": len(held["tables"]), "rows": held["rows"], "objects": held["objects"], "streams": held["streams"]}

    @router.delete("/databases/{name}/tables/{table}", dependencies=[Depends(_require(Permission.DROP))])
    def drop_database_table(name: str, table: str, confirm: str | None = None) -> dict[str, Any]:
        """Permanently delete a table and all its rows. Irreversible, so the
        request must repeat the table's name as `?confirm=`."""
        try:
            handle = manager.get(name)
        except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
            raise _not_found(exc) from exc
        if not handle.schema_registry.has_collection(table):
            raise _not_found(CollectionNotFoundError(table))
        if manager.is_protected_table(name, table):
            raise HTTPException(status_code=409, detail=str(ProtectedTableError(table)))
        rows = handle.engine.count_records(table)
        if confirm != table:
            raise HTTPException(
                status_code=422,
                detail=f'This permanently deletes table "{table}" in "{name}" and its {rows} row(s). Repeat the request with ?confirm={table} to proceed.',
            )
        manager.drop_table(name, table)
        return {"database": name, "dropped": table, "rows_deleted": rows}

    @router.delete("/databases/{name}/tables/{table}/rows/{record_id}", status_code=204, dependencies=[Depends(_require(Permission.WRITE))])
    def delete_database_row(name: str, table: str, record_id: str) -> Response:
        try:
            handle = manager.get(name)
        except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
            raise _not_found(exc) from exc
        try:
            handle.engine.delete_record(table, record_id)
        except (CollectionNotFoundError, RecordNotFoundError) as exc:
            raise _not_found(exc) from exc
        return Response(status_code=204)

    @router.get("/databases/{name}/tables/{table}/rows", dependencies=[Depends(_require(Permission.READ))])
    def get_database_table_rows(name: str, table: str) -> dict[str, Any]:
        try:
            handle = manager.get(name)
        except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
            raise _not_found(exc) from exc
        try:
            result = handle.engine.read(ReadOperation(collection=table, filters=[]))
        except CollectionNotFoundError as exc:
            raise _not_found(exc) from exc
        return {
            "database": name,
            "table": table,
            "rows": [{"record_id": record.record_id, **record.fields} for record in result.records],
            "count": result.count,
        }


    async def _import_body(request: Request) -> bytes:
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > _MAX_IMPORT_BYTES:
            raise HTTPException(status_code=413, detail=f"The file is larger than the {_MAX_IMPORT_BYTES // (1024 * 1024)} MB import limit.")
        body = await request.body()
        if len(body) > _MAX_IMPORT_BYTES:
            raise HTTPException(status_code=413, detail=f"The file is larger than the {_MAX_IMPORT_BYTES // (1024 * 1024)} MB import limit.")
        if not body:
            raise HTTPException(status_code=422, detail="The file is empty.")
        return body

    def _run_import(filename: str, content: bytes, role: Role, explicit_database: str | None, session_database: str | None) -> ImportResult:
        """Parse -> validate everything -> check the caller's role against
        what the import would do -> only then write. Nothing is ever
        executed as SQL: a script is parsed into tables and rows."""
        try:
            plan = read_import(filename, content)
            database = resolve_database_name(plan, explicit_database, session_database, stem_of(filename))
            prepared = prepare_import(manager, plan, database)
        except (ImportFormatError, ImportDataError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ImportConflictError as exc:
            raise HTTPException(status_code=409, detail="Nothing was imported - " + str(exc)) from exc
        missing = sorted(p.value for p in prepared.required_permissions if not role_has(role, p))
        if missing:
            raise HTTPException(status_code=403, detail=f"Token role {role.value!r} lacks the {', '.join(missing)!r} permission this import needs.")
        return prepared.apply()

    @router.get("/databases/{name}/export", dependencies=[Depends(_require(Permission.READ))])
    def export_database_file(name: str, format: str = "dnadb", scope: str | None = None, table: str | None = None) -> Response:
        """Download a database as csv (one file, or a zip of one per
        table), sql, xlsx, md (a design document with an ER diagram), er
        (the Mermaid source of that diagram), or dnadb (an MDC export
        file another instance can import). `scope` is schema, data or
        all; each format has a sensible default."""
        try:
            handle = manager.get(name)
        except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
            raise _not_found(exc) from exc
        try:
            file = export_database(handle, format, scope, table)
        except ExportOptionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CollectionNotFoundError as exc:
            raise _not_found(exc) from exc
        return Response(content=file.content, media_type=file.media_type, headers={"Content-Disposition": f'attachment; filename="{file.filename}"'})

    @router.post("/databases/import", status_code=201)
    async def import_database_file(request: Request, filename: str, database: str | None = None, role: Role = Depends(_authenticate)) -> dict[str, Any]:
        """Create a database / tables from an uploaded file (raw request
        body, `?filename=` picks the format by extension): .md design
        document, .csv, .sql script, or an MDC export (.json /
        .dnadb.json). `database` names the target; without it a file
        names its own database and a CSV goes to "default"."""
        result = _run_import(filename, await _import_body(request), role, database, None)
        return {"message": result.summary(), "database": result.database, "created_database": result.created_database,
                "tables_created": result.tables_created, "rows_inserted": result.rows_inserted, "warnings": result.warnings}

    @router.post("/chat/attach")
    async def chat_attach(request: Request, filename: str, session_id: str = "default", role: Role = Depends(_authenticate)) -> dict[str, Any]:
        """The chat window's paperclip: the same import, except a CSV goes
        into the database this chat session is working in, and a database
        the import creates becomes the session's current one."""
        result = _run_import(filename, await _import_body(request), role, None, chat_engine.session_database(session_id))
        if result.created_database:
            chat_engine.switch_session_database(session_id, result.database)
        return {"message": result.summary(), "database": result.database, "created_database": result.created_database}


    def _events(name: str):
        try:
            return manager.events(name)
        except (DatabaseNotFoundError, InvalidDatabaseNameError) as exc:
            raise _not_found(exc) from exc

    def _stream_http_error(exc: EventStoreError) -> HTTPException:
        if isinstance(exc, (StreamNotFoundError, StateNotYetError)):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, (StreamExistsError, ConcurrencyError)):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, StreamCorruptError):
            return HTTPException(status_code=500, detail=str(exc))
        return HTTPException(status_code=422, detail=str(exc))

    @router.get("/databases/{name}/streams", dependencies=[Depends(_require(Permission.READ))])
    def list_streams(name: str) -> dict[str, Any]:
        return {"database": name, "streams": _events(name).list_streams()}

    @router.post("/databases/{name}/streams/{stream}", status_code=201, dependencies=[Depends(_require(Permission.WRITE))])
    def create_stream(name: str, stream: str, body: CreateStreamRequest) -> dict[str, Any]:
        """Start an event-sourced stream (agent state) with its base state.
        Everything stored - state and timestamps - is sealed with a key
        derived for this stream; see `events.store`."""
        try:
            view = _events(name).create_stream(stream, body.state, note=body.note, ts=body.at)
        except EventStoreError as exc:
            raise _stream_http_error(exc) from exc
        return {"database": name, "stream": stream, "seq": view.seq, "ts": view.ts.isoformat()}

    @router.post("/databases/{name}/streams/{stream}/events", status_code=201, dependencies=[Depends(_require(Permission.WRITE))])
    def append_stream_event(name: str, stream: str, body: AppendEventRequest) -> dict[str, Any]:
        """Record one change - append only, never an update in place. Send
        the full new `state` (the change is worked out) or a codec `payload`;
        `expected_seq` makes the write fail if someone else appended first."""
        try:
            event = _events(name).append(
                stream, state=body.state, payload=body.payload, note=body.note, ts=body.at, expected_seq=body.expected_seq
            )
        except EventStoreError as exc:
            raise _stream_http_error(exc) from exc
        return {"database": name, "stream": stream, "seq": event.seq, "ts": event.ts.isoformat(), "payload": event.payload}

    @router.get("/databases/{name}/streams/{stream}", dependencies=[Depends(_require(Permission.READ))])
    def get_stream_state(name: str, stream: str, at: datetime | None = None, seq: int | None = None) -> dict[str, Any]:
        """The state rebuilt as of time `at` (ISO 8601; no zone means UTC) or
        just after event `seq`; with neither, the latest."""
        try:
            view = _events(name).state_at(stream, at=at, seq=seq)
        except EventStoreError as exc:
            raise _stream_http_error(exc) from exc
        return {"database": name, "stream": stream, "state": view.state, "seq": view.seq, "ts": view.ts.isoformat()}

    @router.get("/databases/{name}/streams/{stream}/history", dependencies=[Depends(_require(Permission.READ))])
    def get_stream_history(name: str, stream: str, since: datetime | None = None, until: datetime | None = None, limit: int | None = None) -> dict[str, Any]:
        try:
            events = _events(name).history(stream, since=since, until=until, limit=limit)
        except EventStoreError as exc:
            raise _stream_http_error(exc) from exc
        return {
            "database": name,
            "stream": stream,
            "events": [{"seq": e.seq, "ts": e.ts.isoformat(), "kind": e.kind, "note": e.note, "payload": e.payload} for e in events],
        }

    @router.delete("/databases/{name}/streams/{stream}", status_code=204, dependencies=[Depends(_require(Permission.DROP))])
    def delete_stream(name: str, stream: str) -> Response:
        try:
            removed = _events(name).delete_stream(stream)
        except EventStoreError as exc:
            raise _stream_http_error(exc) from exc
        if not removed:
            raise HTTPException(status_code=404, detail=f'No stream named "{stream}".')
        return Response(status_code=204)

    @router.get("/objects", dependencies=[Depends(_require(Permission.READ))])
    def list_objects(type: str | None = None, tier: str | None = None) -> dict[str, Any]:
        object_type = DataType(type) if type else None
        storage_tier = StorageTier(tier) if tier else None
        return {"objects": service.list_objects(object_type=object_type, storage_tier=storage_tier)}

    @router.post("/chat", dependencies=[Depends(_require(Permission.WRITE))])
    def chat(body: ChatRequest, role: Role = Depends(_authenticate)) -> dict[str, Any]:
        reply = chat_engine.handle(body.session_id, body.message, role=role)
        return {"message": reply.message, "data": reply.data, "download": reply.download}

    @router.post("/objects", status_code=201, dependencies=[Depends(_require(Permission.WRITE))])
    async def upload_object(request: Request, filename: str | None = None) -> dict[str, Any]:
        content = await request.body()
        return service.upload(content, filename=filename)

    @router.get("/objects/{object_id}", dependencies=[Depends(_require(Permission.READ))])
    def get_object_metadata(object_id: str) -> dict[str, Any]:
        try:
            return service.get_metadata(object_id)
        except ObjectNotFoundError as exc:
            raise _not_found(exc) from exc

    @router.post("/objects/{object_id}/read", dependencies=[Depends(_require(Permission.READ))])
    def read_object(object_id: str) -> Response:
        try:
            content = service.read(object_id)
        except ObjectNotFoundError as exc:
            raise _not_found(exc) from exc
        except DataIntegrityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(content=content, media_type="application/octet-stream")

    @router.put("/objects/{object_id}", dependencies=[Depends(_require(Permission.WRITE))])
    async def replace_object(object_id: str, request: Request, filename: str | None = None) -> dict[str, Any]:
        content = await request.body()
        return service.replace(object_id, content, filename=filename)

    @router.delete("/objects/{object_id}", status_code=204, dependencies=[Depends(_require(Permission.WRITE))])
    def delete_object(object_id: str) -> Response:
        try:
            service.delete(object_id)
        except ObjectNotFoundError as exc:
            raise _not_found(exc) from exc
        return Response(status_code=204)

    @router.post("/objects/{object_id}/move", dependencies=[Depends(_require(Permission.WRITE))])
    def move_object(object_id: str, body: MoveRequest) -> dict[str, Any]:
        try:
            metadata = service.get_metadata(object_id)

            if metadata["type"] == DataType.AI_MODEL.value:
                # A model is its manifest plus one index entry per tensor
                # block, each independently tiered - see
                # `ModelStore.move_model`'s docstring for why a plain
                # `service.move()` would silently leave the actual weight
                # bytes behind.
                moved = service.model_store.move_model(object_id, body.tier)
                return {"object_id": object_id, "storage_tier": body.tier.value, "objects_moved": moved}

            return service.move(object_id, body.tier)
        except (ObjectNotFoundError, ModelNotFoundError) as exc:
            # `move_model` confirms the model exists via `get_manifest`
            # first, which raises `ModelNotFoundError` (not
            # `ObjectNotFoundError`) if its content can't be read back -
            # e.g. it's indexed at HOT but that tier's in-memory backend
            # lost it across a restart.
            raise _not_found(exc) from exc

    @router.post("/objects/{object_id}/optimize", dependencies=[Depends(_require(Permission.WRITE))])
    def optimize_object(object_id: str, body: OptimizeRequest = OptimizeRequest()) -> dict[str, Any]:
        access = AccessProfile(access_frequency=body.access_frequency, mutation_frequency=body.mutation_frequency)
        try:
            return service.optimize(object_id, access=access)
        except ObjectNotFoundError as exc:
            raise _not_found(exc) from exc

    @router.get("/objects/{object_id}/strategy", dependencies=[Depends(_require(Permission.READ))])
    def explain_object(object_id: str) -> dict[str, Any]:
        try:
            return service.explain(object_id)
        except ObjectNotFoundError as exc:
            raise _not_found(exc) from exc

    @router.get("/models/{model_id}", dependencies=[Depends(_require(Permission.READ))])
    def get_model_manifest(model_id: str) -> dict[str, Any]:
        try:
            return service.model_store.get_manifest(model_id).model_dump(mode="json")
        except ModelNotFoundError as exc:
            raise _not_found(exc) from exc

    @router.get("/models/{model_id}/tensors/{tensor_name}", dependencies=[Depends(_require(Permission.READ))])
    def get_tensor(model_id: str, tensor_name: str) -> Response:
        try:
            data = service.model_store.retrieve_tensor(model_id, tensor_name)
        except (ModelNotFoundError, TensorNotFoundError) as exc:
            raise _not_found(exc) from exc
        return Response(content=data, media_type="application/octet-stream")

    app.include_router(router)
    return app
