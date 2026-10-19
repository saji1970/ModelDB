"""Event store errors. Messages are written for whoever is calling - they
name the stream and say what to do, and never include sealed content."""

from __future__ import annotations


class EventStoreError(Exception):
    """Base class for event store errors."""


class StreamNotFoundError(EventStoreError):
    def __init__(self, stream: str):
        super().__init__(f'No stream named "{stream}".')
        self.stream = stream


class StreamExistsError(EventStoreError):
    def __init__(self, stream: str):
        super().__init__(f'A stream named "{stream}" already exists - append events to it instead.')
        self.stream = stream


class InvalidStreamIdError(EventStoreError):
    def __init__(self, stream: str):
        super().__init__(f"{stream!r} is not a valid stream id (letters, digits, '_', '.', '-'; starts with a letter or digit; at most 64 characters).")
        self.stream = stream


class InvalidEventError(EventStoreError):
    """The state, payload or timestamp offered can't be recorded."""


class StateNotYetError(EventStoreError):
    def __init__(self, stream: str, created_at: str):
        super().__init__(f'Stream "{stream}" did not exist yet at that time - it was created at {created_at}.')
        self.stream = stream


class ConcurrencyError(EventStoreError):
    def __init__(self, stream: str, detail: str):
        super().__init__(f'Stream "{stream}" changed underneath this write ({detail}). Read it again and retry.')
        self.stream = stream


class StreamCorruptError(EventStoreError):
    """A sealed record failed authentication, an event is missing, or the
    stream uses a codec this server doesn't have. Never silently skipped:
    a state rebuilt from a damaged log would be wrong, and look right."""

    def __init__(self, stream: str, detail: str):
        super().__init__(f'Stream "{stream}" failed its integrity check: {detail}.')
        self.stream = stream


class WrongKeyError(EventStoreError):
    """The server's sealing key is not the one this database's streams were
    sealed with. Said apart from tampering: nothing was altered, the wrong
    key is configured. Never includes either key."""

    def __init__(self):
        super().__init__(
            "The sealing key in use is not the one these streams were sealed with, so they cannot be read. "
            "Configure the original key (MDC_DNA_KEY or MDC_DNA_KEY_FILE); if you rotated keys, use the new one."
        )
