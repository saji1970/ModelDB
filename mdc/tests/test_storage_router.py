"""Phase C: ObjectIndex + StorageRouter (CLAUDE-STORAGE.md sections 32, 34,
39, 44, required tests in section 48's API category).
"""

from pathlib import Path

import pytest

from mdc.classification.classifier import classify_and_profile
from mdc.classification.data_type import DataType
from mdc.classification.metadata import build_profile
from mdc.index.object_index import IndexEntry, ObjectIndex, utcnow
from mdc.model.object import MDCObject, generate_object_id
from mdc.storage.duckdb_store import DuckDBStore
from mdc.storage.memory_store import MemoryStorageBackend
from mdc.storage_intelligence.analyzer import AccessProfile
from mdc.storage_intelligence.router import (
    DataIntegrityError,
    ObjectNotFoundError,
    ObjectUnavailableError,
    StorageRouter,
    build_default_router,
)
from mdc.storage_intelligence.strategy import CompressionAlgorithm, Representation, StorageStrategyEngine, StorageTier


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    duckdb_store = DuckDBStore(tmp_path / "mdc.duckdb")
    duckdb_store.init_schema()
    return duckdb_store


@pytest.fixture
def router(store: DuckDBStore) -> StorageRouter:
    return build_default_router(store)


def _make_object(data_type: DataType, size: int) -> MDCObject:
    now = utcnow()
    return MDCObject(object_id=generate_object_id(data_type), object_type=data_type, size=size, created_at=now, updated_at=now)


# -- section 48 "API" tests, exercised at the router level (the API's future
# implementation is a thin wrapper over exactly these calls) --------------------

def test_upload_object(router: StorageRouter):
    strategy = StorageStrategyEngine().select(build_profile(DataType.DOCUMENT, b"hello world"))
    obj = _make_object(DataType.DOCUMENT, 11)
    entry = router.store(obj, b"hello world", strategy)
    assert entry.object_id == obj.object_id
    assert entry.size == 11
    assert router.index.get(obj.object_id) is not None


def test_retrieve_object(router: StorageRouter):
    strategy = StorageStrategyEngine().select(build_profile(DataType.TEXT, b"payload"))
    obj = _make_object(DataType.TEXT, 7)
    router.store(obj, b"payload", strategy)
    assert router.retrieve(obj.object_id) == b"payload"


def test_delete_object(router: StorageRouter):
    strategy = StorageStrategyEngine().select(build_profile(DataType.TEXT, b"payload"))
    obj = _make_object(DataType.TEXT, 7)
    router.store(obj, b"payload", strategy)
    router.delete(obj.object_id)
    assert router.index.get(obj.object_id) is None
    with pytest.raises(ObjectNotFoundError):
        router.retrieve(obj.object_id)


# -- routing actually lands objects on different backends by tier ----------------

def test_hot_object_lands_on_memory_backend_warm_lands_on_duckdb(router: StorageRouter):
    engine = StorageStrategyEngine()

    hot_profile = build_profile(DataType.AI_MODEL, b"\x00" * 100)
    hot_obj = _make_object(DataType.AI_MODEL, 100)
    hot_entry = router.store(hot_obj, b"\x00" * 100, engine.select(hot_profile, AccessProfile(access_frequency=100.0)))
    assert hot_entry.storage_tier is StorageTier.HOT
    assert isinstance(router.backends[StorageTier.HOT], MemoryStorageBackend)
    assert router.backends[StorageTier.HOT].exists(hot_entry.location)

    cold_profile = build_profile(DataType.IMAGE, b"\x00" * 100)
    cold_obj = _make_object(DataType.IMAGE, 100)
    cold_entry = router.store(cold_obj, b"\x00" * 100, engine.select(cold_profile, AccessProfile()))
    assert cold_entry.storage_tier is StorageTier.COLD
    assert not router.backends[StorageTier.HOT].exists(cold_entry.location)


