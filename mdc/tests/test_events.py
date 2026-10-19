"""Event-sourced agent-state streams (events/): a base state plus an
append-only log, rebuilt to any point in time, everything sealed.
"""

import copy
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mdc.events import (
    ConcurrencyError,
    EventStore,
    InvalidEventError,
    InvalidStreamIdError,
    PathOpsCodec,
    StateCodec,
    StateNotYetError,
    StreamCorruptError,
    StreamExistsError,
    StreamNotFoundError,
    WrongKeyError,
)
from mdc.events.sealing import derive_stream_key
from mdc.storage.duckdb_store import DuckDBStore

KEY = b"\x07" * 32
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start: datetime = T0):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> datetime:
        self.now += timedelta(**delta)
        return self.now


@pytest.fixture
def storage(tmp_path: Path) -> DuckDBStore:
    store = DuckDBStore(tmp_path / "events.duckdb")
    store.init_schema()
    return store


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def events(storage: DuckDBStore, clock: Clock) -> EventStore:
    return EventStore(storage, KEY, clock=clock)


BASE = {"mood": "calm", "goal": "idle", "memory": {"facts": ["a"], "last_tool": None}}


def timeline(events: EventStore, clock: Clock, stream: str = "agent-1") -> list[dict]:
    """A stream with five hourly changes; returns the state after each step."""
    events.create_stream(stream, BASE)
    states = [copy.deepcopy(BASE)]
    for step in (
        {"mood": "busy"},
        {"goal": "write report"},
        {"memory": {"facts": ["a", "b"], "last_tool": "search"}},
        {"mood": "tired"},
        {"goal": None},
    ):
        clock.advance(hours=1)
        new = {**copy.deepcopy(states[-1]), **step}
        events.append(stream, state=new, note=f"step {len(states)}")
        states.append(new)
    return states


# -- the codec -------------------------------------------------------------------------

def test_codec_diff_then_apply_reproduces_the_new_state_for_random_changes():
    codec, rng = PathOpsCodec(), random.Random(1970)

    def value(depth=0):
        kind = rng.choice(["int", "float", "bool", "str", "null", "list", "dict" if depth < 2 else "int"])
        return {
            "int": lambda: rng.randint(-5, 5), "float": lambda: rng.choice([0.5, 1.0, 2.5]), "bool": lambda: rng.random() < 0.5,
            "str": lambda: rng.choice(["a", "b", ""]), "null": lambda: None,
            "list": lambda: [rng.randint(0, 3) for _ in range(rng.randint(0, 3))],
            "dict": lambda: {k: value(depth + 1) for k in rng.sample("abcde", rng.randint(0, 3))},
        }[kind]()

    for _ in range(300):
        old = {k: value() for k in rng.sample("abcde", rng.randint(0, 4))}
        new = {k: value() for k in rng.sample("abcde", rng.randint(0, 4))}
        assert codec.apply(old, codec.diff(old, new)) == new


def test_codec_tells_one_from_true_and_one_point_zero():
    codec = PathOpsCodec()
    assert codec.diff({"a": 1}, {"a": True}) == [{"op": "set", "path": ["a"], "value": True}]
    assert codec.diff({"a": 1}, {"a": 1.0}) == [{"op": "set", "path": ["a"], "value": 1.0}]
    assert codec.diff({"a": 1}, {"a": 1}) == []


def test_codec_can_set_a_value_to_null_and_remove_a_key():
    codec = PathOpsCodec()
    assert codec.apply({"a": 1, "b": 2}, [{"op": "set", "path": ["a"], "value": None}, {"op": "unset", "path": ["b"]}]) == {"a": None}


def test_codec_apply_does_not_mutate_its_input_and_edits_nested_keys():
    codec = PathOpsCodec()
    state = {"m": {"x": 1, "y": 2}}
    result = codec.apply(state, [{"op": "set", "path": ["m", "x"], "value": 9}, {"op": "set", "path": ["n", "deep"], "value": True}])
    assert state == {"m": {"x": 1, "y": 2}}
    assert result == {"m": {"x": 9, "y": 2}, "n": {"deep": True}}


def test_codec_unset_of_something_missing_is_a_no_op():
    assert PathOpsCodec().apply({"a": 1}, [{"op": "unset", "path": ["x", "y"]}]) == {"a": 1}


