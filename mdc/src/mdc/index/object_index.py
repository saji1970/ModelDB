"""ObjectIndex (CLAUDE-STORAGE.md section 32).

The catalog stays centralized on one always-queryable backend even
though the objects it describes are scattered across whichever
per-tier backend `StorageRouter` chose for each one (section 44) -
exactly how a real tiered system works: you don't have to know which
physical store an object lives on to look it up.

Built on the same generic block-addressed `StorageBackend` every other
persisted thing in this project uses (section 32: "may initially be
implemented using DuckDB") rather than a bespoke table, so it needs no
schema migration of its own.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from mdc.classification.data_type import DataType
from mdc.storage.interface import StorageBackend
from mdc.storage_intelligence.strategy_types import CompressionAlgorithm, Representation, StorageTier

_INDEX_PREFIX = "__index__:"


class IndexEntry(BaseModel):
    object_id: str
    object_type: DataType
    storage_backend: str
    storage_tier: StorageTier
    location: str
    size: int
    # Physical bytes actually written after compression - None when no
    # compression was applied. Real, measured (Phase F), not the
    # theoretical estimate `storage_intelligence.optimizer` computes.
    compressed_size: int | None = None
    checksum: str
    compression: CompressionAlgorithm
    representation: Representation
    tensor_id: str | None = None
    tensor_name: str | None = None
    block_id: str | None = None
    indexed_at: datetime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ObjectIndex:
    def __init__(self, storage: StorageBackend):
        self.storage = storage

    def register(self, entry: IndexEntry) -> None:
        metadata = {
            "object_id": entry.object_id,
            "object_type": entry.object_type.value,
            "storage_tier": entry.storage_tier.value,
        }
        if entry.tensor_id is not None:
            metadata["tensor_id"] = entry.tensor_id
        if entry.tensor_name is not None:
            metadata["tensor_name"] = entry.tensor_name
        self.storage.put(
            _INDEX_PREFIX + entry.object_id,
            entry.model_dump_json().encode("utf-8"),
            metadata=metadata,
        )

    def get(self, object_id: str) -> IndexEntry | None:
        try:
            payload = self.storage.get(_INDEX_PREFIX + object_id)
        except KeyError:
            return None
        return IndexEntry.model_validate_json(payload)

    def delete(self, object_id: str) -> None:
        self.storage.delete(_INDEX_PREFIX + object_id)

    def search(self, **filters: Any) -> list[IndexEntry]:
        normalized = {key: (value.value if hasattr(value, "value") else value) for key, value in filters.items()}
        block_ids = self.storage.search(**normalized)
        entries = []
        for block_id in block_ids:
            if not block_id.startswith(_INDEX_PREFIX):
                continue
            entries.append(IndexEntry.model_validate_json(self.storage.get(block_id)))
        return entries