def test_move_transfers_bytes_between_backends_and_deletes_from_the_old_one(router: StorageRouter):
    strategy = StorageStrategyEngine().select(build_profile(DataType.AI_MODEL, b"\x00" * 100), AccessProfile(access_frequency=100.0))
    obj = _make_object(DataType.AI_MODEL, 100)
    entry = router.store(obj, b"\x00" * 100, strategy)
    assert entry.storage_tier is StorageTier.HOT
    hot_backend = router.backends[StorageTier.HOT]
    assert hot_backend.exists(entry.location)

    moved = router.move(obj.object_id, StorageTier.ARCHIVE)
    assert moved.storage_tier is StorageTier.ARCHIVE
    assert not hot_backend.exists(entry.location)
    assert router.retrieve(obj.object_id) == b"\x00" * 100


def test_move_to_the_same_tier_is_a_no_op(router: StorageRouter):
    strategy = StorageStrategyEngine().select(build_profile(DataType.TEXT, b"x"))
    obj = _make_object(DataType.TEXT, 1)
    entry = router.store(obj, b"x", strategy)
    moved = router.move(obj.object_id, entry.storage_tier)
    assert moved == entry


# -- data integrity (section 39) --------------------------------------------------

def test_retrieve_detects_corrupted_bytes(router: StorageRouter):
    strategy = StorageStrategyEngine().select(build_profile(DataType.TEXT, b"original"))
    obj = _make_object(DataType.TEXT, 8)
    entry = router.store(obj, b"original", strategy)

    router.backends[entry.storage_tier].put(entry.location, b"corrupted!", metadata={"object_id": obj.object_id})

    with pytest.raises(DataIntegrityError):
        router.retrieve(obj.object_id)


def test_retrieve_can_skip_integrity_check(router: StorageRouter):
    # High-entropy content so the strategy picks CompressionAlgorithm.NONE -
    # verify_integrity=False means "skip the checksum comparison," not
    # "skip decompression," so corrupting the raw stored bytes only stays
    # meaningful to assert on when there's no compression in the way.
    content = bytes(range(256)) * 4
    strategy = StorageStrategyEngine().select(build_profile(DataType.TEXT, content))
    assert strategy.compression is CompressionAlgorithm.NONE
    obj = _make_object(DataType.TEXT, len(content))
    entry = router.store(obj, content, strategy)
    router.backends[entry.storage_tier].put(entry.location, b"corrupted!", metadata={"object_id": obj.object_id})

    assert router.retrieve(obj.object_id, verify_integrity=False) == b"corrupted!"


# -- HOT tier survives a lookup after the in-memory backend is emptied ----------
#
# `MemoryStorageBackend` is deliberately not persisted across a process
# restart (its own module docstring), but `ObjectIndex` always rides the
# durable backend - so a fresh process can end up with an index entry
# that still claims HOT for an object whose bytes are simply gone. This
# reproduces exactly that (swap in a fresh, empty `MemoryStorageBackend`
# without touching the index, the same shape a real restart produces)
# and checks it surfaces as a clean, catchable error instead of an
# unhandled `KeyError` bubbling out of the storage layer.

def test_retrieve_after_hot_backend_loses_its_content_raises_a_clean_error(router: StorageRouter):
    strategy = StorageStrategyEngine().select(build_profile(DataType.AI_MODEL, b"weights"), AccessProfile(access_frequency=100.0))
    obj = _make_object(DataType.AI_MODEL, 7)
    router.store(obj, b"weights", strategy)
    assert router.index.get(obj.object_id).storage_tier is StorageTier.HOT

    router.backends[StorageTier.HOT] = MemoryStorageBackend()  # simulates a restart

    with pytest.raises(ObjectUnavailableError) as excinfo:
        router.retrieve(obj.object_id)
    assert excinfo.value.tier is StorageTier.HOT
    assert isinstance(excinfo.value, ObjectNotFoundError)  # existing broad "not found" handlers still catch it