@pytest.mark.parametrize("payload", [
    "nope", [1], [{"op": "delete", "path": ["a"]}], [{"op": "set", "path": [], "value": 1}], [{"op": "set", "path": ["a"]}],
    [{"op": "set", "path": [1], "value": 1}], [{"op": "set", "path": ["a"] * 40, "value": 1}],
])
def test_codec_rejects_malformed_payloads(payload):
    with pytest.raises(InvalidEventError):
        PathOpsCodec().validate(payload)


def test_codec_refuses_to_write_through_a_non_object():
    with pytest.raises(InvalidEventError, match="not an object"):
        PathOpsCodec().apply({"a": 5}, [{"op": "set", "path": ["a", "b"], "value": 1}])


# -- creating, appending, rebuilding ---------------------------------------------------

def test_create_then_read_back_the_base_state(events: EventStore):
    view = events.create_stream("agent-1", BASE)
    assert (view.seq, view.ts) == (0, T0)
    assert events.state_at("agent-1").state == BASE


def test_state_at_any_time_rebuilds_exactly_that_moment(events: EventStore, clock: Clock):
    states = timeline(events, clock)
    for hour in range(0, 6):
        view = events.state_at("agent-1", at=T0 + timedelta(hours=hour, minutes=30))
        assert view.state == states[hour] and view.seq == hour
    assert events.state_at("agent-1", at=T0 + timedelta(days=30)).state == states[5]  # later than the last event


def test_the_boundary_is_inclusive_an_event_at_exactly_that_time_is_applied(events: EventStore, clock: Clock):
    states = timeline(events, clock)
    assert events.state_at("agent-1", at=T0 + timedelta(hours=2)).state == states[2]
    assert events.state_at("agent-1", at=T0 + timedelta(hours=2) - timedelta(microseconds=1)).state == states[1]


def test_state_at_event_number(events: EventStore, clock: Clock):
    states = timeline(events, clock)
    for n in range(6):
        assert events.state_at("agent-1", seq=n).state == states[n]
    with pytest.raises(InvalidEventError, match="no event 9"):
        events.state_at("agent-1", seq=9)
    with pytest.raises(InvalidEventError, match="not both"):
        events.state_at("agent-1", seq=1, at=T0)


def test_asking_for_a_time_before_the_stream_existed_is_an_error_not_an_empty_state(events: EventStore, clock: Clock):
    timeline(events, clock)
    with pytest.raises(StateNotYetError, match="did not exist yet"):
        events.state_at("agent-1", at=T0 - timedelta(seconds=1))


def test_naive_times_are_read_as_utc(events: EventStore, clock: Clock):
    states = timeline(events, clock)
    assert events.state_at("agent-1", at=datetime(2026, 10, 1, 13, 30)).state == states[1]


def test_snapshots_do_not_change_any_answer(storage: DuckDBStore, clock: Clock):
    frequent = EventStore(storage, KEY, clock=clock, snapshot_every=2)
    states = timeline(frequent, clock)
    assert len(storage.search(stream="agent-1", kind="snapshot")) == 2  # after events 2 and 4
    for n in range(6):
        assert frequent.state_at("agent-1", seq=n).state == states[n]
        assert frequent.state_at("agent-1", at=T0 + timedelta(hours=n, minutes=1)).state == states[n]


def test_a_snapshot_newer_than_the_requested_time_is_not_used(storage: DuckDBStore, clock: Clock):
    events = EventStore(storage, KEY, clock=clock, snapshot_every=2)
    states = timeline(events, clock)
    # at 3h30 the newest snapshot (event 4, at 4h) is in the future and must be skipped
    assert events.state_at("agent-1", at=T0 + timedelta(hours=3, minutes=30)).state == states[3]


def test_reads_survive_a_restart_and_appends_continue_the_sequence(storage: DuckDBStore, clock: Clock):
    first = EventStore(storage, KEY, clock=clock)
    states = timeline(first, clock)
    second = EventStore(storage, KEY, clock=clock)  # a new process over the same file
    assert second.state_at("agent-1").state == states[5]
    clock.advance(hours=1)
    event = second.append("agent-1", state={**states[5], "mood": "rested"})
    assert event.seq == 6
    assert second.state_at("agent-1").state["mood"] == "rested"


