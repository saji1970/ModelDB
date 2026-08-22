"""Phase B: StorageStrategy + StorageStrategyEngine (CLAUDE-STORAGE.md
sections 8-9, 11, 27-29, required tests in section 48).
"""

import json
import struct

from mdc.classification.classifier import classify_and_profile
from mdc.classification.data_type import DataType
from mdc.classification.metadata import build_profile
from mdc.storage_intelligence.analyzer import AccessProfile
from mdc.storage_intelligence.optimizer import estimate_storage_savings
from mdc.storage_intelligence.policy import DEFAULT_POLICY, StoragePolicy
from mdc.storage_intelligence.strategy import (
    CachePolicy,
    CompressionAlgorithm,
    Representation,
    StorageStrategyEngine,
    StorageTier,
)

engine = StorageStrategyEngine()


def _safetensors_bytes() -> bytes:
    header = {"weight": {"dtype": "F32", "shape": [2, 2], "data_offsets": [0, 16]}}
    header_bytes = json.dumps(header).encode("utf-8")
    return struct.pack("<Q", len(header_bytes)) + header_bytes + b"\x00" * 16


# -- section 48 required strategy tests ------------------------------------------

def test_model_strategy():
    profile = classify_and_profile(_safetensors_bytes(), "model.safetensors")
    strategy = engine.select(profile)
    assert strategy.representation is Representation.TENSOR


def test_image_strategy():
    # High-entropy payload (bytes 0-255 repeated), like a real compressed
    # photo - should not be flagged as a further compression candidate.
    jpeg = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 8
    profile = classify_and_profile(jpeg, "photo.jpg")
    strategy = engine.select(profile)
    assert strategy.representation is Representation.RAW
    assert strategy.compression is CompressionAlgorithm.NONE
    assert strategy.indexing is False


def test_document_strategy():
    pdf = b"%PDF-1.7\n" + b"\x00" * 64
    profile = classify_and_profile(pdf, "report.pdf")
    strategy = engine.select(profile)
    assert strategy.indexing is True


def test_database_strategy():
    record = json.dumps({"merchant_id": "M1", "name": "ABC Store"}).encode("utf-8")
    profile = classify_and_profile(record, "record.json")
    strategy = engine.select(profile)
    assert strategy.representation is Representation.RAW
    assert strategy.indexing is True


# -- tiering (section 27-29) -------------------------------------------------------

def test_hot_tiering_for_frequently_accessed_object():
    profile = build_profile(DataType.AI_MODEL, b"\x00" * 100)
    strategy = engine.select(profile, AccessProfile(access_frequency=50.0))
    assert strategy.storage_tier is StorageTier.HOT
    assert strategy.cache_policy is CachePolicy.MEMORY


def test_warm_tiering_for_moderately_accessed_object():
    profile = build_profile(DataType.AI_MODEL, b"\x00" * 100)
    strategy = engine.select(profile, AccessProfile(access_frequency=1.0))
    assert strategy.storage_tier is StorageTier.WARM
    assert strategy.cache_policy is CachePolicy.NONE


def test_cold_tiering_for_immutable_never_accessed_object():
    profile = build_profile(DataType.IMAGE, b"\x00" * 100)  # IMAGE defaults to immutable
    strategy = engine.select(profile, AccessProfile())
    assert strategy.storage_tier is StorageTier.COLD


def test_archive_tiering_requires_explicit_request():
    profile = build_profile(DataType.AI_MODEL, b"\x00" * 100)
    not_archived = engine.select(profile, AccessProfile())
    assert not_archived.storage_tier is StorageTier.COLD  # low access alone is not enough

    archived = engine.select(profile, AccessProfile(archive_requested=True))
    assert archived.storage_tier is StorageTier.ARCHIVE


def test_mutable_database_record_never_defaults_to_cold_or_archive():
    profile = build_profile(DataType.DATABASE_RECORD, b'{"a": 1}')
    strategy = engine.select(profile, AccessProfile())
    assert strategy.storage_tier is StorageTier.WARM


# -- two objects of the same type, different strategies (the point of the layer) --

def test_two_models_get_different_strategies_from_different_profiles_or_access():
    hot_small_model = build_profile(DataType.AI_MODEL, bytes(range(256)) * 4)  # small, high-entropy
    cold_large_model = build_profile(DataType.AI_MODEL, b"\x00" * (5 * 1024 * 1024))  # large, compressible

    hot_strategy = engine.select(hot_small_model, AccessProfile(access_frequency=100.0))
    cold_strategy = engine.select(cold_large_model, AccessProfile())

    assert hot_strategy.storage_tier is StorageTier.HOT
    assert cold_strategy.storage_tier is StorageTier.COLD
    assert hot_strategy.chunking is False
    assert cold_strategy.chunking is True
    assert cold_strategy.chunk_size == DEFAULT_POLICY.chunk_size_bytes


# -- DNA_ENCODED is never auto-selected in this phase ------------------------------

def test_dna_encoded_representation_is_never_automatically_selected():
    for data_type in DataType:
        profile = build_profile(data_type, b"\x00" * 100)
        strategy = engine.select(profile, AccessProfile(archive_requested=True))
        assert strategy.representation is not Representation.DNA_ENCODED


# -- chunking threshold ------------------------------------------------------------

def test_large_object_is_chunked_small_object_is_not():
    small = build_profile(DataType.VIDEO, b"\x00" * 1024)
    large = build_profile(DataType.VIDEO, b"\x00" * (5 * 1024 * 1024))

    assert engine.select(small).chunking is False
    large_strategy = engine.select(large)
    assert large_strategy.chunking is True
    assert large_strategy.chunk_size == 4 * 1024 * 1024


# -- configurable policy (section 29) ----------------------------------------------

def test_policy_thresholds_are_configurable():
    lenient_policy = StoragePolicy(hot_access_frequency=1000.0)
    lenient_engine = StorageStrategyEngine(policy=lenient_policy)
    profile = build_profile(DataType.AI_MODEL, b"\x00" * 100)

    default_strategy = engine.select(profile, AccessProfile(access_frequency=50.0))
    lenient_strategy = lenient_engine.select(profile, AccessProfile(access_frequency=50.0))

    assert default_strategy.storage_tier is StorageTier.HOT
    assert lenient_strategy.storage_tier is not StorageTier.HOT


# -- optimizer (savings estimate, theoretical not measured) -----------------------

def test_estimated_savings_is_zero_when_no_compression_selected():
    profile = build_profile(DataType.IMAGE, bytes(range(256)) * 8)
    strategy = engine.select(profile)
    assert strategy.compression is CompressionAlgorithm.NONE
    assert estimate_storage_savings(profile, strategy) == 0.0


def test_estimated_savings_is_positive_when_compression_selected():
    profile = build_profile(DataType.DOCUMENT, b"\x00" * 1000)
    strategy = engine.select(profile)
    assert strategy.compression is not CompressionAlgorithm.NONE
    assert estimate_storage_savings(profile, strategy) > 0.0
