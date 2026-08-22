"""Pluggable ECCProvider (CLAUDE-STORAGE.md section 25/38: "the storage
engine must not depend on a particular ECC algorithm").

Only `RepetitionECC` (N-way byte-level majority vote across
independently DNA-encoded, independently corrupted copies) is
implemented. Reed-Solomon/fountain codes/LDPC (named in the spec as
future options) aren't built - documenting one real, working, but
limited scheme, not a stand-in for all of them.

Operates on N *separate* redundant copies (`encode` returns
`list[bytes]`), not one byte blob with embedded redundancy: each copy
becomes its own independent DNA sequence and is corrupted
independently (`dna/corruption.py`). This sidesteps the alignment
problem a single concatenated blob would have once an
insertion/deletion shifts one copy's length but not another's - and
it's how physical DNA storage redundancy actually works (separate
strands/reads, not one giant strand with inline redundancy).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class ECCDecodeResult:
    data: bytes | None
    recovered: bool  # True if majority voting corrected a disagreement, or a copy was lost
    corrected_byte_count: int
    usable_copies: int


class ECCProvider(ABC):
    @abstractmethod
    def encode(self, payload: bytes) -> list[bytes]:
        """Return N redundant copies of `payload`."""

    @abstractmethod
    def decode(self, copies: list[bytes | None]) -> ECCDecodeResult:
        """Recover the original payload from N possibly-corrupted or
        missing copies (`None` = that copy was entirely lost)."""


class RepetitionECC(ECCProvider):
    def __init__(self, copies: int = 3):
        if copies < 1 or copies % 2 == 0:
            raise ValueError("copies must be odd and >= 1 for majority voting to be unambiguous")
        self.copies = copies

    def encode(self, payload: bytes) -> list[bytes]:
        return [payload for _ in range(self.copies)]

    def decode(self, copies: list[bytes | None]) -> ECCDecodeResult:
        usable = [c for c in copies if c is not None]
        if not usable:
            return ECCDecodeResult(data=None, recovered=False, corrected_byte_count=0, usable_copies=0)

        by_length: dict[int, list[bytes]] = {}
        for copy in usable:
            by_length.setdefault(len(copy), []).append(copy)
        _, majority_group = max(by_length.items(), key=lambda item: len(item[1]))

        if len(majority_group) == 1:
            # Only one copy survived at a consistent length - return it,
            # but honestly flagged as unverified, not "recovered."
            return ECCDecodeResult(data=majority_group[0], recovered=False, corrected_byte_count=0, usable_copies=len(usable))

        length = len(majority_group[0])
        recovered = bytearray(length)
        corrected = 0
        for i in range(length):
            votes = Counter(copy[i] for copy in majority_group)
            value, count = votes.most_common(1)[0]
            recovered[i] = value
            if count < len(majority_group):
                corrected += 1
        return ECCDecodeResult(
            data=bytes(recovered),
            recovered=corrected > 0 or len(usable) < len(copies),
            corrected_byte_count=corrected,
            usable_copies=len(usable),
        )
