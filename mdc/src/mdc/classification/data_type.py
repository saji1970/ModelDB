"""The polymorphic data type vocabulary (CLAUDE-STORAGE.md section 4).

Deliberately flat, not a class hierarchy: `DataType` is a label a
deterministic classifier attaches to a blob of bytes, nothing more.
What that label implies about representation/compression/storage tier
is the Storage Intelligence Layer's decision (a later phase), never
this module's.
"""

from __future__ import annotations

from enum import Enum


class DataType(str, Enum):
    DATABASE_RECORD = "DATABASE_RECORD"
    TABULAR = "TABULAR"
    AI_MODEL = "AI_MODEL"
    TENSOR = "TENSOR"
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    DOCUMENT = "DOCUMENT"
    TEXT = "TEXT"
    LOG = "LOG"
    TIME_SERIES = "TIME_SERIES"
    BINARY = "BINARY"
    ARCHIVE = "ARCHIVE"
    UNKNOWN = "UNKNOWN"
