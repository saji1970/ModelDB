"""ImageStore (CLAUDE-STORAGE.md section 19).

    IMAGE -> metadata -> optional compression -> chunking -> object storage

Compression is already handled correctly by construction: Phase B's
`select_compression` reads `DataProfile.compression_candidate`, which
comes from real Shannon entropy (Phase A) - an actually-compressed
JPEG/WebP naturally measures high entropy and is left alone, exactly
as section 19 requires ("do NOT recompress blindly"), with no
image-specific logic needed here to make that true.

Large-object chunking (the pipeline's fourth step) is not yet
implemented for images - only `models/` has real multi-block
addressing (Phase D's per-tensor blocks). Extending that scheme to
generic binary objects is future work, not faked here: an image is
currently always stored as a single block regardless of what
`StorageStrategy.chunking` says.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from pydantic import BaseModel

from mdc.classification import detector
from mdc.classification.data_type import DataType
from mdc.classification.metadata import build_profile
from mdc.model.object import MDCObject, generate_object_id
from mdc.objects.errors import ImageNotFoundError
from mdc.objects.image_dimensions import extract_image_dimensions
from mdc.storage_intelligence.analyzer import AccessProfile
from mdc.storage_intelligence.router import ObjectNotFoundError, StorageRouter
from mdc.storage_intelligence.strategy import StorageStrategyEngine

_METADATA_SUFFIX = ":metadata"


class ImageMetadata(BaseModel):
    image_id: str
    format: str
    width: int | None = None
    height: int | None = None
    size_bytes: int
    checksum: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def detect_image_format(content: bytes) -> str | None:
    signature = detector.detect_image(content)
    return signature.detail if signature is not None else None


class ImageStore:
    def __init__(self, router: StorageRouter, strategy_engine: StorageStrategyEngine | None = None):
        self.router = router
        self.strategy_engine = strategy_engine or StorageStrategyEngine()

    def store_image(self, content: bytes, access: AccessProfile | None = None) -> ImageMetadata:
        access = access or AccessProfile()
        image_format = detect_image_format(content)
        dimensions = extract_image_dimensions(content, image_format) if image_format else None

        image_id = generate_object_id(DataType.IMAGE)
        profile = build_profile(DataType.IMAGE, content)
        strategy = self.strategy_engine.select(profile, access)
        original = MDCObject(object_id=image_id, object_type=DataType.IMAGE, size=len(content), created_at=_utcnow(), updated_at=_utcnow())
        self.router.store(original, content, strategy)

        metadata = ImageMetadata(
            image_id=image_id,
            format=image_format or "unknown",
            width=dimensions.width if dimensions else None,
            height=dimensions.height if dimensions else None,
            size_bytes=len(content),
            checksum=hashlib.sha256(content).hexdigest(),
        )
        self._store_metadata(metadata, access)
        return metadata

    def _store_metadata(self, metadata: ImageMetadata, access: AccessProfile) -> None:
        payload = metadata.model_dump_json().encode("utf-8")
        profile = build_profile(DataType.IMAGE, payload)
        strategy = self.strategy_engine.select(profile, access)
        metadata_object = MDCObject(
            object_id=metadata.image_id + _METADATA_SUFFIX,
            object_type=DataType.IMAGE,
            size=len(payload),
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        self.router.store(metadata_object, payload, strategy)

    def retrieve_image(self, image_id: str) -> bytes:
        try:
            return self.router.retrieve(image_id)
        except ObjectNotFoundError:
            raise ImageNotFoundError(image_id) from None

    def get_metadata(self, image_id: str) -> ImageMetadata:
        try:
            payload = self.router.retrieve(image_id + _METADATA_SUFFIX)
        except ObjectNotFoundError:
            raise ImageNotFoundError(image_id) from None
        return ImageMetadata.model_validate_json(payload)