def test_appending_with_a_payload_instead_of_a_state(events: EventStore, clock: Clock):
    events.create_stream("agent-1", {"a": 1})
    clock.advance(minutes=1)
    event = events.append("agent-1", payload=[{"op": "set", "path": ["a"], "value": 2}], note="custom")
    assert event.payload == [{"op": "set", "path": ["a"], "value": 2}] and event.note == "custom"
    assert events.state_at("agent-1").state == {"a": 2}


def test_append_validates_what_it_is_given(events: EventStore):
    events.create_stream("agent-1", {"a": 1})
    with pytest.raises(InvalidEventError, match="exactly one"):
        events.append("agent-1")
    with pytest.raises(InvalidEventError, match="exactly one"):
        events.append("agent-1", state={"a": 2}, payload=[])
    with pytest.raises(InvalidEventError, match="Nothing changed"):
        events.append("agent-1", state={"a": 1})
    with pytest.raises(InvalidEventError, match="path-ops"):
        events.append("agent-1", payload="nonsense")
    with pytest.raises(StreamNotFoundError):
        events.append("ghost", state={"a": 1})


def test_stream_ids_and_duplicates(events: EventStore):
    for bad in ("", "../x", "a:b", "-lead", "has space", "x" * 65):
        with pytest.raises(InvalidStreamIdError):
            events.create_stream(bad, {})
    events.create_stream("ok.id_1-x", {})
    with pytest.raises(StreamExistsError):
        events.create_stream("ok.id_1-x", {})


def test_states_must_be_plain_json_objects(events: EventStore):
    with pytest.raises(InvalidEventError, match="keys must be strings"):
        events.create_stream("a", {1: "x"})
    with pytest.raises(InvalidEventError, match="not plain JSON"):
        events.create_stream("b", {"x": float("nan")})
    with pytest.raises(InvalidEventError, match="not plain JSON"):
        events.create_stream("c", {"x": object()})
    with pytest.raises(InvalidEventError, match="JSON object"):
        events.create_stream("d", ["not", "an", "object"])  # type: ignore[arg-type]


def test_a_record_over_the_size_limit_is_refused(storage: DuckDBStore):
    small = EventStore(storage, KEY, max_record_bytes=100)
    with pytest.raises(InvalidEventError, match="limit"):
        small.create_stream("big", {"blob": "x" * 500})


# -- time -------------------------------------------------------------------------------

def test_time_never_runs_backwards_even_if_the_clock_does(events: EventStore, clock: Clock):
    events.create_stream("agent-1", {"n": 0})
    clock.advance(hours=1)
    first = events.append("agent-1", state={"n": 1})
    clock.now -= timedelta(hours=3)  # the machine's clock jumps back
    second = events.append("agent-1", state={"n": 2})
    assert second.ts == first.ts


def test_a_supplied_time_may_backfill_but_not_rewrite_the_past_or_claim_the_future(events: EventStore, clock: Clock):
    events.create_stream("agent-1", {"n": 0}, ts=T0 - timedelta(days=1))
    events.append("agent-1", state={"n": 1}, ts=T0 - timedelta(hours=5))
    with pytest.raises(InvalidEventError, match="append-only"):
        events.append("agent-1", state={"n": 2}, ts=T0 - timedelta(hours=6))
    with pytest.raises(InvalidEventError, match="future"):
        events.append("agent-1", state={"n": 2}, ts=T0 + timedelta(hours=1))
    assert events.state_at("agent-1", at=T0 - timedelta(hours=4)).state == {"n": 1}


def test_expected_seq_guards_against_a_lost_update(events: EventStore):
    events.create_stream("agent-1", {"n": 0})
    events.append("agent-1", state={"n": 1}, expected_seq=0)
    with pytest.raises(ConcurrencyError, match="at event 1"):
        events.append("agent-1", state={"n": 2}, expected_seq=0)


