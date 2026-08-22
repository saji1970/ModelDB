"""StorageStrategy (CLAUDE-STORAGE.md sections 8-9, 11).

The *output* of the Storage Intelligence Layer - what to do with an
object - kept separate from how that decision gets made
(`policy.py`/`analyzer.py`) and from what it's estimated to save
(`optimizer.py`). Nothing here talks to a storage backend; `router.py`
(Phase C) is what turns a strategy into an actual write.
"""

from __future__ import annotations

from pydantic import BaseModel

from mdc.classification.metadata import DataProfile
from mdc.storage_intelligence.analyzer import (
    AccessProfile,
    select_cache_policy,
    select_chunking,
    select_compression,
    select_indexing,
    select_representation,
    select_tier,
)
from mdc.storage_intelligence.policy import DEFAULT_POLICY, StoragePolicy
from mdc.storage_intelligence.strategy_types import CachePolicy, CompressionAlgorithm, Representation, StorageTier

__all__ = [
    "AccessProfile", "CachePolicy", "CompressionAlgorithm", "Representation", "StorageTier",
    "StorageStrategy", "StorageStrategyEngine",
]


class StorageStrategy(BaseModel):
    representation: Representation
    compression: CompressionAlgorithm
    chunking: bool
    chunk_size: int | None = None
    indexing: bool
    encryption: bool = False
    error_correction: bool = False
    storage_tier: StorageTier
    cache_policy: CachePolicy


class StorageStrategyEngine:
    """`strategy = engine.select(profile)` (section 11). Every individual
    decision is a pure function of `(profile, access, policy)` in
    `analyzer.py` - this class only assembles their outputs, so each
    decision can be tested and reasoned about independently."""

    def __init__(self, policy: StoragePolicy | None = None):
        self.policy = policy or DEFAULT_POLICY

    def select(self, profile: DataProfile, access: AccessProfile | None = None) -> StorageStrategy:
        access = access or AccessProfile()
        chunking, chunk_size = select_chunking(profile, self.policy)
        tier = select_tier(profile, access, self.policy)
        return StorageStrategy(
            representation=select_representation(profile),
            compression=select_compression(profile, self.policy),
            chunking=chunking,
            chunk_size=chunk_size,
            indexing=select_indexing(profile),
            encryption=False,
            error_correction=False,
            storage_tier=tier,
            cache_policy=select_cache_policy(tier, self.policy),
        )
