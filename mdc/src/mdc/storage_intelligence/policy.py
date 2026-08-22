"""Storage policy thresholds (CLAUDE-STORAGE.md section 29: "these rules
must be configurable").

Every threshold `analyzer.py` uses to turn a profile into a strategy
lives here, in one place, as data - not scattered as magic numbers
through the decision functions. Swap `DEFAULT_POLICY` for a custom
`StoragePolicy` to change behavior without touching decision logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from mdc.storage_intelligence.strategy_types import CompressionAlgorithm

# 4 MiB - the chunk size CLAUDE-STORAGE.md section 40's example plan uses.
_FOUR_MIB = 4 * 1024 * 1024


@dataclass(frozen=True)
class StoragePolicy:
    chunk_threshold_bytes: int = _FOUR_MIB
    chunk_size_bytes: int = _FOUR_MIB
    default_compression_algorithm: CompressionAlgorithm = CompressionAlgorithm.ZLIB
    # Reads/day at or above this -> HOT.
    hot_access_frequency: float = 10.0
    # Reads/day at or above this (but below hot) -> WARM.
    warm_access_frequency: float = 0.1


DEFAULT_POLICY = StoragePolicy()
