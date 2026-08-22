"""Data Engine errors (CLAUDE.md section 18: "if validation fails, DO NOT EXECUTE")."""

from __future__ import annotations


class DataEngineError(Exception):
    """Base class for Data Engine errors."""


class RecordNotFoundError(DataEngineError):
    def __init__(self, collection: str, message: str = "no matching record"):
        super().__init__(f"{collection}: {message}")
        self.collection = collection
