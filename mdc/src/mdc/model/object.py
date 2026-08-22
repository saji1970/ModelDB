"""MDCObject (CLAUDE-STORAGE.md section 33).

The polymorphic sibling of `Record`: `Record` is the envelope for a
schema'd row inside a known `SchemaRegistry` collection (what the
`merchants` CRUD flow uses); `MDCObject` is the envelope for anything
else the classifier can identify - a model, an image, a document -
whose internal shape is described by `DataProfile`, not a
`CollectionSchema`. Specialized per-type metadata (a model's tensor
manifest, an image's dimensions) is intentionally not modeled as
subclasses yet - that arrives with the storage backend that actually
needs it (Phase D/E), rather than being speculatively built now.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from mdc.classification.data_type import DataType


class MDCObject(BaseModel):
    object_id: str
    object_type: DataType
    schema_version: str = "1.0"
    size: int
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    storage_strategy: str | None = None


def generate_object_id(object_type: DataType) -> str:
    prefix = "".join(ch for ch in object_type.value if ch.isalpha())[:3] or "OBJ"
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
