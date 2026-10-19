"""Event-sourced agent state: a base snapshot plus an append-only log of
timestamped changes, rebuilt to any point in time, every record sealed.

See `store.EventStore`. The pieces:

    codec.py    how a change to the state is written down (the part an AI
                model's own encoding plugs into) and replayed
    sealing.py  AES-256-GCM per stream, so what is stored is unreadable
                without the key - content and timestamps both
    store.py    streams: create, append, state-as-of, history
"""

from mdc.events.codec import PathOpsCodec, StateCodec
from mdc.events.errors import (
    ConcurrencyError,
    EventStoreError,
    InvalidEventError,
    InvalidStreamIdError,
    StateNotYetError,
    StreamCorruptError,
    StreamExistsError,
    StreamNotFoundError,
    WrongKeyError,
)
from mdc.events.store import EventStore, RotationReport, StateView, StreamEvent

__all__ = [
    "ConcurrencyError", "EventStore", "EventStoreError", "InvalidEventError", "InvalidStreamIdError", "PathOpsCodec",
    "StateCodec", "StateNotYetError", "StateView", "StreamCorruptError", "StreamEvent", "StreamExistsError", "StreamNotFoundError",
    "WrongKeyError", "RotationReport",
]
