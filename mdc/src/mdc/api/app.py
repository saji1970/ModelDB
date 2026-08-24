"""The Universal Object API (CLAUDE-STORAGE.md sections 34-35, 41) as
a FastAPI app.

`POST /objects` accepts a raw body (not multipart - no extra
dependency needed for that) with an optional `?filename=` query
parameter used only for classification hints (extension) and
metadata; the bytes themselves are always inspected first (section 6).

Every route is a thin translation of `ObjectService` - no decision
logic lives here, only HTTP verbs/status codes/error mapping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from mdc.api.chat import ChatEngine
from mdc.classification.data_type import DataType
from mdc.databases.errors import DatabaseAlreadyExistsError, DatabaseNotFoundError, InvalidDatabaseNameError
from mdc.databases.manager import DatabaseManager, DEFAULT_DATABASE
from mdc.model.operation import ReadOperation
from mdc.models.errors import ModelNotFoundError, TensorNotFoundError
from mdc.schema.registry import CollectionNotFoundError
from mdc.storage_intelligence.analyzer import AccessProfile
from mdc.storage_intelligence.router import DataIntegrityError, ObjectNotFoundError
from mdc.storage_intelligence.strategy import StorageTier

_STATIC_DIR = Path(__file__).resolve().parent / "static"


class MoveRequest(BaseModel):
    tier: StorageTier


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class CreateDatabaseRequest(BaseModel):
    name: str


class OptimizeRequest(BaseModel):
    # No automatic access tracking exists anywhere in this system (Phase
    # C onward never recorded real read/write history) - re-tiering
    # without these defaults to "never accessed," which would silently
    # undo a manual move to HOT. A caller who has real access stats
    # reports them here; omitting the body just re-applies policy
    # defaults to the object's current profile.
    access_frequency: float = 0.0
    mutation_frequency: float = 0.0


def create_app(manager: DatabaseManager) -> FastAPI:
    # /objects, /models, and /chat's default session all operate on the
    # DEFAULT database for backward compatibility - `POST /databases` and
    # the chat panel are how a caller reaches any other one (chat is
    # database-aware per-session via `state.current_database`; see
    # `conversation.interpreter`'s module docstring).
    service = manager.get(DEFAULT_DATABASE).object_service
    chat_engine = ChatEngine(manager)
    app = FastAPI(title="MDC Universal Object API")

    def _not_found(exc: Exception) -> HTTPException:
        return HTTPException(status_code=404, detail=str(exc))

    @app.get("/")
    def browser_ui() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/databases")
    def list_databases() -> dict[str, Any]:
        return {"databases": manager.list_names()}

    @app.post("/databases", status_code=201)
    def create_database(body: CreateDatabaseRequest) -> dict[str, Any]:
        try:
            manager.create(body.name)
        except (DatabaseAlreadyExistsError, InvalidDatabaseNameError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"name": body.name}

    @app.get("/databases/{name}/tables")
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

    @app.get("/databases/{name}/tables/{table}")
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

    @app.get("/databases/{name}/tables/{table}/rows")
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

    @app.get("/objects")
    def list_objects(type: str | None = None, tier: str | None = None) -> dict[str, Any]:
        object_type = DataType(type) if type else None
        storage_tier = StorageTier(tier) if tier else None
        return {"objects": service.list_objects(object_type=object_type, storage_tier=storage_tier)}

    @app.post("/chat")
    def chat(body: ChatRequest) -> dict[str, Any]:
        reply = chat_engine.handle(body.session_id, body.message)
        return {"message": reply.message, "data": reply.data}

    @app.post("/objects", status_code=201)
    async def upload_object(request: Request, filename: str | None = None) -> dict[str, Any]:
        content = await request.body()
        return service.upload(content, filename=filename)

    @app.get("/objects/{object_id}")
    def get_object_metadata(object_id: str) -> dict[str, Any]:
        try:
            return service.get_metadata(object_id)
        except ObjectNotFoundError as exc:
            raise _not_found(exc) from exc

    @app.post("/objects/{object_id}/read")
    def read_object(object_id: str) -> Response:
        try:
            content = service.read(object_id)
        except ObjectNotFoundError as exc:
            raise _not_found(exc) from exc
        except DataIntegrityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(content=content, media_type="application/octet-stream")

    @app.put("/objects/{object_id}")
    async def replace_object(object_id: str, request: Request, filename: str | None = None) -> dict[str, Any]:
        content = await request.body()
        return service.replace(object_id, content, filename=filename)

    @app.delete("/objects/{object_id}", status_code=204)
    def delete_object(object_id: str) -> Response:
        try:
            service.delete(object_id)
        except ObjectNotFoundError as exc:
            raise _not_found(exc) from exc
        return Response(status_code=204)

    @app.post("/objects/{object_id}/move")
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

    @app.post("/objects/{object_id}/optimize")
    def optimize_object(object_id: str, body: OptimizeRequest = OptimizeRequest()) -> dict[str, Any]:
        access = AccessProfile(access_frequency=body.access_frequency, mutation_frequency=body.mutation_frequency)
        try:
            return service.optimize(object_id, access=access)
        except ObjectNotFoundError as exc:
            raise _not_found(exc) from exc

    @app.get("/objects/{object_id}/strategy")
    def explain_object(object_id: str) -> dict[str, Any]:
        try:
            return service.explain(object_id)
        except ObjectNotFoundError as exc:
            raise _not_found(exc) from exc

    @app.get("/models/{model_id}")
    def get_model_manifest(model_id: str) -> dict[str, Any]:
        try:
            return service.model_store.get_manifest(model_id).model_dump(mode="json")
        except ModelNotFoundError as exc:
            raise _not_found(exc) from exc

    @app.get("/models/{model_id}/tensors/{tensor_name}")
    def get_tensor(model_id: str, tensor_name: str) -> Response:
        try:
            data = service.model_store.retrieve_tensor(model_id, tensor_name)
        except (ModelNotFoundError, TensorNotFoundError) as exc:
            raise _not_found(exc) from exc
        return Response(content=data, media_type="application/octet-stream")

    return app
