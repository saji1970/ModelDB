"""The MDC record envelope (CLAUDE.md section 8).

A `Record` is the storage-agnostic unit the Data Engine deals in:
every backend - DuckDB today, Matrix/DNA later - stores and returns
this same shape, so nothing above the engine needs to know which
backend is underneath. `fields` is the caller's own schema'd payload
(section 9); everything else is envelope metadata the engine owns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Record(BaseModel):
    record_id: str
    collection: str
    version: int = 1
    created_at: datetime
    updated_at: datetime
    fields: dict[str, Any] = Field(default_factory=dict)
    checksum: str | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
