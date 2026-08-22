"""ObjectService: the business logic behind the Universal Object API
(CLAUDE-STORAGE.md sections 34-35, 41).

Framework-independent on purpose - every method here is plain Python
against `StorageRouter`/the specialized stores, fully testable without
HTTP. `api/app.py` is a thin translation layer (HTTP verbs/status
codes) over exactly this; a future CLI/NLP integration (Phase J) can
call the same methods directly rather than duplicating logic per
transport (the "API and CLI share one engine" principle carried
through since the Data Engine work).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mdc.classification.classifier import classify_and_profile
from mdc.classification.data_type import DataType
from mdc.classification.metadata import build_profile
from mdc.index.object_index import IndexEntry
from mdc.model.object import MDCObject, generate_object_id
from mdc.models.extractor import InvalidSafetensorsError
from mdc.models.model_store import ModelStore
from mdc.objects.document import DocumentStore
from mdc.objects.errors import DocumentNotFoundError
from mdc.objects.image import ImageStore
from mdc.storage_intelligence.analyzer import AccessProfile
from mdc.storage_intelligence.router import ObjectNotFoundError, StorageRouter
from mdc.storage_intelligence.strategy import StorageStrategyEngine, StorageTier

_SNIPPET_RADIUS = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ObjectService:
    def __init__(self, router: StorageRouter, strategy_engine: StorageStrategyEngine | None = None):
        self.router = router
        self.strategy_engine = strategy_engine or StorageStrategyEngine()
        self.model_store = ModelStore(router, self.strategy_engine)
        self.image_store = ImageStore(router, self.strategy_engine)
        self.document_store = DocumentStore(router, self.strategy_engine)

    # -- upload (section 35: inspect -> classify -> profile -> strategy -> store -> index -> return) --

    def upload(self, content: bytes, filename: str | None = None, access: AccessProfile | None = None) -> dict[str, Any]:
        access = access or AccessProfile()
        profile = classify_and_profile(content, filename)

        if profile.data_type is DataType.AI_MODEL:
            try:
                manifest = self.model_store.store_model(content, filename or "model", access)
                return {
                    "object_id": manifest.model_id,
                    "type": DataType.AI_MODEL.value,
                    "representation": "TENSOR",
                    "tensor_count": manifest.tensor_count,
                    "total_parameters": manifest.total_parameters,
                }
            except InvalidSafetensorsError:
                pass  # classified as AI_MODEL but not actually parseable - fall through, store generically

        if profile.data_type is DataType.IMAGE:
            metadata = self.image_store.store_image(content, access)
            return {"object_id": metadata.image_id, "type": DataType.IMAGE.value, "format": metadata.format, "width": metadata.width, "height": metadata.height}

        if profile.data_type is DataType.DOCUMENT:
            record = self.document_store.store_document(content, filename, access)
            return {
                "object_id": record.document_id,
                "type": DataType.DOCUMENT.value,
                "format": record.metadata.format,
                "text_length": record.metadata.text_length,
                "chunk_count": record.metadata.chunk_count,
            }

        strategy = self.strategy_engine.select(profile, access)
        object_id = generate_object_id(profile.data_type)
        obj = MDCObject(object_id=object_id, object_type=profile.data_type, size=len(content), created_at=_utcnow(), updated_at=_utcnow())
        entry = self.router.store(obj, content, strategy)
        return self._entry_response(entry)

    # -- browsing (the Explorer-style UI's listing/search path) ---------------------

    def list_objects(
        self, object_type: DataType | None = None, storage_tier: StorageTier | None = None, *, include_companions: bool = False
    ) -> list[dict[str, Any]]:
        """Browsable, top-level objects only by default. `ModelStore`/
        `DocumentStore`/`ImageStore` each persist more than one physical
        object per logical upload (a model's tensor blocks; a document's
        `:record` companion carrying text/chunks; an image's `:metadata`
        companion) - those are implementation detail, not separate things
        a user uploaded, so a browsing UI shouldn't see them as peers of
        the object they actually asked for. `GET /models/{id}` still
        exposes a model's tensors explicitly, on purpose."""
        filters: dict[str, Any] = {}
        if object_type is not None:
            filters["object_type"] = object_type
        if storage_tier is not None:
            filters["storage_tier"] = storage_tier
        entries = self.router.index.search(**filters)
        if not include_companions:
            entries = [entry for entry in entries if _is_primary(entry)]
        return [self._entry_response(entry) for entry in entries]

    def search_documents(self, term: str) -> list[dict[str, Any]]:
        """Full-text search over stored documents' extracted text (section
        20). A real, if naive (linear-scan) search - it only searches
        documents whose format had a text extractor (section 20's
        documented gap: PDF/office documents have no `text` to search)."""
        term_lower = term.lower()
        results: list[dict[str, Any]] = []
        for entry in self.router.index.search(object_type=DataType.DOCUMENT):
            if not entry.object_id.endswith(":record"):
                continue
            document_id = entry.object_id[: -len(":record")]
            try:
                record = self.document_store.get_record(document_id)
            except DocumentNotFoundError:
                continue
            if record.text and term_lower in record.text.lower():
                results.append({"document_id": document_id, "format": record.metadata.format, "snippet": _snippet(record.text, term_lower)})
        return results

    # -- CRUD on the generic index/router path --------------------------------------

    def get_metadata(self, object_id: str) -> dict[str, Any]:
        entry = self.router.index.get(object_id)
        if entry is None:
            raise ObjectNotFoundError(object_id)
        return self._entry_response(entry)

    def read(self, object_id: str) -> bytes:
        return self.router.retrieve(object_id)

    def delete(self, object_id: str) -> None:
        self.router.delete(object_id)

    def replace(self, object_id: str, content: bytes, filename: str | None = None, access: AccessProfile | None = None) -> dict[str, Any]:
        """PUT semantics: re-classify and re-store fresh content under the
        same object_id, discarding whatever was there before."""
        if self.router.index.get(object_id) is not None:
            self.router.delete(object_id)
        access = access or AccessProfile()
        profile = classify_and_profile(content, filename)
        strategy = self.strategy_engine.select(profile, access)
        obj = MDCObject(object_id=object_id, object_type=profile.data_type, size=len(content), created_at=_utcnow(), updated_at=_utcnow())
        entry = self.router.store(obj, content, strategy)
        return self._entry_response(entry)

    def move(self, object_id: str, tier: StorageTier) -> dict[str, Any]:
        entry = self.router.move(object_id, tier)
        return self._entry_response(entry)

    def optimize(self, object_id: str, access: AccessProfile | None = None) -> dict[str, Any]:
        """Re-evaluate an object's storage tier against its *current*
        access pattern (section 34/39-40) and move it if warranted.
        Representation/compression are deterministic functions of the
        object's own bytes, so they don't change here without the content
        changing - only tier (which depends on access, the thing that
        actually varies over time) is re-derived. Never applies a lossy
        transformation automatically (section 42's safety rule).

        Profiles from `entry.object_type` (already recorded at write
        time), not from re-running the classifier on the raw bytes: a
        model manifest's stored JSON, re-classified standalone, reads as
        a generic DATABASE_RECORD with no hint it was ever an AI_MODEL -
        re-guessing the type here would silently change the object's
        mutability/tier defaults out from under it (found by hand: it
        moved a freshly-archived model manifest straight back out of
        ARCHIVE)."""
        entry = self.router.index.get(object_id)
        if entry is None:
            raise ObjectNotFoundError(object_id)
        access = access or AccessProfile()
        content = self.router.retrieve(object_id)
        profile = build_profile(entry.object_type, content)
        recommended_tier = self.strategy_engine.select(profile, access).storage_tier

        if recommended_tier == entry.storage_tier:
            return {"changed": False, "storage_tier": entry.storage_tier.value}
        moved = self.router.move(object_id, recommended_tier)
        return {"changed": True, "previous_tier": entry.storage_tier.value, "storage_tier": moved.storage_tier.value}

    def explain(self, object_id: str) -> dict[str, Any]:
        """section 41: every automatic storage decision must be
        explainable - built from the decision fields actually recorded at
        write time, never fabricated after the fact."""
        entry = self.router.index.get(object_id)
        if entry is None:
            raise ObjectNotFoundError(object_id)

        reasons: list[str] = []
        if entry.storage_tier is StorageTier.HOT:
            reasons.append("HOT (in-memory): selected for frequent access.")
        elif entry.storage_tier is StorageTier.ARCHIVE:
            reasons.append("ARCHIVE (DNA-encoded): explicit archival request on an immutable, unaccessed object.")
        elif entry.storage_tier is StorageTier.COLD:
            reasons.append("COLD: immutable and not yet accessed.")
        else:
            reasons.append("WARM: the default tier - no strong HOT/COLD/ARCHIVE signal yet.")

        if entry.compression.value != "NONE":
            reasons.append(f"Compressed with {entry.compression.value}: the content measured as compressible.")
        else:
            reasons.append("Stored uncompressed: the content did not measure as compressible.")

        return {
            "object_id": entry.object_id,
            "storage_tier": entry.storage_tier.value,
            "representation": entry.representation.value,
            "compression": entry.compression.value,
            "explanation": " ".join(reasons),
        }

    @staticmethod
    def _entry_response(entry: IndexEntry) -> dict[str, Any]:
        return {
            "object_id": entry.object_id,
            "type": entry.object_type.value,
            "representation": entry.representation.value,
            "compression": entry.compression.value,
            "storage_tier": entry.storage_tier.value,
            "size": entry.size,
            "checksum": entry.checksum,
        }


_COMPANION_SUFFIXES = (":record", ":metadata")


def _is_primary(entry: IndexEntry) -> bool:
    if entry.tensor_id is not None:
        return False  # a model/matrix block, not the manifest itself
    return not entry.object_id.endswith(_COMPANION_SUFFIXES)


def _snippet(text: str, term_lower: str) -> str:
    index = text.lower().find(term_lower)
    start = max(0, index - _SNIPPET_RADIUS)
    end = min(len(text), index + len(term_lower) + _SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix
