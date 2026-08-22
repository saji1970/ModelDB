"""Individual storage decisions (CLAUDE-STORAGE.md sections 11, 27-29).

Each `select_*` function is a pure function of `(profile, access,
policy)` - not one big if/elif ladder branching on `data_type` alone.
Representation is decided from the classifier's *structural* signals
(`tensor_candidate`/`matrix_candidate`), chunking from measured size,
compression from measured entropy (`compressibility_estimate`, a real
number from Phase A - not a guess), and tier from access history. Two
objects of the same `DataType` can and do get different strategies
once their profiles or access patterns differ - see
`test_storage_intelligence.py`'s "two models, two strategies" case.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from mdc.classification.data_type import DataType
from mdc.classification.metadata import DataProfile
from mdc.storage_intelligence.policy import StoragePolicy
from mdc.storage_intelligence.strategy_types import CachePolicy, CompressionAlgorithm, Representation, StorageTier

# Types where content-level indexing is meaningful (full-text/field
# search). An opaque IMAGE/VIDEO/AUDIO blob isn't indexed here - only
# its metadata would be, which is a cataloging concern, not this
# object's own representation.
_INDEXABLE_TYPES = {
    DataType.DATABASE_RECORD, DataType.TABULAR, DataType.TIME_SERIES,
    DataType.DOCUMENT, DataType.LOG, DataType.TEXT,
}


@dataclass(frozen=True)
class AccessProfile:
    """CLAUDE-STORAGE.md section 28. `access_frequency`/`mutation_frequency`
    are reads-per-day / writes-per-day - the caller's responsibility to
    derive from `read_count`/`write_count`/`last_access` history; this
    phase doesn't yet *collect* that history (no router/backend observes
    real requests yet - Phase C+), so a freshly-created object legitimately
    has an all-zero, "no history yet" `AccessProfile()`."""

    read_count: int = 0
    write_count: int = 0
    last_access: datetime | None = None
    access_frequency: float = 0.0
    mutation_frequency: float = 0.0
    # Explicit archival intent (section 25: "archival = true"). ARCHIVE is
    # never inferred from low access alone - low-access-but-never-asked-
    # to-be-archived objects land in COLD, one step short, so a caller
    # always has to say they mean it.
    archive_requested: bool = False


def select_representation(profile: DataProfile) -> Representation:
    if profile.tensor_candidate:
        return Representation.TENSOR
    if profile.matrix_candidate:
        return Representation.MATRIX
    return Representation.RAW


def select_compression(profile: DataProfile, policy: StoragePolicy) -> CompressionAlgorithm:
    if not profile.compression_candidate:
        return CompressionAlgorithm.NONE
    return policy.default_compression_algorithm


def select_chunking(profile: DataProfile, policy: StoragePolicy) -> tuple[bool, int | None]:
    if profile.size_bytes > policy.chunk_threshold_bytes:
        return True, policy.chunk_size_bytes
    return False, None


def select_indexing(profile: DataProfile) -> bool:
    return profile.data_type in _INDEXABLE_TYPES


def select_tier(profile: DataProfile, access: AccessProfile, policy: StoragePolicy) -> StorageTier:
    if access.archive_requested and not profile.mutable and access.mutation_frequency == 0.0:
        return StorageTier.ARCHIVE
    if access.access_frequency >= policy.hot_access_frequency:
        return StorageTier.HOT
    if access.access_frequency >= policy.warm_access_frequency:
        return StorageTier.WARM
    if not profile.mutable and access.mutation_frequency == 0.0 and access.access_frequency == 0.0:
        # Immutable and never yet accessed or modified - a candidate for
        # COLD, not ARCHIVE: ARCHIVE additionally requires the explicit
        # archival intent section 25 describes, which this phase doesn't
        # collect yet (no `archive=true` request path exists before
        # Phase H's DNA backend does).
        return StorageTier.COLD
    return StorageTier.WARM


def select_cache_policy(tier: StorageTier, policy: StoragePolicy) -> CachePolicy:
    return CachePolicy.MEMORY if tier is StorageTier.HOT else CachePolicy.NONE
