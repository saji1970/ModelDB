"""Data Engine errors (CLAUDE.md section 18: "if validation fails, DO NOT EXECUTE")."""

from __future__ import annotations


class DataEngineError(Exception):
    """Base class for Data Engine errors."""


class RecordNotFoundError(DataEngineError):
    def __init__(self, collection: str, message: str = "no matching record"):
        super().__init__(f"{collection}: {message}")
        self.collection = collection


class DuplicateRecordError(DataEngineError):
    def __init__(self, collection: str, record_id: str):
        super().__init__(f"{collection}: a record with id {record_id!r} already exists")
        self.collection = collection
        self.record_id = record_id


class InvalidRecordIdError(DataEngineError):
    def __init__(self, record_id: str):
        super().__init__(f"{record_id!r} is not a valid record id (letters, digits, '_', '.', '-'; at most 64 characters)")
        self.record_id = record_id
