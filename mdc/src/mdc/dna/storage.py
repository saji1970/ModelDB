"""DNAStorageBackend (CLAUDE-STORAGE.md sections 10, 26, 35).

Implements the same `StorageBackend` interface as every other backend
(`DuckDBStore`, `MemoryStorageBackend`) so it can be routed to like any
other tier. `put()`/`get()` round-trip losslessly by default - DNA
encode/decode is an exact bijection, so a normal read never
corrupts anything. Corruption is a separate, explicit research tool
(`corrupt_and_recover`) that studies reliability *without* mutating
the actually-stored block - a real backend would be useless if every
read silently lost data.

Every payload is AES-256-GCM encrypted (`dna/crypto.py`) *before* it's
DNA-encoded - the stored sequence is ciphertext-as-ACGT, not
plaintext-as-ACGT, so it's unreadable without the key this backend
holds (see `security/keys.py`), not just unreadable without knowing
the public 2-bit-per-base mapping.

Persistence: constructed with `persist=<a durable StorageBackend>` (the
router passes the database's own DuckDB store), every block is also written
there - under the id `dna:<block_id>`, with the caller's metadata and the
plaintext checksum alongside - and read back from there, so archived objects
survive a restart and are removed with the database. The stored form is the
2-bit-per-base sequence packed four bases to the byte, which is the encrypted
bytes themselves: writing the same thing as the letters A/C/G/T takes four
times the space and adds nothing, so the letters are produced on demand
(`sequences_for`) and by the research tooling, not stored. Without `persist` the backend is a pure in-memory simulation (the
research tooling and unit tests use it that way). Only ciphertext is ever
stored; reading needs the same key, and `rotate_key` re-keys what is stored.
"""

from __future__ import annotations

import hashlib
import json
import random
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from mdc.dna.corruption import CorruptionRates, corrupt_sequence
from mdc.dna.crypto import DecryptionError, decrypt, encrypt
from mdc.dna.ecc import ECCDecodeResult, RepetitionECC
from mdc.dna.encoder import DNADecodeError, decode, encode
from mdc.security.keys import load_or_create_dna_key
from mdc.storage.interface import StorageBackend


_PERSIST_PREFIX = "dna:"
_PACK_MAGIC = b"MDCD1"  # header of the packed form: magic, copies (1 byte), copy length (4 bytes)


def _pack(copies: list[bytes]) -> bytes:
    return _PACK_MAGIC + struct.pack(">BI", len(copies), len(copies[0])) + b"".join(copies)


def _unpack(blob: bytes) -> list[bytes]:
    """The copies in a stored block. Also reads the form the first persistent
    archive wrote (a JSON list of A/C/G/T strings), so nothing stored earlier is lost."""
    if blob.startswith(_PACK_MAGIC):
        head = len(_PACK_MAGIC) + 5
        count, length = struct.unpack(">BI", blob[len(_PACK_MAGIC) : head])
        body = blob[head:]
        if count < 1 or len(body) != count * length:
            raise DecryptionError("stored archive block is damaged (its length does not match its header)")
        return [body[i * length : (i + 1) * length] for i in range(count)]
    return [decode(seq) for seq in json.loads(blob)["sequences"]]

_PERSIST_MARK = {"dna_archive": "1"}


@dataclass(frozen=True)
class ArchiveRotationReport:
    blocks: int  # blocks re-encrypted under the new key
    already_current: int  # blocks already under the new key (a rerun after an interruption)


