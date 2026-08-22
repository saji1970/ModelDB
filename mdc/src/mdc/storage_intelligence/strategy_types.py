"""The vocabulary `StorageStrategy` is built from (CLAUDE-STORAGE.md
section 9). Split out from `strategy.py` so `policy.py` and
`analyzer.py` can depend on the enums without a circular import back
through `strategy.py` (which depends on both of them).
"""

from __future__ import annotations

from enum import Enum


class Representation(str, Enum):
    """A structural shape, not a physical backend. DNA_ENCODED is never
    chosen automatically by the Phase B engine - it's reachable only
    once an explicit archival flow (Phase H) exists to back it."""

    RAW = "RAW"
    COMPRESSED = "COMPRESSED"
    MATRIX = "MATRIX"
    TENSOR = "TENSOR"
    BLOCK = "BLOCK"
    DNA_ENCODED = "DNA_ENCODED"


class CompressionAlgorithm(str, Enum):
    NONE = "NONE"
    ZLIB = "ZLIB"
    GZIP = "GZIP"
    LZ4 = "LZ4"
    ZSTD = "ZSTD"


class StorageTier(str, Enum):
    HOT = "HOT"
    WARM = "WARM"
    COLD = "COLD"
    ARCHIVE = "ARCHIVE"


class CachePolicy(str, Enum):
    NONE = "NONE"
    MEMORY = "MEMORY"