def test_an_event_slot_that_is_already_taken_is_never_overwritten(storage: DuckDBStore, clock: Clock):
    one = EventStore(storage, KEY, clock=clock)
    other = EventStore(storage, KEY, clock=clock)
    one.create_stream("agent-1", {"n": 0})  # `one` now believes the next slot is 1
    other.append("agent-1", state={"n": 1})  # ...but another writer takes it
    taken = storage.get("evt:agent-1:000000000001")
    with pytest.raises(ConcurrencyError, match="already exists"):
        one.append("agent-1", state={"n": 99})
    assert storage.get("evt:agent-1:000000000001") == taken  # not overwritten
    # the stale view was dropped, so simply retrying works and lands in the next slot
    assert one.append("agent-1", state={"n": 2}).seq == 2
    assert one.state_at("agent-1").state == {"n": 2}


# -- history, listing, deleting -----------------------------------------------------------

def test_history_lists_the_log_oldest_first_with_notes_and_payloads(events: EventStore, clock: Clock):
    timeline(events, clock)
    log = events.history("agent-1")
    assert [(e.seq, e.kind) for e in log] == [(0, "base")] + [(n, "event") for n in range(1, 6)]
    assert log[0].payload == BASE and log[1].note == "step 1"
    assert log[1].payload == [{"op": "set", "path": ["mood"], "value": "busy"}]


def test_history_filters_by_time_and_limit(events: EventStore, clock: Clock):
    timeline(events, clock)
    assert [e.seq for e in events.history("agent-1", since=T0 + timedelta(hours=2), until=T0 + timedelta(hours=4))] == [2, 3, 4]
    assert [e.seq for e in events.history("agent-1", limit=2)] == [4, 5]


def test_list_streams_counts_events_without_decrypting_anything(storage: DuckDBStore, clock: Clock):
    events = EventStore(storage, KEY, clock=clock)
    timeline(events, clock, "alpha")
    events.create_stream("beta", {})
    stranger = EventStore(storage, b"\x99" * 32)  # a different key cannot read, but can list
    assert stranger.list_streams() == [{"stream": "alpha", "events": 5}, {"stream": "beta", "events": 0}]


def test_delete_stream_removes_every_record_and_the_id_can_be_reused(storage: DuckDBStore, clock: Clock):
    events = EventStore(storage, KEY, clock=clock, snapshot_every=2)
    timeline(events, clock)
    assert events.delete_stream("agent-1") is True
    assert storage.search(stream="agent-1") == []
    assert events.delete_stream("agent-1") is False
    with pytest.raises(StreamNotFoundError):
        events.state_at("agent-1")
    events.create_stream("agent-1", {"fresh": True})
    assert events.state_at("agent-1").state == {"fresh": True}


# -- what is on disk ----------------------------------------------------------------------

def test_nothing_readable_is_stored_not_content_not_keys_not_the_event_times(events: EventStore, storage: DuckDBStore, clock: Clock):
    events.create_stream("agent-1", {"secret_goal": "acquire the moon", "level": 41})
    clock.advance(hours=1)
    events.append("agent-1", state={"secret_goal": "acquire the moon", "level": 42}, note="a private note")
    ids = storage.search(stream="agent-1")
    # what a reader of the database sees per record: the sealed bytes and the stored metadata dict
    # (the storage layer's own write-time column is deliberately not part of this - see events.store)
    stored = b"".join(storage.get(i) for i in ids) + b"".join(str(storage.metadata(i)["metadata"]).encode() for i in ids)
    for marker in (b"secret_goal", b"acquire", b"moon", b"level", b"private note", b"2026", b"path-ops", b"set", b"calm"):
        assert marker not in stored, marker
    assert {tuple(sorted(storage.metadata(i)["metadata"])) for i in ids} == {("kind", "stream")}


def test_the_same_content_seals_differently_every_time(storage: DuckDBStore):
    events = EventStore(storage, KEY)
    events.create_stream("a", {"x": 1})
    events.create_stream("b", {"x": 1})
    assert storage.get("evt:a:000000000000") != storage.get("evt:b:000000000000")


def test_each_stream_has_its_own_key():
    assert derive_stream_key(KEY, "a") != derive_stream_key(KEY, "b")
    assert derive_stream_key(KEY, "a") == derive_stream_key(KEY, "a")
    assert derive_stream_key(KEY, "a") != derive_stream_key(b"\x08" * 32, "a")