class DNAStorageBackend(StorageBackend):
    def __init__(self, ecc_copies: int = 1, encryption_key: bytes | None = None, persist: StorageBackend | None = None):
        if ecc_copies < 1:
            raise ValueError("ecc_copies must be >= 1")
        self.ecc_copies = ecc_copies
        self._ecc = RepetitionECC(copies=ecc_copies) if ecc_copies > 1 else None
        self._explicit_key = encryption_key
        self._persist = persist
        # In-memory mode keeps the sequences themselves in `_sequences`; with
        # `persist` they live in the durable store and only this small index
        # (metadata, checksum, time) is held here.
        self._sequences: dict[str, list[str]] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._checksums: dict[str, str] = {}
        self._created_at: dict[str, datetime] = {}
        if persist is not None:
            self._load_index()

    @property
    def _key(self) -> bytes:
        # Resolved on first use, so merely opening a database never creates a
        # key file; one is made when something is first sealed.
        if self._explicit_key is None:
            self._explicit_key = load_or_create_dna_key()
        return self._explicit_key

    @_key.setter
    def _key(self, value: bytes) -> None:
        self._explicit_key = value

    def put(self, block_id: str, payload: bytes, metadata: dict[str, Any] | None = None) -> str:
        return self._put(block_id, payload, metadata, self._key)

    def _put(self, block_id: str, payload: bytes, metadata: dict[str, Any] | None, key: bytes) -> str:
        checksum = hashlib.sha256(payload).hexdigest()
        ciphertext = encrypt(key, payload)
        copies = self._ecc.encode(ciphertext) if self._ecc else [ciphertext]
        meta = dict(metadata) if metadata else {}
        if self._persist is not None:
            self._persist.put(
                _PERSIST_PREFIX + block_id,
                _pack(copies),
                metadata={**_PERSIST_MARK, "block": block_id, "checksum": checksum, "meta": meta},
            )
            self._metadata[block_id], self._checksums[block_id] = meta, checksum
            self._created_at[block_id] = self._persist.metadata(_PERSIST_PREFIX + block_id)["created_at"]
            return checksum
        self._sequences[block_id] = [encode(copy) for copy in copies]
        self._metadata[block_id] = meta
        self._checksums[block_id] = checksum
        self._created_at.setdefault(block_id, datetime.now(timezone.utc))
        return checksum

    def get(self, block_id: str) -> bytes:
        return self._read(block_id, self._key)

    def _read(self, block_id: str, key: bytes) -> bytes:
        copies = self._copies(block_id)
        if len(copies) == 1:
            return decrypt(key, copies[0])
        # Decode with the number of copies the block was written with, not the
        # current setting: a block stays readable if ecc_copies is changed.
        ecc = self._ecc if self._ecc is not None and self._ecc.copies == len(copies) else RepetitionECC(copies=len(copies))
        result = ecc.decode(copies)
        if result.data is None:
            raise KeyError(f"block {block_id!r} could not be recovered")
        return decrypt(key, result.data)

    def _copies(self, block_id: str) -> list[bytes]:
        """The stored copies of a block, as bytes (the packed bases)."""
        if self._persist is not None:
            if block_id not in self._metadata:
                raise KeyError(f"No block found for block_id={block_id!r}")
            try:
                return _unpack(self._persist.get(_PERSIST_PREFIX + block_id))
            except KeyError:
                raise KeyError(f"No block found for block_id={block_id!r}") from None
        return [decode(seq) for seq in self._require(block_id)]

    def exists(self, block_id: str) -> bool:
        return block_id in self._metadata if self._persist is not None else block_id in self._sequences

    def delete(self, block_id: str) -> None:
        if self._persist is not None and block_id in self._metadata:
            self._persist.delete(_PERSIST_PREFIX + block_id)
        self._sequences.pop(block_id, None)
        self._metadata.pop(block_id, None)
        self._checksums.pop(block_id, None)
        self._created_at.pop(block_id, None)

    def rotate_key(self, new_key: bytes) -> ArchiveRotationReport:
        """Re-encrypt every stored block under `new_key` and adopt it.

        Each block is decrypted and re-encrypted in memory first, so one that
        authenticates under neither key (damaged, or the wrong old key) stops
        the rotation before anything is written. The writes that follow are
        one block at a time; if the process dies part-way, run it again -
        blocks already under the new key are recognized and skipped. Stop the
        server first, as for stream rotation."""
        if len(new_key) != 32:
            raise ValueError("the new key must be 32 bytes")
        old_key = self._key
        if new_key == old_key:
            raise ValueError("The new key is the same as the current one.")
        plan: list[tuple[str, bytes]] = []
        already = 0
        for block_id in self.search():
            try:
                plan.append((block_id, self._read(block_id, old_key)))
            except DecryptionError:
                self._read(block_id, new_key)  # raises DecryptionError if it is under neither key
                already += 1
        for block_id, plaintext in plan:
            self._put(block_id, plaintext, self._metadata[block_id], new_key)
        self._explicit_key = new_key
        return ArchiveRotationReport(blocks=len(plan), already_current=already)

    def _load_index(self) -> None:
        for row_id in self._persist.search(**_PERSIST_MARK):
            info = self._persist.metadata(row_id)
            stored = info["metadata"] or {}
            block_id = stored.get("block") or row_id[len(_PERSIST_PREFIX):]
            self._metadata[block_id] = stored.get("meta") or {}
            self._checksums[block_id] = stored.get("checksum", "")
            self._created_at[block_id] = info["created_at"]

    def metadata(self, block_id: str) -> dict[str, Any]:
        if not self.exists(block_id):
            raise KeyError(f"No block found for block_id={block_id!r}")
        return {
            "metadata": self._metadata[block_id],
            "checksum": self._checksums[block_id],
            "created_at": self._created_at[block_id],
        }

    def search(self, **filters: Any) -> list[str]:
        if not filters:
            return list(self._metadata.keys())
        return [
            block_id
            for block_id, meta in self._metadata.items()
            if all(str(meta.get(key)) == str(value) for key, value in filters.items())
        ]

    def sequences_for(self, block_id: str) -> list[str]:
        """The raw DNA sequence(s) backing a block - for research tooling."""
        return list(self._require(block_id))

    def corrupt_and_recover(self, block_id: str, rates: CorruptionRates, seed: int) -> ECCDecodeResult:
        """Simulate corruption on this block's stored sequences and attempt
        recovery, WITHOUT touching the actually-stored data (section 37:
        research into reliability, not a destructive operation).

        ECC recovery operates on ciphertext (that's what's actually
        DNA-encoded); this decrypts the recovered bytes before
        returning so the result still reflects the original payload.
        A majority vote that "recovers" the wrong bytes fails AES-GCM's
        authentication tag rather than silently returning garbage - that
        counts as recovery failure here, same as a dropped sequence.
        """
        sequences = self._require(block_id)
        rng = random.Random(seed)
        decoded_copies: list[bytes | None] = []
        for sequence in sequences:
            corrupted = corrupt_sequence(sequence, rates, rng)
            if corrupted is None:
                decoded_copies.append(None)
                continue
            try:
                decoded_copies.append(decode(corrupted))
            except DNADecodeError:
                decoded_copies.append(None)

        if self._ecc is not None:
            result = self._ecc.decode(decoded_copies)
        else:
            single = decoded_copies[0] if decoded_copies else None
            result = ECCDecodeResult(data=single, recovered=False, corrected_byte_count=0, usable_copies=1 if single is not None else 0)

        if result.data is None:
            return result
        try:
            plaintext = decrypt(self._key, result.data)
        except DecryptionError:
            return ECCDecodeResult(data=None, recovered=False, corrected_byte_count=result.corrected_byte_count, usable_copies=result.usable_copies)
        return ECCDecodeResult(data=plaintext, recovered=result.recovered, corrected_byte_count=result.corrected_byte_count, usable_copies=result.usable_copies)

    def _require(self, block_id: str) -> list[str]:
        """The block's sequences as A/C/G/T text (for research tooling)."""
        if self._persist is not None:
            return [encode(copy) for copy in self._copies(block_id)]
        if block_id not in self._sequences:
            raise KeyError(f"No block found for block_id={block_id!r}")
        return self._sequences[block_id]
