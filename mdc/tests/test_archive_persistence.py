"""The ARCHIVE tier is persistent: archived objects survive a restart, hold
only ciphertext, and are re-keyed by key rotation."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mdc.databases.manager import DatabaseManager
from mdc.dna.crypto import DecryptionError
from mdc.dna.encoder import encode
from mdc.dna.storage import DNAStorageBackend
from mdc.events import EventStore, WrongKeyError
from mdc.schema.loader import load_default_registry
from mdc.storage.duckdb_store import DuckDBStore
from mdc.storage_intelligence.router import build_default_router
from mdc.storage_intelligence.strategy import StorageTier

OLD = b"\x03" * 32
NEW = b"\x04" * 32
T0 = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MDC_DNA_KEY", raising=False)
    monkeypatch.delenv("MDC_DNA_KEY_FILE", raising=False)
    monkeypatch.setattr("mdc.security.keys._DEFAULT_KEY_PATH", tmp_path / "default" / "dna.key")


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    s = DuckDBStore(tmp_path / "mdc.duckdb")
    s.init_schema()
    return s


def backend(store, key=OLD, **kw) -> DNAStorageBackend:
    return DNAStorageBackend(encryption_key=key, persist=store, **kw)


# -- the backend ----------------------------------------------------------------------


def test_blocks_survive_a_new_backend_on_the_same_store(store):
    first = backend(store)
    first.put("DOC:a", b"archived text", metadata={"object_id": "a", "object_type": "DOCUMENT"})
    created = first.metadata("DOC:a")["created_at"]

    second = backend(store)  # what a restart builds
    assert second.exists("DOC:a") and second.get("DOC:a") == b"archived text"
    info = second.metadata("DOC:a")
    assert info["metadata"] == {"object_id": "a", "object_type": "DOCUMENT"}
    assert info["checksum"] == first.metadata("DOC:a")["checksum"]
    assert info["created_at"] == created
    assert second.search() == ["DOC:a"] and second.search(object_id="a") == ["DOC:a"] and second.search(object_id="zzz") == []


def test_delete_and_overwrite_are_persisted(store):
    first = backend(store)
    first.put("b1", b"one")
    first.put("b2", b"two")
    first.put("b1", b"one again")
    first.delete("b2")
    second = backend(store)
    assert second.get("b1") == b"one again" and not second.exists("b2") and second.search() == ["b1"]
    with pytest.raises(KeyError):
        second.get("b2")


def test_only_ciphertext_reaches_the_store(store):
    secret = b"the archived launch codes are 0000"
    backend(store).put("b1", secret)
    raw = store.get("dna:b1")
    assert secret not in raw and encode(secret).encode() not in raw
    assert set(store.search(dna_archive="1")) == {"dna:b1"}


def test_the_wrong_key_cannot_read_the_stored_archive(store):
    backend(store, OLD).put("b1", b"classified")
    with pytest.raises(DecryptionError):
        backend(store, NEW).get("b1")


def test_a_block_stays_readable_if_ecc_copies_changes(store):
    backend(store, ecc_copies=3).put("b1", b"three copies")
    assert backend(store, ecc_copies=1).get("b1") == b"three copies"
    backend(store, ecc_copies=1).put("b2", b"one copy")
    assert backend(store, ecc_copies=3).get("b2") == b"one copy"


def test_corruption_research_still_works_on_stored_blocks(store):
    from mdc.dna.corruption import CorruptionRates

    b = backend(store, ecc_copies=3)
    b.put("b1", b"research payload")
    result = b.corrupt_and_recover("b1", CorruptionRates(), seed=1)
    assert result.data == b"research payload"
    assert backend(store, ecc_copies=3).get("b1") == b"research payload"  # the stored block was not touched


def test_opening_a_backend_does_not_create_a_key_file(store, tmp_path):
    b = DNAStorageBackend(persist=store)
    assert not (tmp_path / "default" / "dna.key").exists()
    b.put("b1", b"x")  # first use makes the key
    assert (tmp_path / "default" / "dna.key").exists()


def test_archive_rows_never_show_up_as_other_things(store):
    backend(store).put("DOC:a", b"x", metadata={"object_id": "a"})
    assert store.search(object_id="a") == [] and store.search(kind="event") == []
    assert EventStore(store, OLD).list_streams() == []


# -- through the router and the manager ---------------------------------------------------


def test_default_router_archive_is_persistent(store):
    router = build_default_router(store, encryption_key=OLD)
    archive = router.backends[StorageTier.ARCHIVE]
    assert isinstance(archive, DNAStorageBackend) and archive._persist is store


def test_an_archived_object_survives_a_restart(tmp_path):
    db = tmp_path / "data" / "mdc.duckdb"
    db.parent.mkdir()

    def boot():
        s = DuckDBStore(db)
        s.init_schema()
        return s, DatabaseManager(db.parent / "databases", s, load_default_registry(), event_master_key=OLD)

    s1, m1 = boot()
    service = m1.get("default").object_service
    uploaded = service.upload(b"# Quarterly notes\n\nKeep for seven years.", filename="notes.md")
    moved = service.move(uploaded["object_id"], StorageTier.ARCHIVE)
    assert moved["storage_tier"] == "ARCHIVE"
    s1.close()

    s2, m2 = boot()
    again = m2.get("default").object_service
    assert again.get_metadata(uploaded["object_id"])["storage_tier"] == "ARCHIVE"
    assert b"Keep for seven years." in again.read(uploaded["object_id"])
    s2.close()


def test_moving_out_of_the_archive_removes_the_stored_copy(store):
    from mdc.api.service import ObjectService

    service = ObjectService(build_default_router(store, encryption_key=OLD))
    oid = service.upload(b"# Notes\n\nbody", filename="n.md")["object_id"]
    service.move(oid, StorageTier.ARCHIVE)
    assert store.search(dna_archive="1")
    service.move(oid, StorageTier.COLD)
    assert store.search(dna_archive="1") == []
    assert b"body" in service.read(oid)


# -- key rotation -------------------------------------------------------------------------


def test_rotating_the_archive_rekeys_every_block(store):
    b = backend(store, ecc_copies=3)
    for i in range(4):
        b.put(f"b{i}", f"payload {i}".encode(), metadata={"n": i})
    before = {i: b.metadata(f"b{i}") for i in range(4)}
    report = b.rotate_key(NEW)
    assert (report.blocks, report.already_current) == (4, 0)
    reopened = backend(store, NEW, ecc_copies=3)
    assert [reopened.get(f"b{i}") for i in range(4)] == [f"payload {i}".encode() for i in range(4)]
    assert all(reopened.metadata(f"b{i}")["metadata"] == before[i]["metadata"] and reopened.metadata(f"b{i}")["created_at"] == before[i]["created_at"] for i in range(4))
    with pytest.raises(DecryptionError):
        backend(store, OLD).get("b0")
    assert b.get("b0") == b"payload 0"  # the rotated backend keeps working


def test_archive_rotation_with_a_wrong_current_key_changes_nothing(store):
    backend(store, OLD).put("b1", b"x")
    before = store.get("dna:b1")
    with pytest.raises(DecryptionError):
        backend(store, b"\x09" * 32).rotate_key(NEW)
    assert store.get("dna:b1") == before


def test_archive_rotation_to_the_same_key_is_refused(store):
    b = backend(store)
    b.put("b1", b"x")
    with pytest.raises(ValueError, match="same"):
        b.rotate_key(OLD)


def test_an_interrupted_archive_rotation_finishes_when_run_again(store, monkeypatch):
    b = backend(store)
    for i in range(4):
        b.put(f"b{i}", f"p{i}".encode())
    real_put, calls = store.put, {"n": 0}

    def dies(block_id, payload, metadata=None):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("power cut")
        return real_put(block_id, payload, metadata)

    monkeypatch.setattr(store, "put", dies)
    with pytest.raises(RuntimeError):
        b.rotate_key(NEW)
    monkeypatch.setattr(store, "put", real_put)
    report = backend(store, OLD).rotate_key(NEW)
    assert report.already_current == 2 and report.blocks == 2
    assert [backend(store, NEW).get(f"b{i}") for i in range(4)] == [f"p{i}".encode() for i in range(4)]


def make_manager(tmp_path: Path, key: bytes):
    s = DuckDBStore(tmp_path / "mdc.duckdb")
    s.init_schema()
    return s, DatabaseManager(tmp_path / "databases", s, load_default_registry(), event_master_key=key)


def test_manager_rotation_covers_streams_and_the_archive_in_every_database(tmp_path):
    s, m = make_manager(tmp_path, OLD)
    m.create("shop")
    for name in ("default", "shop"):
        m.events(name).create_stream("agent", {"mood": "calm"}, ts=T0)
        svc = m.get(name).object_service
        oid = svc.upload(b"# Notes\n\narchived in " + name.encode(), filename="n.md")["object_id"]
        svc.move(oid, StorageTier.ARCHIVE)
    reports = m.rotate_event_key(NEW)
    assert all(r.streams.records == 1 and r.archive.blocks >= 1 for r in reports.values())
    s.close()

    s2, m2 = make_manager(tmp_path, NEW)
    for name in ("default", "shop"):
        assert m2.events(name).state_at("agent").state == {"mood": "calm"}
        svc = m2.get(name).object_service
        [item] = [o for o in svc.list_objects() if o["storage_tier"] == "ARCHIVE"]
        assert name.encode() in svc.read(item["object_id"])
    s3, m3 = make_manager(tmp_path, OLD)
    with pytest.raises(WrongKeyError):
        m3.events("default").state_at("agent")


def test_a_rotation_across_databases_can_be_rerun_after_one_fails(tmp_path, monkeypatch):
    s, m = make_manager(tmp_path, OLD)
    m.create("shop")
    for name in ("default", "shop"):
        m.events(name).create_stream("agent", {"n": name}, ts=T0)
        svc = m.get(name).object_service
        svc.move(svc.upload(b"# N\n\n" + name.encode(), filename="n.md")["object_id"], StorageTier.ARCHIVE)
    shop_archive = m.get("shop").router.backends[StorageTier.ARCHIVE]
    real = shop_archive.rotate_key

    def fails(new_key):
        raise RuntimeError("disk full")

    monkeypatch.setattr(shop_archive, "rotate_key", fails)
    with pytest.raises(RuntimeError):
        m.rotate_event_key(NEW)  # "default" is done; "shop" has streams done but its archive not
    monkeypatch.setattr(shop_archive, "rotate_key", real)

    s.close()
    s2, m2 = make_manager(tmp_path, OLD)  # a fresh process, still configured with the OLD key
    reports = m2.rotate_event_key(NEW)
    assert reports["default"].streams.records == 0 and reports["default"].streams.already_current >= 1
    assert reports["shop"].archive.blocks >= 1
    s2.close()

    s3, m3 = make_manager(tmp_path, NEW)
    for name in ("default", "shop"):
        assert m3.events(name).state_at("agent").state == {"n": name}
        svc = m3.get(name).object_service
        [item] = [o for o in svc.list_objects() if o["storage_tier"] == "ARCHIVE"]
        assert name.encode() in svc.read(item["object_id"])
