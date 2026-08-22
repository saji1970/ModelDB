"""Storage savings estimation (CLAUDE-STORAGE.md section 45: distinguish
theoretical capability from a measured result).

`estimate_storage_savings` is explicitly a *theoretical* estimate
derived from `DataProfile.compressibility_estimate` (itself computed
from real Shannon entropy in Phase A) - never a claim about what a
real codec will actually achieve. Phase F's real compressors produce
measured numbers to compare this against; conflating the two here
would be exactly the kind of unverified claim section 45 forbids.
"""

from __future__ import annotations

from mdc.classification.metadata import DataProfile
from mdc.index.object_index import IndexEntry
from mdc.storage_intelligence.strategy import StorageStrategy
from mdc.storage_intelligence.strategy_types import CompressionAlgorithm


def estimate_storage_savings(profile: DataProfile, strategy: StorageStrategy) -> float:
    """Estimated fraction of bytes saved (0.0-1.0) if `strategy.compression`
    is applied - a theoretical estimate, not a measured compression ratio."""
    if strategy.compression is CompressionAlgorithm.NONE:
        return 0.0
    return max(0.0, min(1.0, profile.compressibility_estimate or 0.0))


def measured_compression_ratio(entry: IndexEntry) -> float | None:
    """The *actual* fraction of bytes saved (0.0-1.0), from a real
    compress() call recorded at write time (Phase F) - `None` when the
    object wasn't compressed. This is the number `estimate_storage_savings`
    should be checked against, never assumed to already agree with
    (section 45)."""
    if entry.compressed_size is None or entry.size == 0:
        return None
    return max(0.0, 1.0 - (entry.compressed_size / entry.size))
