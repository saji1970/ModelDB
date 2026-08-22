"""In-process memory `StorageBackend` (CLAUDE-STORAGE.md section 10).

The real second backend `StorageRouter`'s HOT tier needs to make tier
routing an actual consequence rather than a label everything shares
with WARM/COLD/ARCHIVE. Not persisted across process restarts by
design - that's the point of HOT/RAM versus a disk-backed tier.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from mdc.storage.interface import StorageBackend


class MemoryStorageBackend(StorageBackend):
    def __init__(self) -> None:
        self._blocks: dict[str, bytes] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._checksums: dict[str, str] = {}
        self._created_at: dict[str, datetime] = {}

    def put(self, block_id: str, payload: bytes, metadata: dict[str, Any] | None = None) -> str:
        checksum = hashlib.sha256(payload).hexdigest()
        self._blocks[block_id] = payload
        self._metadata[block_id] = dict(metadata) if metadata else {}
        self._checksums[block_id] = checksum
        self._created_at.setdefault(block_id, datetime.now(timezone.utc))
        return checksum

    def get(self, block_id: str) -> bytes:
        if block_id not in self._blocks:
            raise KeyError(f"No block found for block_id={block_id!r}")
        return self._blocks[block_id]

    def exists(self, block_id: str) -> bool:
        return block_id in self._blocks

    def delete(self, block_id: str) -> None:
        self._blocks.pop(block_id, None)
        self._metadata.pop(block_id, None)
        self._checksums.pop(block_id, None)
        self._created_at.pop(block_id, None)

    def metadata(self, block_id: str) -> dict[str, Any]:
        if block_id not in self._blocks:
            raise KeyError(f"No block found for block_id={block_id!r}")
        return {
            "metadata": self._metadata[block_id],
            "checksum": self._checksums[block_id],
            "created_at": self._created_at[block_id],
        }

    def search(self, **filters: Any) -> list[str]:
        if not filters:
            return list(self._blocks.keys())
        return [
            block_id
            for block_id, meta in self._metadata.items()
            if all(str(meta.get(key)) == str(value) for key, value in filters.items())
        ]