def test_move_off_an_emptied_hot_backend_also_raises_a_clean_error(router: StorageRouter):
    strategy = StorageStrategyEngine().select(build_profile(DataType.AI_MODEL, b"weights"), AccessProfile(access_frequency=100.0))
    obj = _make_object(DataType.AI_MODEL, 7)
    router.store(obj, b"weights", strategy)

    router.backends[StorageTier.HOT] = MemoryStorageBackend()  # simulates a restart

    with pytest.raises(ObjectUnavailableError):
        router.move(obj.object_id, StorageTier.WARM)


def test_operating_on_an_unknown_object_id_raises_not_found(router: StorageRouter):
    with pytest.raises(ObjectNotFoundError):
        router.retrieve("nope")
    with pytest.raises(ObjectNotFoundError):
        router.delete("nope")
    with pytest.raises(ObjectNotFoundError):
        router.move("nope", StorageTier.HOT)


# -- ObjectIndex --------------------------------------------------------------------

def test_object_index_register_get_delete(store: DuckDBStore):
    index = ObjectIndex(store)
    entry = IndexEntry(
        object_id="OBJ-1", object_type=DataType.IMAGE, storage_backend="MemoryStorageBackend",
        storage_tier=StorageTier.HOT, location="IMAGE:OBJ-1", size=10, checksum="abc",
        compression=CompressionAlgorithm.NONE, representation=Representation.RAW, indexed_at=utcnow(),
    )
    index.register(entry)
    fetched = index.get("OBJ-1")
    assert fetched is not None
    assert fetched.object_type is DataType.IMAGE

    index.delete("OBJ-1")
    assert index.get("OBJ-1") is None


def test_object_index_search_by_type_and_tier(store: DuckDBStore):
    index = ObjectIndex(store)
    for i, (data_type, tier) in enumerate([
        (DataType.IMAGE, StorageTier.HOT), (DataType.IMAGE, StorageTier.COLD), (DataType.DOCUMENT, StorageTier.WARM)
    ]):
        index.register(IndexEntry(
            object_id=f"OBJ-{i}", object_type=data_type, storage_backend="x", storage_tier=tier,
            location=f"loc-{i}", size=1, checksum="c", compression=CompressionAlgorithm.NONE,
            representation=Representation.RAW, indexed_at=utcnow(),
        ))

    images = index.search(object_type=DataType.IMAGE)
    assert {e.object_id for e in images} == {"OBJ-0", "OBJ-1"}

    hot_only = index.search(storage_tier=StorageTier.HOT)
    assert {e.object_id for e in hot_only} == {"OBJ-0"}


def test_object_index_does_not_leak_unrelated_blocks_from_a_shared_backend(store: DuckDBStore):
    # The DuckDB store used for the index is the same physical backend
    # WARM/COLD/ARCHIVE data lives on - the index must only ever surface
    # its own entries, never raw object blocks that happen to share it.
    store.put("some:other:block", b"not an index entry", metadata={"object_type": "IMAGE"})
    index = ObjectIndex(store)
    index.register(IndexEntry(
        object_id="OBJ-1", object_type=DataType.IMAGE, storage_backend="x", storage_tier=StorageTier.WARM,
        location="loc", size=1, checksum="c", compression=CompressionAlgorithm.NONE,
        representation=Representation.RAW, indexed_at=utcnow(),
    ))
    assert [e.object_id for e in index.search()] == ["OBJ-1"]


# -- end-to-end: classify -> profile -> strategy -> store -> retrieve -----------

def test_end_to_end_classify_to_router_round_trip(router: StorageRouter):
    content = b"%PDF-1.7\n" + b"body" * 100
    profile = classify_and_profile(content, "report.pdf")
    assert profile.data_type is DataType.DOCUMENT

    strategy = StorageStrategyEngine().select(profile)
    obj = _make_object(profile.data_type, profile.size_bytes)
    router.store(obj, content, strategy)

    assert router.retrieve(obj.object_id) == content
