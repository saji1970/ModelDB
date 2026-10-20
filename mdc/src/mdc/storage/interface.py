"""StorageBackend interface (CLAUDE.md section 53).

Every storage backend (DuckDBStore, MatrixStore, DNAStore) implements this
same block-addressed interface so the StorageRouter (section 54, phase 6+)
can move a payload between backends without callers caring which one is
underneath.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable


class StorageBackend(ABC):
    """Block-addressed storage contract shared by all backends."""

    @abstractmethod
    def put(self, block_id: str, payload: bytes, metadata: dict[str, Any] | None = None) -> str:
        """Store `payload` under `block_id`. Returns the checksum."""

    @abstractmethod
    def get(self, block_id: str) -> bytes:
        """Retrieve the payload stored under `block_id`."""

    @abstractmethod
    def exists(self, block_id: str) -> bool:
        """Return True if `block_id` is present in this backend."""

    @abstractmethod
    def delete(self, block_id: str) -> None:
        """Remove `block_id` from this backend."""

    @abstractmethod
    def metadata(self, block_id: str) -> dict[str, Any]:
        """Return stored metadata (including checksum) for `block_id`."""

    @abstractmethod
    def search(self, **filters: Any) -> list[str]:
        """Return block_ids matching the given metadata filters."""


@runtime_checkable
class RecordBackend(Protocol):
    """Optional capability: a backend that keeps the Data Engine's records
    itself, so the engine can read a collection with one query, find a record
    by id, and let the backend narrow a filter, instead of fetching one block
    per record and checking each in Python. `DuckDBStore` has it. A backend
    without it still works: the engine falls back to blocks.

    A row is (record_id, version, created_at, updated_at, fields_json,
    checksum_bytes). `record_query` may return MORE than the matches (the
    engine re-checks every filter); it must never return fewer.
    """

    def record_insert_many(self, rows: list[tuple], *, replace: bool = False) -> None: ...
    def record_get(self, collection: str, record_id: str) -> tuple | None: ...
    def record_exists(self, collection: str, record_id: str) -> bool: ...
    def record_existing_ids(self, collection: str, record_ids: list[str]) -> set[str]: ...
    def record_count(self, collection: str) -> int: ...
    def record_delete(self, collection: str, record_id: str) -> bool: ...
    def record_delete_collection(self, collection: str) -> int: ...
    def record_query(self, collection: str, filters: list, limit: int | None = None) -> tuple[list[tuple], bool]: ...
