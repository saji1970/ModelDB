"""Phase F: real compression (CLAUDE-STORAGE.md section 15, required test
`test_compression_roundtrip` in section 48).
"""

from pathlib import Path

import pytest

from mdc.classification.classifier import classify_and_profile
from mdc.classification.data_type import DataType
from mdc.compression.compressor import (
    CompressionError,
    CompressionNotAvailableError,
    GzipCompressor,
    NoneCompressor,
    ZlibCompressor,
    compress,
    decompress,
    get_compressor,
)
from mdc.model.object import MDCObject, generate_object_id, utcnow
from mdc.storage.duckdb_store import DuckDBStore
from mdc.storage_intelligence.optimizer import estimate_storage_savings, measured_compression_ratio
from mdc.storage_intelligence.router import DataIntegrityError, build_default_router
from mdc.storage_intelligence.strategy import CompressionAlgorithm, StorageStrategyEngine


# -- section 48 required test: compress -> decompress round trips ----------------

@pytest.mark.parametrize("compressor_cls", [NoneCompressor, ZlibCompressor, GzipCompressor])
def test_compression_roundtrip(compressor_cls):
    compressor = compressor_cls()
    original = b"hello molecular world " * 100
    compressed = compressor.compress(original)
    assert compressor.decompress(compressed) == original


def test_compress_decompress_helpers_dispatch_by_algorithm():
    original = b"repeat me " * 500
    for algorithm in (CompressionAlgorithm.NONE, CompressionAlgorithm.ZLIB, CompressionAlgorithm.GZIP):
        assert decompress(compress(original, algorithm), algorithm) == original


def test_zlib_and_gzip_actually_shrink_repetitive_data():
    original = b"A" * 100_000
    for algorithm in (CompressionAlgorithm.ZLIB, CompressionAlgorithm.GZIP):
        compressed = compress(original, algorithm)
        assert len(compressed) < len(original) // 100  # dramatic, real shrinkage


def test_none_compressor_is_a_true_passthrough():
    original = b"anything at all"
    assert compress(original, CompressionAlgorithm.NONE) == original


def test_lz4_and_zstd_are_not_available_and_say_so_clearly():
    for algorithm in (CompressionAlgorithm.LZ4, CompressionAlgorithm.ZSTD):
        with pytest.raises(CompressionNotAvailableError):
            get_compressor(algorithm)


def test_decompressing_garbage_raises_compression_error():
    with pytest.raises(CompressionError):
        ZlibCompressor().decompress(b"not zlib data at all")
    with pytest.raises(CompressionError):
        GzipCompressor().decompress(b"not gzip data at all")


# -- integration: the router actually compresses on the wire ---------------------

@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    duckdb_store = DuckDBStore(tmp_path / "mdc.duckdb")
    duckdb_store.init_schema()
    return duckdb_store


def test_router_physically_shrinks_compressible_content_on_disk(store: DuckDBStore):
    router = build_default_router(store)
    engine = StorageStrategyEngine()
    content = b"repeat this line.\n" * 5000

    profile = classify_and_profile(content, "log.txt")
    strategy = engine.select(profile)
    assert strategy.compression is not CompressionAlgorithm.NONE

    now = utcnow()
    obj = MDCObject(object_id=generate_object_id(profile.data_type), object_type=profile.data_type, size=len(content), created_at=now, updated_at=now)
    entry = router.store(obj, content, strategy)

    physical_bytes = router.backends[entry.storage_tier].get(entry.location)
    assert len(physical_bytes) < len(content)
    assert entry.compressed_size == len(physical_bytes)
    assert entry.size == len(content)

    assert router.retrieve(obj.object_id) == content


def test_router_does_not_compress_when_strategy_says_none(store: DuckDBStore):
    router = build_default_router(store)
    engine = StorageStrategyEngine()
    content = bytes(range(256)) * 4  # high-entropy - NONE selected

    profile = classify_and_profile(content, "photo.jpg")
    strategy = engine.select(profile)
    assert strategy.compression is CompressionAlgorithm.NONE

    now = utcnow()
    obj = MDCObject(object_id=generate_object_id(profile.data_type), object_type=profile.data_type, size=len(content), created_at=now, updated_at=now)
    entry = router.store(obj, content, strategy)

    assert entry.compressed_size is None
    physical_bytes = router.backends[entry.storage_tier].get(entry.location)
    assert physical_bytes == content  # true passthrough, byte for byte


def test_corrupted_compressed_bytes_raise_data_integrity_error_not_a_raw_codec_error(store: DuckDBStore):
    router = build_default_router(store)
    engine = StorageStrategyEngine()
    content = b"repeat this line.\n" * 5000
    strategy = engine.select(classify_and_profile(content, "log.txt"))
    assert strategy.compression is not CompressionAlgorithm.NONE

    now = utcnow()
    obj = MDCObject(object_id=generate_object_id(DataType.LOG), object_type=DataType.LOG, size=len(content), created_at=now, updated_at=now)
    entry = router.store(obj, content, strategy)

    router.backends[entry.storage_tier].put(entry.location, b"not valid compressed data", metadata={"object_id": obj.object_id})

    with pytest.raises(DataIntegrityError):
        router.retrieve(obj.object_id)


def test_move_preserves_physical_compressed_bytes_and_logical_checksum(store: DuckDBStore):
    from mdc.storage_intelligence.strategy import StorageTier

    router = build_default_router(store)
    engine = StorageStrategyEngine()
    content = b"repeat this line.\n" * 5000
    profile = classify_and_profile(content, "log.txt")
    strategy = engine.select(profile)

    now = utcnow()
    obj = MDCObject(object_id=generate_object_id(DataType.LOG), object_type=DataType.LOG, size=len(content), created_at=now, updated_at=now)
    entry = router.store(obj, content, strategy)
    original_checksum = entry.checksum

    moved = router.move(obj.object_id, StorageTier.ARCHIVE)
    assert moved.checksum == original_checksum
    assert moved.compressed_size == entry.compressed_size
    assert router.retrieve(obj.object_id) == content


# -- optimizer: theoretical estimate vs. real measured ratio ---------------------

def test_measured_compression_ratio_is_none_when_not_compressed(store: DuckDBStore):
    router = build_default_router(store)
    engine = StorageStrategyEngine()
    content = bytes(range(256)) * 4
    strategy = engine.select(classify_and_profile(content, "photo.jpg"))
    now = utcnow()
    obj = MDCObject(object_id=generate_object_id(DataType.IMAGE), object_type=DataType.IMAGE, size=len(content), created_at=now, updated_at=now)
    entry = router.store(obj, content, strategy)
    assert measured_compression_ratio(entry) is None


def test_measured_compression_ratio_reflects_real_savings(store: DuckDBStore):
    router = build_default_router(store)
    engine = StorageStrategyEngine()
    content = b"A" * 100_000
    profile = classify_and_profile(content, "log.txt")
    strategy = engine.select(profile)
    now = utcnow()
    obj = MDCObject(object_id=generate_object_id(DataType.LOG), object_type=DataType.LOG, size=len(content), created_at=now, updated_at=now)
    entry = router.store(obj, content, strategy)

    measured = measured_compression_ratio(entry)
    theoretical = estimate_storage_savings(profile, strategy)
    assert measured is not None
    assert measured > 0.99  # near-total savings on all-'A' content
    # Both express "expected savings" but from different sources (real vs.
    # entropy-derived) - they need not be numerically identical.
    assert theoretical > 0.9
