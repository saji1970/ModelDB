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
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timezone
from typing import Any

from mdc.dna.corruption import CorruptionRates, corrupt_sequence
from mdc.dna.crypto import DecryptionError, decrypt, encrypt
from mdc.dna.ecc import ECCDecodeResult, RepetitionECC
from mdc.dna.encoder import DNADecodeError, decode, encode
from mdc.security.keys import load_or_create_dna_key
from mdc.storage.interface import StorageBackend


class DNAStorageBackend(StorageBackend):
    def __init__(self, ecc_copies: int = 1, encryption_key: bytes | None = None):
        if ecc_copies < 1:
            raise ValueError("ecc_copies must be >= 1")
        self.ecc_copies = ecc_copies
        self._ecc = RepetitionECC(copies=ecc_copies) if ecc_copies > 1 else None
        self._key = encryption_key if encryption_key is not None else load_or_create_dna_key()
        self._sequences: dict[str, list[str]] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._checksums: dict[str, str] = {}
        self._created_at: dict[str, datetime] = {}

    def put(self, block_id: str, payload: bytes, metadata: dict[str, Any] | None = None) -> str:
        checksum = hashlib.sha256(payload).hexdigest()
        ciphertext = encrypt(self._key, payload)
        copies = self._ecc.encode(ciphertext) if self._ecc else [ciphertext]
        self._sequences[block_id] = [encode(copy) for copy in copies]
        self._metadata[block_id] = dict(metadata) if metadata else {}
        self._checksums[block_id] = checksum
        self._created_at.setdefault(block_id, datetime.now(timezone.utc))
        return checksum

    def get(self, block_id: str) -> bytes:
        sequences = self._require(block_id)
        if self._ecc is None:
            return decrypt(self._key, decode(sequences[0]))
        result = self._ecc.decode([decode(seq) for seq in sequences])
        if result.data is None:
            raise KeyError(f"block {block_id!r} could not be recovered")
        return decrypt(self._key, result.data)

    def exists(self, block_id: str) -> bool:
        return block_id in self._sequences

    def delete(self, block_id: str) -> None:
        self._sequences.pop(block_id, None)
        self._metadata.pop(block_id, None)
        self._checksums.pop(block_id, None)
        self._created_at.pop(block_id, None)

    def metadata(self, block_id: str) -> dict[str, Any]:
        self._require(block_id)
        return {
            "metadata": self._metadata[block_id],
            "checksum": self._checksums[block_id],
            "created_at": self._created_at[block_id],
        }

    def search(self, **filters: Any) -> list[str]:
        if not filters:
            return list(self._sequences.keys())
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
        if block_id not in self._sequences:
            raise KeyError(f"No block found for block_id={block_id!r}")
        return self._sequences[block_id]
