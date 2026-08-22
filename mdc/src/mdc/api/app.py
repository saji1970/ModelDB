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
from mdc.api.service import ObjectService
from mdc.classification.data_type import DataType
from mdc.models.errors import ModelNotFoundError, TensorNotFoundError
from mdc.storage_intelligence.analyzer import AccessProfile
from mdc.storage_intelligence.router import DataIntegrityError, ObjectNotFoundError, StorageRouter
from mdc.storage_intelligence.strategy import StorageTier

_STATIC_DIR = Path(__file__).resolve().parent / "static"


class MoveRequest(BaseModel):
    tier: StorageTier


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class OptimizeRequest(BaseModel):
    # No automatic access tracking exists anywhere in this system (Phase
    # C onward never recorded real read/write history) - re-tiering
    # without these defaults to "never accessed," which would silently
    # undo a manual move to HOT. A caller who has real access stats
    # reports them here; omitting the body just re-applies policy
    # defaults to the object's current profile.
    access_frequency: float = 0.0
    mutation_frequency: float = 0.0


def create_app(router: StorageRouter) -> FastAPI:
    service = ObjectService(router)
    chat_engine = ChatEngine(service)
    app = FastAPI(title="MDC Universal Object API")

    def _not_found(exc: Exception) -> HTTPException:
        return HTTPException(status_code=404, detail=str(exc))

    @app.get("/")
    def browser_ui() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

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
            return service.move(object_id, body.tier)
        except ObjectNotFoundError as exc:
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
