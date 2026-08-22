"""DocumentStore (CLAUDE-STORAGE.md section 20).

    Document
       |-- Original    (exact bytes, untouched)
       |-- Text         (extracted, when a parser exists)
       |-- Chunks        (paragraph-aware text chunks, for future full-text search)
       |-- Metadata
       `-- Embeddings   (not built - no embedding provider exists yet)

The original is stored completely separately from the derived record
(text/chunks/metadata) so retrieving one never requires touching the
other - "exact retrieval" (the original, byte-for-byte) and "full-text
search" (the derived record) are genuinely independent operations,
matching section 20's "without modifying the original document."
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from mdc.classification.data_type import DataType
from mdc.classification.metadata import build_profile
from mdc.model.object import MDCObject, generate_object_id
from mdc.objects.document_text import chunk_text, detect_document_format, extract_text
from mdc.objects.errors import DocumentNotFoundError
from mdc.storage_intelligence.analyzer import AccessProfile
from mdc.storage_intelligence.router import ObjectNotFoundError, StorageRouter
from mdc.storage_intelligence.strategy import StorageStrategyEngine

_RECORD_SUFFIX = ":record"


class DocumentMetadata(BaseModel):
    document_id: str
    format: str
    size_bytes: int
    checksum: str
    text_length: int | None = None
    chunk_count: int | None = None


class DocumentRecord(BaseModel):
    document_id: str
    metadata: DocumentMetadata
    text: str | None = None
    chunks: list[str] = Field(default_factory=list)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DocumentStore:
    def __init__(self, router: StorageRouter, strategy_engine: StorageStrategyEngine | None = None):
        self.router = router
        self.strategy_engine = strategy_engine or StorageStrategyEngine()

    def store_document(self, content: bytes, filename: str | None = None, access: AccessProfile | None = None) -> DocumentRecord:
        access = access or AccessProfile()
        document_id = generate_object_id(DataType.DOCUMENT)
        document_format = detect_document_format(content, filename)

        profile = build_profile(DataType.DOCUMENT, content)
        strategy = self.strategy_engine.select(profile, access)
        original = MDCObject(object_id=document_id, object_type=DataType.DOCUMENT, size=len(content), created_at=_utcnow(), updated_at=_utcnow())
        self.router.store(original, content, strategy)

        text = extract_text(content, document_format)
        chunks = chunk_text(text) if text else []
        metadata = DocumentMetadata(
            document_id=document_id,
            format=document_format,
            size_bytes=len(content),
            checksum=hashlib.sha256(content).hexdigest(),
            text_length=len(text) if text is not None else None,
            chunk_count=len(chunks) if chunks else None,
        )
        record = DocumentRecord(document_id=document_id, metadata=metadata, text=text, chunks=chunks)
        self._store_record(record, access)
        return record

    def _store_record(self, record: DocumentRecord, access: AccessProfile) -> None:
        payload = record.model_dump_json().encode("utf-8")
        profile = build_profile(DataType.DOCUMENT, payload)
        strategy = self.strategy_engine.select(profile, access)
        record_object = MDCObject(
            object_id=record.document_id + _RECORD_SUFFIX,
            object_type=DataType.DOCUMENT,
            size=len(payload),
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        self.router.store(record_object, payload, strategy)

    def retrieve_original(self, document_id: str) -> bytes:
        try:
            return self.router.retrieve(document_id)
        except ObjectNotFoundError:
            raise DocumentNotFoundError(document_id) from None

    def get_record(self, document_id: str) -> DocumentRecord:
        try:
            payload = self.router.retrieve(document_id + _RECORD_SUFFIX)
        except ObjectNotFoundError:
            raise DocumentNotFoundError(document_id) from None
        return DocumentRecord.model_validate_json(payload)