def test_without_the_key_the_stream_cannot_be_read(events: EventStore, storage: DuckDBStore, clock: Clock):
    timeline(events, clock)
    with pytest.raises(WrongKeyError, match="not the one these streams were sealed with"):
        EventStore(storage, b"\x55" * 32).state_at("agent-1")


def test_a_flipped_byte_is_detected(events: EventStore, storage: DuckDBStore, clock: Clock):
    timeline(events, clock)
    blob = bytearray(storage.get("evt:agent-1:000000000003"))
    blob[20] ^= 0x01
    storage.put("evt:agent-1:000000000003", bytes(blob), metadata={"stream": "agent-1", "kind": "event"})
    with pytest.raises(StreamCorruptError, match="record 3"):
        events.state_at("agent-1")


def test_events_swapped_between_positions_are_detected(events: EventStore, storage: DuckDBStore, clock: Clock):
    timeline(events, clock)
    two, four = storage.get("evt:agent-1:000000000002"), storage.get("evt:agent-1:000000000004")
    storage.put("evt:agent-1:000000000002", four, metadata={"stream": "agent-1", "kind": "event"})
    storage.put("evt:agent-1:000000000004", two, metadata={"stream": "agent-1", "kind": "event"})
    with pytest.raises(StreamCorruptError):
        events.state_at("agent-1")


def test_an_event_copied_from_another_stream_is_detected(events: EventStore, storage: DuckDBStore, clock: Clock):
    timeline(events, clock, "victim")
    events.create_stream("target", BASE)
    clock.advance(hours=1)
    events.append("target", state={**BASE, "mood": "x"})
    storage.put("evt:target:000000000001", storage.get("evt:victim:000000000001"), metadata={"stream": "target", "kind": "event"})
    with pytest.raises(StreamCorruptError):
        EventStore(storage, KEY).state_at("target")


def test_a_deleted_event_is_a_hole_not_a_quiet_shortcut(events: EventStore, storage: DuckDBStore, clock: Clock):
    timeline(events, clock)
    storage.delete("evt:agent-1:000000000002")
    with pytest.raises(StreamCorruptError, match="event 2 is missing"):
        events.state_at("agent-1")
    with pytest.raises(StreamCorruptError, match="event 2 is missing"):
        events.history("agent-1")


def test_a_snapshot_cannot_be_forged_into_another_position(storage: DuckDBStore, clock: Clock):
    events = EventStore(storage, KEY, clock=clock, snapshot_every=2)
    timeline(events, clock)
    storage.put("snp:agent-1:000000000004", storage.get("snp:agent-1:000000000002"), metadata={"stream": "agent-1", "kind": "snapshot"})
    with pytest.raises(StreamCorruptError, match="snapshot record 4"):
        EventStore(storage, KEY).state_at("agent-1")


# -- the codec seam ------------------------------------------------------------------------

class WholeStateCodec(StateCodec):
    """What a model's own encoding looks like: here each event simply carries
    the full state, and replay takes the latest."""

    name = "whole-state"

    def diff(self, old, new):
        return {"state": new}

    def apply(self, state, payload):
        return dict(payload["state"])


def test_a_custom_codec_decides_how_changes_are_written_and_replayed(storage: DuckDBStore, clock: Clock):
    events = EventStore(storage, KEY, clock=clock, codecs={"whole-state": WholeStateCodec()})
    events.create_stream("agent-1", {"v": 0}, codec="whole-state")
    clock.advance(minutes=1)
    event = events.append("agent-1", state={"v": 1})
    assert event.payload == {"state": {"v": 1}}
    assert events.state_at("agent-1", seq=0).state == {"v": 0}
    assert events.state_at("agent-1").state == {"v": 1}


def test_a_stream_whose_codec_is_unavailable_is_reported_not_guessed(storage: DuckDBStore, clock: Clock):
    EventStore(storage, KEY, clock=clock, codecs={"whole-state": WholeStateCodec()}).create_stream("agent-1", {"v": 0}, codec="whole-state")
    with pytest.raises(StreamCorruptError, match='"whole-state" codec'):
        EventStore(storage, KEY).state_at("agent-1")
    with pytest.raises(InvalidEventError, match="Unknown codec"):
        EventStore(storage, KEY).create_stream("other", {}, codec="nope")
