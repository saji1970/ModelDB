"""Sealing-key management: where the key comes from, telling a wrong key from
tampering, and rotating streams to a new key."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from mdc.api.app import create_app
from mdc.app import app
from mdc.databases.manager import DatabaseManager
from mdc.events import EventStore, InvalidEventError, StreamCorruptError, WrongKeyError
from mdc.events.sealing import derive_stream_key
from mdc.schema.loader import load_default_registry
from mdc.security.keys import (
    KEY_LEN,
    find_dna_key,
    generate_key,
    key_fingerprint,
    key_notice,
    key_source,
    load_or_create_dna_key,
)
from mdc.security.tokens import TokenStore
from mdc.storage.duckdb_store import DuckDBStore

OLD = b"\x01" * 32
NEW = b"\x02" * 32
T0 = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
runner = CliRunner()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("MDC_DNA_KEY", raising=False)
    monkeypatch.delenv("MDC_DNA_KEY_FILE", raising=False)


@pytest.fixture
def storage(tmp_path: Path) -> DuckDBStore:
    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    return store


def fill(events: EventStore, stream: str = "agent-1", n: int = 5) -> None:
    events.create_stream(stream, {"mood": "calm", "level": 1}, ts=T0)
    for i in range(1, n + 1):
        events.append(stream, state={"mood": f"m{i}", "level": i + 1}, ts=T0 + timedelta(hours=i))


# -- where the key comes from -------------------------------------------------------


def test_key_file_location_can_be_set_off_the_default(tmp_path, monkeypatch):
    elsewhere = tmp_path / "secrets" / "sealing.key"
    monkeypatch.setenv("MDC_DNA_KEY_FILE", str(elsewhere))
    assert find_dna_key() is None  # looking never creates one
    assert not elsewhere.exists()
    key = load_or_create_dna_key()
    assert elsewhere.exists() and elsewhere.read_text().strip() == key.hex()
    assert find_dna_key() == key
    assert elsewhere.stat().st_mode & 0o777 == 0o600


def test_env_key_beats_the_key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MDC_DNA_KEY_FILE", str(tmp_path / "k"))
    load_or_create_dna_key()
    monkeypatch.setenv("MDC_DNA_KEY", "ab" * KEY_LEN)
    assert find_dna_key() == bytes.fromhex("ab" * KEY_LEN)
    assert key_source().kind == "env"


def test_a_corrupt_key_file_is_an_error_not_a_new_key(tmp_path, monkeypatch):
    bad = tmp_path / "k"
    bad.write_text("not a key")
    monkeypatch.setenv("MDC_DNA_KEY_FILE", str(bad))
    with pytest.raises(ValueError, match="does not contain a valid"):
        load_or_create_dna_key()
    assert bad.read_text() == "not a key"  # never overwritten


def test_fingerprint_identifies_without_revealing():
    a, b = generate_key(), generate_key()
    assert key_fingerprint(a) == key_fingerprint(a)
    assert key_fingerprint(a) != key_fingerprint(b)
    assert len(key_fingerprint(a)) == 8
    assert a.hex() not in key_fingerprint(a) and key_fingerprint(a) not in a.hex()


def test_startup_notice_says_where_the_key_is_without_printing_it(tmp_path, monkeypatch):
    monkeypatch.setenv("MDC_DNA_KEY_FILE", str(tmp_path / "k"))
    assert "none yet" in key_notice() and not (tmp_path / "k").exists()
    key = load_or_create_dna_key()
    notice = key_notice()
    assert str(tmp_path / "k") in notice and key_fingerprint(key) in notice and key.hex() not in notice
    monkeypatch.setenv("MDC_DNA_KEY", OLD.hex())
    notice = key_notice()
    assert "MDC_DNA_KEY (fingerprint" in notice and OLD.hex() not in notice


# -- a wrong key is not tampering ----------------------------------------------------


def test_the_wrong_key_is_reported_as_the_wrong_key(storage):
    fill(EventStore(storage, OLD))
    stranger = EventStore(storage, NEW)
    for call in (lambda: stranger.state_at("agent-1"), lambda: stranger.history("agent-1"), lambda: stranger.append("agent-1", state={"x": 1})):
        with pytest.raises(WrongKeyError, match="MDC_DNA_KEY"):
            call()
    with pytest.raises(WrongKeyError):
        stranger.create_stream("second", {})
    assert stranger.list_streams()  # listing needs no key
    assert EventStore(storage, OLD).state_at("agent-1").state["level"] == 6  # nothing was harmed


def test_tampering_with_the_right_key_is_still_corruption(storage):
    events = EventStore(storage, OLD)
    fill(events)
    blob = bytearray(storage.get("evt:agent-1:000000000003"))
    blob[20] ^= 1
    storage.put("evt:agent-1:000000000003", bytes(blob), metadata={"stream": "agent-1", "kind": "event"})
    with pytest.raises(StreamCorruptError):
        EventStore(storage, OLD).state_at("agent-1")


def test_a_database_from_before_key_checks_still_opens(storage):
    events = EventStore(storage, OLD)
    fill(events)
    storage.delete("keycheck:events")  # what an older database looks like
    assert EventStore(storage, OLD).state_at("agent-1").state["level"] == 6


# -- rotation ------------------------------------------------------------------------


def test_rotation_keeps_every_past_state_and_the_old_key_stops_working(storage):
    events = EventStore(storage, OLD, snapshot_every=2)
    fill(events, "a")
    fill(events, "b", n=3)
    before = {(s, n): events.state_at(s, seq=n).state for s, last in (("a", 5), ("b", 3)) for n in range(last + 1)}
    history = events.history("a")

    report = events.rotate_key(NEW)

    assert report.streams == 2 and report.already_current == 0
    assert report.records == len(storage.search(kind="event")) + len(storage.search(kind="snapshot"))
    reopened = EventStore(storage, NEW, snapshot_every=2)
    assert {(s, n): reopened.state_at(s, seq=n).state for (s, n) in before} == before
    assert reopened.history("a") == history
    assert events.state_at("a").state == reopened.state_at("a").state  # the same object keeps working
    with pytest.raises(WrongKeyError):
        EventStore(storage, OLD).state_at("a")
    reopened.append("a", state={"mood": "after", "level": 99})  # and it can keep writing
    assert reopened.state_at("a").state["mood"] == "after"


def test_rotation_never_stores_anything_readable(storage):
    events = EventStore(storage, OLD)
    events.create_stream("agent-1", {"secret_goal": "launch the moon"}, ts=T0)
    events.rotate_key(NEW)
    raw = b"".join(storage.get(b) for b in storage.search())
    assert b"secret_goal" not in raw and b"launch the moon" not in raw


def test_rotating_to_the_same_key_is_refused(storage):
    events = EventStore(storage, OLD)
    fill(events)
    with pytest.raises(InvalidEventError, match="same"):
        events.rotate_key(OLD)


def test_rotation_with_the_wrong_current_key_changes_nothing(storage):
    fill(EventStore(storage, OLD))
    snapshot = {b: storage.get(b) for b in storage.search()}
    with pytest.raises(WrongKeyError):
        EventStore(storage, b"\x09" * 32).rotate_key(NEW)
    assert {b: storage.get(b) for b in storage.search()} == snapshot


def test_a_damaged_record_stops_rotation_before_anything_is_written(storage):
    events = EventStore(storage, OLD)
    fill(events)
    blob = bytearray(storage.get("evt:agent-1:000000000002"))
    blob[20] ^= 1
    storage.put("evt:agent-1:000000000002", bytes(blob), metadata={"stream": "agent-1", "kind": "event"})
    snapshot = {b: storage.get(b) for b in storage.search()}
    with pytest.raises(StreamCorruptError, match="neither the old nor the new key"):
        events.rotate_key(NEW)
    assert {b: storage.get(b) for b in storage.search()} == snapshot


def test_an_interrupted_rotation_finishes_when_run_again(storage, monkeypatch):
    events = EventStore(storage, OLD)
    fill(events)
    total = len(storage.search(kind="event"))
    real_put, calls = storage.put, {"n": 0}

    def dies_part_way(block_id, payload, metadata=None):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("power cut")
        return real_put(block_id, payload, metadata)

    monkeypatch.setattr(storage, "put", dies_part_way)
    with pytest.raises(RuntimeError):
        events.rotate_key(NEW)
    monkeypatch.setattr(storage, "put", real_put)

    resumed = EventStore(storage, OLD).rotate_key(NEW)  # same command, same keys
    assert resumed.already_current == 2 and resumed.records == total - 2
    assert EventStore(storage, NEW).state_at("agent-1").state["level"] == 6


def test_rotating_an_empty_database_is_a_no_op(storage):
    report = EventStore(storage, OLD).rotate_key(NEW)
    assert (report.streams, report.records) == (0, 0)
    assert not storage.exists("keycheck:events")


def test_the_key_check_block_is_not_a_stream(storage):
    events = EventStore(storage, OLD)
    fill(events)
    assert [s["stream"] for s in events.list_streams()] == ["agent-1"]
    assert derive_stream_key(OLD, "__keycheck__") != derive_stream_key(OLD, "agent-1")


# -- manager, API and CLI --------------------------------------------------------------


def make_manager(tmp_path: Path, key: bytes) -> tuple[DuckDBStore, DatabaseManager]:
    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    return store, DatabaseManager(tmp_path / "databases", store, load_default_registry(), event_master_key=key)


def test_manager_rotates_every_database(tmp_path):
    store, manager = make_manager(tmp_path, OLD)
    manager.create("shop")
    fill(manager.events("default"))
    fill(manager.events("shop"), "cart")
    reports = manager.rotate_event_key(NEW)
    assert {n: r.streams.streams for n, r in reports.items()} == {"default": 1, "shop": 1}
    assert manager.events("shop").state_at("cart").state["level"] == 6
    store.close()
    _, again = make_manager(tmp_path, NEW)
    assert again.events("default").state_at("agent-1").state["level"] == 6
    _, wrong = make_manager(tmp_path, OLD)
    with pytest.raises(WrongKeyError):
        wrong.events("default").state_at("agent-1")


def test_api_says_the_key_is_wrong_not_that_data_was_altered(tmp_path):
    store, manager = make_manager(tmp_path, OLD)
    fill(manager.events("default"))
    store.close()
    _, wrong = make_manager(tmp_path, NEW)
    tokens = TokenStore(tmp_path / "t.json")
    client = TestClient(create_app(wrong, token_store=tokens))
    client.headers.update({"Authorization": f"Bearer {tokens.issue('c')}"})
    response = client.get("/databases/default/streams/agent-1")
    assert response.status_code == 500
    assert "not the one these streams were sealed with" in response.json()["detail"]
    assert "altered" not in response.json()["detail"]


def run_cli(*args: str, env: dict | None = None):
    return runner.invoke(app, list(args), env=env)


def seeded(tmp_path: Path, key: bytes) -> Path:
    db = tmp_path / "data" / "mdc.duckdb"
    db.parent.mkdir()
    store = DuckDBStore(db)
    store.init_schema()
    manager = DatabaseManager(db.parent / "databases", store, load_default_registry(), event_master_key=key)
    fill(manager.events("default"))
    store.close()
    return db


def test_cli_generate_prints_a_usable_key():
    result = run_cli("key", "generate")
    assert result.exit_code == 0
    assert len(bytes.fromhex(result.output.strip())) == KEY_LEN


def test_cli_show_never_prints_the_key(tmp_path):
    result = run_cli("key", "show", env={"MDC_DNA_KEY": OLD.hex()})
    assert result.exit_code == 0 and key_fingerprint(OLD) in result.output and OLD.hex() not in result.output


def test_cli_rotate_to_a_key_from_the_environment(tmp_path):
    db = seeded(tmp_path, OLD)
    result = run_cli("key", "rotate", "--database", str(db), "--new-key-env", "NEWKEY", env={"MDC_DNA_KEY": OLD.hex(), "NEWKEY": NEW.hex()})
    assert result.exit_code == 0, result.output
    assert "1 stream(s)" in result.output and "archive: 0 object block(s)" in result.output and key_fingerprint(NEW) in result.output
    store = DuckDBStore(db)
    assert EventStore(store, NEW).state_at("agent-1").state["level"] == 6
    store.close()


def test_cli_rotate_generate_replaces_a_key_file_and_keeps_the_old_one(tmp_path):
    keyfile = tmp_path / "sealing.key"
    keyfile.write_text(OLD.hex())
    db = seeded(tmp_path, OLD)
    result = run_cli("key", "rotate", "--database", str(db), "--generate", env={"MDC_DNA_KEY_FILE": str(keyfile)})
    assert result.exit_code == 0, result.output
    new = bytes.fromhex(keyfile.read_text())
    assert new != OLD
    backup = tmp_path / f"sealing.key.{key_fingerprint(OLD)}.old"
    assert bytes.fromhex(backup.read_text()) == OLD
    assert not (tmp_path / "sealing.key.new").exists()
    store = DuckDBStore(db)
    assert EventStore(store, new).state_at("agent-1").state["level"] == 6
    store.close()


def test_cli_rotate_generate_with_an_env_key_prints_the_new_key_first(tmp_path):
    db = seeded(tmp_path, OLD)
    result = run_cli("key", "rotate", "--database", str(db), "--generate", env={"MDC_DNA_KEY": OLD.hex()})
    assert result.exit_code == 0, result.output
    printed = [line for line in result.output.splitlines() if len(line) == KEY_LEN * 2 and all(c in "0123456789abcdef" for c in line)]
    assert len(printed) == 1
    assert result.output.index(printed[0]) < result.output.index("stream(s)")  # shown before anything changed
    store = DuckDBStore(db)
    assert EventStore(store, bytes.fromhex(printed[0])).state_at("agent-1").state["level"] == 6
    store.close()


def test_cli_rotate_checks_its_arguments(tmp_path):
    db = seeded(tmp_path, OLD)
    env = {"MDC_DNA_KEY": OLD.hex()}
    assert run_cli("key", "rotate", "--database", str(db), env=env).exit_code == 1  # no new key given
    both = run_cli("key", "rotate", "--database", str(db), "--generate", "--new-key-env", "X", env=env)
    assert both.exit_code == 1
    same = run_cli("key", "rotate", "--database", str(db), "--new-key-env", "SAME", env={**env, "SAME": OLD.hex()})
    assert same.exit_code == 1 and "same" in same.output
    bad = run_cli("key", "rotate", "--database", str(db), "--new-key-env", "BAD", env={**env, "BAD": "xyz"})
    assert bad.exit_code == 1
    store = DuckDBStore(db)
    assert EventStore(store, OLD).state_at("agent-1").state["level"] == 6  # none of that touched the data
    store.close()


def test_cli_rotate_with_the_wrong_current_key_stops_and_changes_nothing(tmp_path):
    db = seeded(tmp_path, OLD)
    result = run_cli("key", "rotate", "--database", str(db), "--new-key-env", "N", env={"MDC_DNA_KEY": ("09" * 32), "N": NEW.hex()})
    assert result.exit_code == 1 and "Rotation stopped" in result.output
    store = DuckDBStore(db)
    assert EventStore(store, OLD).state_at("agent-1").state["level"] == 6
    store.close()


def test_cli_rotate_with_no_key_has_nothing_to_do(tmp_path):
    result = run_cli("key", "rotate", "--database", str(tmp_path / "x.duckdb"), "--generate", env={"MDC_DNA_KEY_FILE": str(tmp_path / "none.key")})
    assert result.exit_code == 1 and "nothing sealed" in result.output
    assert not (tmp_path / "none.key").exists()
