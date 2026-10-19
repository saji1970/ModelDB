"""EventStore: event-sourced state with time travel, sealed at rest.

A *stream* is one agent's (or anything's) state history:

    seq 0   base      the starting state
    seq 1+  event     one change each, with the time it happened
    snapshot          a sealed copy of the state every `snapshot_every`
                      events, only so rebuilding a long stream is fast

`state_at(stream, at=...)` rebuilds the state exactly as it was at that
time by replaying the log through the stream's codec; `seq=` does the same
for "after event N". Nothing is ever updated in place - appending is the
only way a stream changes - so every earlier state stays recoverable.

Everything written is sealed (`sealing.py`) *including the event
timestamps*. What is visible to anyone reading the storage is the stream
id, how many records there are, their sizes, and the moment each record
was written (the storage layer stamps every block) - so for live appends
the rough time of each change is not hidden, only recorded times that
were back-filled with `ts=` are. What is not visible is what the state
is, what changed, and the event times themselves. This is encryption
with a server-held key, not a claim that the key is hardware-protected;
the master key is the same trust anchor the DNA tier uses
(`security/keys.py`).

Writes are serialized by a lock, so this assumes one writing process per
database file - which is how DuckDB is used here anyway. A second writer
would be caught by the sequence check and raise `ConcurrencyError` rather
than overwrite an event.
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from mdc.events.codec import PathOpsCodec, StateCodec
from mdc.events.errors import (
    ConcurrencyError,
    InvalidEventError,
    InvalidStreamIdError,
    StateNotYetError,
    StreamCorruptError,
    StreamExistsError,
    StreamNotFoundError,
)
from mdc.events.sealing import derive_stream_key, open_sealed, seal
from mdc.storage.interface import StorageBackend

STREAM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_EVENT_PREFIX = "evt:"
_SNAPSHOT_PREFIX = "snp:"
_MAX_CLOCK_SKEW = timedelta(minutes=5)
_CACHE_SIZE = 256


@dataclass(frozen=True)
class StreamEvent:
    seq: int
    ts: datetime
    kind: str  # "base" | "event"
    note: str | None
    payload: Any  # for "base": the starting state; for "event": the codec payload


@dataclass(frozen=True)
class StateView:
    stream: str
    state: dict[str, Any]
    seq: int  # the last event applied (0 = just the base)
    ts: datetime  # when that event happened


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _event_id(stream: str, seq: int) -> str:
    return f"{_EVENT_PREFIX}{stream}:{seq:012d}"


def _snapshot_id(stream: str, seq: int) -> str:
    return f"{_SNAPSHOT_PREFIX}{stream}:{seq:012d}"


def _seq_of(block_id: str) -> int:
    return int(block_id.rsplit(":", 1)[1])


def _stream_of(block_id: str) -> str:
    return block_id.split(":", 2)[1]


def _canonical(value: Any, what: str, max_bytes: int) -> Any:
    """`value` as it will read back from JSON; refuses what JSON would
    silently change (non-string keys) or cannot hold (NaN, objects)."""

    def check(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str):
                    raise InvalidEventError(f"{what}: object keys must be strings, got {key!r}.")
                check(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                check(child)

    check(value)
    try:
        text = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise InvalidEventError(f"{what} is not plain JSON ({exc}).") from exc
    if len(text.encode("utf-8")) > max_bytes:
        raise InvalidEventError(f"{what} is larger than the {max_bytes // 1000} KB limit for one record.")
    return json.loads(text)


def list_streams_in(storage: StorageBackend) -> list[dict[str, Any]]:
    """The streams a storage backend holds, from block ids alone - no key needed."""
    counts: dict[str, int] = {}
    for block_id in storage.search(kind="event"):
        if block_id.startswith(_EVENT_PREFIX):
            stream = _stream_of(block_id)
            counts[stream] = max(counts.get(stream, 0), _seq_of(block_id))
    return [{"stream": s, "events": counts[s]} for s in sorted(counts)]


class EventStore:
    def __init__(
        self,
        storage: StorageBackend,
        master_key: bytes,
        *,
        codecs: dict[str, StateCodec] | None = None,
        snapshot_every: int = 50,
        clock: Callable[[], datetime] | None = None,
        max_record_bytes: int = 1_000_000,
    ):
        if len(master_key) != 32:
            raise ValueError("master_key must be 32 bytes")
        if snapshot_every < 1:
            raise ValueError("snapshot_every must be >= 1")
        default = PathOpsCodec()
        self._codecs: dict[str, StateCodec] = {default.name: default, **(codecs or {})}
        self._storage = storage
        self._master = master_key
        self._snapshot_every = snapshot_every
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max = max_record_bytes
        self._lock = threading.RLock()
        # stream -> (codec name, last seq, last ts, current state): only so an
        # append that gives the full new state need not replay the whole log.
        self._tails: OrderedDict[str, tuple[str, int, datetime, dict[str, Any]]] = OrderedDict()

    # -- writing ------------------------------------------------------------------

    def create_stream(self, stream: str, state: dict[str, Any], *, codec: str = "path-ops", note: str | None = None, ts: datetime | None = None) -> StateView:
        self._check_id(stream)
        if codec not in self._codecs:
            raise InvalidEventError(f'Unknown codec "{codec}". Available: {", ".join(sorted(self._codecs))}.')
        state = _canonical(state, "The starting state", self._max)
        if not isinstance(state, dict):
            raise InvalidEventError("The starting state must be a JSON object.")
        with self._lock:
            if self._block_ids(stream, "event"):
                raise StreamExistsError(stream)
            when = self._when(ts, floor=None)
            record = {"ts": _iso(when), "codec": codec, "note": note, "state": state}
            self._put(stream, "base", 0, record)
            self._remember(stream, codec, 0, when, state)
            return StateView(stream, state, 0, when)

    def append(
        self,
        stream: str,
        *,
        state: dict[str, Any] | None = None,
        payload: Any = None,
        note: str | None = None,
        ts: datetime | None = None,
        expected_seq: int | None = None,
    ) -> StreamEvent:
        """Record one change. Give either the full new `state` (the codec
        works out what changed) or a ready-made codec `payload`."""
        self._check_id(stream)
        if (state is None) == (payload is None):
            raise InvalidEventError("Give exactly one of state (the new full state) or payload (a codec event).")
        with self._lock:
            codec_name, last_seq, last_ts, current = self._tail(stream)
            if expected_seq is not None and expected_seq != last_seq:
                raise ConcurrencyError(stream, f"expected event {expected_seq}, but the stream is at event {last_seq}")
            codec = self._codec(stream, codec_name)
            if state is not None:
                new_state = _canonical(state, "The new state", self._max)
                if not isinstance(new_state, dict):
                    raise InvalidEventError("The new state must be a JSON object.")
                payload = codec.diff(current, new_state)
                if not payload:
                    raise InvalidEventError("Nothing changed - the new state is the same as the current one.")
            payload = _canonical(payload, "The event payload", self._max)
            codec.validate(payload)
            new_state = codec.apply(current, payload)
            when = self._when(ts, floor=last_ts)
            seq = last_seq + 1
            self._put(stream, "event", seq, {"ts": _iso(when), "note": note, "payload": payload})
            if seq % self._snapshot_every == 0:
                self._put(stream, "snapshot", seq, {"ts": _iso(when), "state": new_state})
            self._remember(stream, codec_name, seq, when, new_state)
            return StreamEvent(seq=seq, ts=when, kind="event", note=note, payload=payload)

    def delete_stream(self, stream: str) -> bool:
        self._check_id(stream)
        with self._lock:
            ids = self._block_ids(stream, "event") + self._block_ids(stream, "snapshot")
            for block_id in ids:
                self._storage.delete(block_id)
            self._tails.pop(stream, None)
            return bool(ids)

    # -- reading ------------------------------------------------------------------

    def state_at(self, stream: str, *, at: datetime | None = None, seq: int | None = None) -> StateView:
        """The state as of time `at`, or just after event `seq`; with neither,
        the latest. A time before the stream was created is an error, not an
        empty state."""
        self._check_id(stream)
        if at is not None and seq is not None:
            raise InvalidEventError("Give a time or an event number, not both.")
        return self._rebuild(stream, _utc(at) if at is not None else None, seq)

    def history(self, stream: str, *, since: datetime | None = None, until: datetime | None = None, limit: int | None = None) -> list[StreamEvent]:
        """The log, oldest first: the base (kind "base", payload = starting
        state), then each event. `limit` keeps the most recent N."""
        self._check_id(stream)
        ids = self._ordered_event_ids(stream)
        key = derive_stream_key(self._master, stream)
        events: list[StreamEvent] = []
        for seq, block_id in ids:
            record = self._open(key, stream, "base" if seq == 0 else "event", seq, block_id)
            ts = datetime.fromisoformat(record["ts"])
            if (since is not None and ts < _utc(since)) or (until is not None and ts > _utc(until)):
                continue
            if seq == 0:
                events.append(StreamEvent(0, ts, "base", record.get("note"), record["state"]))
            else:
                events.append(StreamEvent(seq, ts, "event", record.get("note"), record["payload"]))
        return events[-limit:] if limit else events

    def list_streams(self) -> list[dict[str, Any]]:
        """Stream ids and how many events each has - read from the block ids
        alone, so nothing is decrypted."""
        return list_streams_in(self._storage)

    def exists(self, stream: str) -> bool:
        self._check_id(stream)
        return bool(self._block_ids(stream, "event"))

    # -- internals ----------------------------------------------------------------

    def _check_id(self, stream: str) -> None:
        if not isinstance(stream, str) or not STREAM_ID_RE.match(stream):
            raise InvalidStreamIdError(str(stream))

    def _codec(self, stream: str, name: str) -> StateCodec:
        codec = self._codecs.get(name)
        if codec is None:
            raise StreamCorruptError(stream, f'it uses the "{name}" codec, which this server does not have')
        return codec

    def _when(self, requested: datetime | None, floor: datetime | None) -> datetime:
        now = _utc(self._clock())
        when = _utc(requested) if requested is not None else now
        if requested is not None and when > now + _MAX_CLOCK_SKEW:
            raise InvalidEventError("A recorded time can't be in the future.")
        if floor is not None and requested is not None and when < floor:
            raise InvalidEventError(f"A new event can't be earlier than the previous one ({_iso(floor)}) - a stream is append-only.")
        return max(when, floor) if floor is not None else when

    def _block_ids(self, stream: str, kind: str) -> list[str]:
        return self._storage.search(stream=stream, kind=kind)

    def _ordered_event_ids(self, stream: str) -> list[tuple[int, str]]:
        ids = sorted((_seq_of(b), b) for b in self._block_ids(stream, "event") if b.startswith(_EVENT_PREFIX))
        if not ids:
            raise StreamNotFoundError(stream)
        for expected, (seq, _) in enumerate(ids):
            if seq != expected:
                raise StreamCorruptError(stream, f"event {expected} is missing")
        return ids

    def _put(self, stream: str, kind: str, seq: int, record: dict[str, Any]) -> None:
        plaintext = json.dumps(record, allow_nan=False, separators=(",", ":")).encode("utf-8")
        block_kind = "event" if kind == "base" else kind
        block_id = _snapshot_id(stream, seq) if kind == "snapshot" else _event_id(stream, seq)
        if self._storage.exists(block_id):
            self._tails.pop(stream, None)
            raise ConcurrencyError(stream, f"record {seq} already exists")
        key = derive_stream_key(self._master, stream)
        self._storage.put(block_id, seal(key, stream, kind, seq, plaintext), metadata={"stream": stream, "kind": block_kind})

    def _open(self, key: bytes, stream: str, kind: str, seq: int, block_id: str) -> dict[str, Any]:
        try:
            blob = self._storage.get(block_id)
        except KeyError:
            raise StreamCorruptError(stream, f"{kind} record {seq} is missing") from None
        try:
            return json.loads(open_sealed(key, stream, kind, seq, blob))
        except json.JSONDecodeError:
            raise StreamCorruptError(stream, f"{kind} record {seq} is not readable") from None

    def _remember(self, stream: str, codec: str, seq: int, ts: datetime, state: dict[str, Any]) -> None:
        self._tails[stream] = (codec, seq, ts, state)
        self._tails.move_to_end(stream)
        while len(self._tails) > _CACHE_SIZE:
            self._tails.popitem(last=False)

    def _tail(self, stream: str) -> tuple[str, int, datetime, dict[str, Any]]:
        cached = self._tails.get(stream)
        if cached is not None:
            return cached
        view = self._rebuild(stream, None, None)
        base = self._open(derive_stream_key(self._master, stream), stream, "base", 0, _event_id(stream, 0))
        self._remember(stream, base["codec"], view.seq, view.ts, view.state)
        return self._tails[stream]

    def _rebuild(self, stream: str, at: datetime | None, seq: int | None) -> StateView:
        ids = self._ordered_event_ids(stream)
        last = ids[-1][0]
        if seq is not None and not 0 <= seq <= last:
            raise InvalidEventError(f'Stream "{stream}" has events 0 to {last}; there is no event {seq}.')
        target = last if seq is None else seq
        key = derive_stream_key(self._master, stream)

        base = self._open(key, stream, "base", 0, ids[0][1])
        base_ts = datetime.fromisoformat(base["ts"])
        if at is not None and at < base_ts:
            raise StateNotYetError(stream, _iso(base_ts))
        codec = self._codec(stream, base["codec"])

        state, applied, applied_ts = base["state"], 0, base_ts
        snapshots = sorted(((_seq_of(b), b) for b in self._block_ids(stream, "snapshot") if b.startswith(_SNAPSHOT_PREFIX)), reverse=True)
        for snap_seq, snap_id in snapshots:
            if snap_seq > target:
                continue
            snap = self._open(key, stream, "snapshot", snap_seq, snap_id)
            snap_ts = datetime.fromisoformat(snap["ts"])
            if at is None or snap_ts <= at:
                state, applied, applied_ts = snap["state"], snap_seq, snap_ts
                break

        for n in range(applied + 1, target + 1):
            event = self._open(key, stream, "event", n, ids[n][1])
            ts = datetime.fromisoformat(event["ts"])
            if at is not None and ts > at:
                break
            state = codec.apply(state, event["payload"])
            applied, applied_ts = n, ts
        return StateView(stream, state, applied, applied_ts)
