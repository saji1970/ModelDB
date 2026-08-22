"""StorageRouter (CLAUDE-STORAGE.md sections 15, 44).

Takes a `StorageStrategy` (Phase B's decision, already made) and turns
it into an actual write to the tier-appropriate backend, applying the
strategy's chosen compression for real (Phase F) and recording the
result in the `ObjectIndex` (section 32) so a later `retrieve`/`delete`
doesn't need to know which backend an object landed on - or that it
was ever compressed. `checksum` always tracks the *logical*
(uncompressed) content, never the physical bytes actually written, so
integrity checking on retrieve is independent of whatever compression
was applied.

Only two tiers currently map to a genuinely distinct physical backend
(HOT -> in-memory, everything else -> the durable DuckDB-backed store)
- WARM/COLD/ARCHIVE are recorded as *decisions* today, not yet
  *enforced* as different physical backends, since NVMe/filesystem/DNA
  backends don't exist until later phases (sections 10, 33-38). Giving
  three tiers three real destinations without those backends existing
  would be exactly the unverified claim section 45 forbids.
"""

from __future__ import annotations

import hashlib

from mdc.compression.compressor import CompressionError, compress, decompress
from mdc.dna.storage import DNAStorageBackend
from mdc.index.object_index import IndexEntry, ObjectIndex, utcnow
from mdc.model.object import MDCObject
from mdc.storage.interface import StorageBackend
from mdc.storage.memory_store import MemoryStorageBackend
from mdc.storage_intelligence.strategy import StorageStrategy, StorageTier
from mdc.storage_intelligence.strategy_types import CompressionAlgorithm


class ObjectNotFoundError(Exception):
    def __init__(self, object_id: str):
        super().__init__(f"No object found with object_id={object_id!r}")
        self.object_id = object_id


class DataIntegrityError(Exception):
    """Raised when a retrieved object's checksum doesn't match what was
    recorded at write time (CLAUDE-STORAGE.md section 39)."""

    def __init__(self, object_id: str, expected: str, actual: str):
        super().__init__(f"Checksum mismatch for {object_id!r}: expected {expected}, got {actual}")
        self.object_id = object_id
        self.expected = expected
        self.actual = actual


class StorageRouter:
    def __init__(self, backends: dict[StorageTier, StorageBackend], index: ObjectIndex):
        missing = set(StorageTier) - set(backends)
        if missing:
            raise ValueError(f"StorageRouter needs a backend for every tier, missing: {sorted(t.value for t in missing)}")
        self.backends = backends
        self.index = index

    def store(
        self,
        obj: MDCObject,
        content: bytes,
        strategy: StorageStrategy,
        *,
        tensor_id: str | None = None,
        tensor_name: str | None = None,
        block_id: str | None = None,
    ) -> IndexEntry:
        backend = self.backends[strategy.storage_tier]
        location = f"{obj.object_type.value}:{obj.object_id}"
        physical_bytes = compress(content, strategy.compression)
        backend.put(location, physical_bytes, metadata={"object_id": obj.object_id, "object_type": obj.object_type.value})

        entry = IndexEntry(
            object_id=obj.object_id,
            object_type=obj.object_type,
            storage_backend=type(backend).__name__,
            storage_tier=strategy.storage_tier,
            location=location,
            size=len(content),
            compressed_size=len(physical_bytes) if strategy.compression is not CompressionAlgorithm.NONE else None,
            checksum=hashlib.sha256(content).hexdigest(),
            compression=strategy.compression,
            representation=strategy.representation,
            tensor_id=tensor_id,
            tensor_name=tensor_name,
            block_id=block_id,
            indexed_at=utcnow(),
        )
        self.index.register(entry)
        return entry

    def retrieve(self, object_id: str, *, verify_integrity: bool = True) -> bytes:
        entry = self._require_entry(object_id)
        backend = self.backends[entry.storage_tier]
        physical_bytes = backend.get(entry.location)
        try:
            content = decompress(physical_bytes, entry.compression)
        except CompressionError as exc:
            raise DataIntegrityError(object_id, entry.checksum, f"<decompression failed: {exc}>") from exc
        if verify_integrity:
            actual = hashlib.sha256(content).hexdigest()
            if actual != entry.checksum:
                raise DataIntegrityError(object_id, entry.checksum, actual)
        return content

    def delete(self, object_id: str) -> None:
        entry = self._require_entry(object_id)
        self.backends[entry.storage_tier].delete(entry.location)
        self.index.delete(object_id)

    def move(self, object_id: str, new_tier: StorageTier) -> IndexEntry:
        """POST /objects/{id}/move (section 34)."""
        entry = self._require_entry(object_id)
        if entry.storage_tier is new_tier:
            return entry

        old_backend = self.backends[entry.storage_tier]
        new_backend = self.backends[new_tier]
        # Relocates the physical (already-compressed) bytes as-is - no
        # decompress/recompress round trip, since the compression
        # algorithm doesn't change on a tier move. `entry.checksum`
        # (of the logical content) is therefore untouched too. Several
        # tiers can share one physical backend (build_default_router:
        # WARM/COLD/ARCHIVE all point at the same durable store) - in
        # that case the bytes are already in the right place, so
        # put-then-delete would just write and immediately erase them.
        if old_backend is not new_backend:
            physical_bytes = old_backend.get(entry.location)
            new_backend.put(entry.location, physical_bytes, metadata={"object_id": object_id, "object_type": entry.object_type.value})
            old_backend.delete(entry.location)

        moved = entry.model_copy(update={
            "storage_tier": new_tier,
            "storage_backend": type(new_backend).__name__,
            "indexed_at": utcnow(),
        })
        self.index.register(moved)
        return moved

    def _require_entry(self, object_id: str) -> IndexEntry:
        entry = self.index.get(object_id)
        if entry is None:
            raise ObjectNotFoundError(object_id)
        return entry


def build_default_router(persistent_backend: StorageBackend, *, dna_backend: StorageBackend | None = None) -> StorageRouter:
    """HOT -> a fresh in-memory backend; WARM/COLD -> the given durable
    backend (typically a `DuckDBStore`); ARCHIVE -> a real
    `DNAStorageBackend` (Phase H) - the honest gap flagged since Phase C
    (every tier sharing one physical backend) is now closed for the one
    tier that's only ever reached by explicit archival intent (section 25).
    The index always rides on `persistent_backend` so it survives
    independently of any tier's data."""
    backends: dict[StorageTier, StorageBackend] = {
        StorageTier.HOT: MemoryStorageBackend(),
        StorageTier.WARM: persistent_backend,
        StorageTier.COLD: persistent_backend,
        StorageTier.ARCHIVE: dna_backend or DNAStorageBackend(),
    }
    return StorageRouter(backends, ObjectIndex(persistent_backend))
