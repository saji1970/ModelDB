"""StorageBackend interface (CLAUDE.md section 53).

Every storage backend (DuckDBStore, MatrixStore, DNAStore) implements this
same block-addressed interface so the StorageRouter (section 54, phase 6+)
can move a payload between backends without callers caring which one is
underneath.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


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
