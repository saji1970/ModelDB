"""DataProfile (CLAUDE-STORAGE.md section 7).

A profile is descriptive, not prescriptive - it records measured/
estimated characteristics of a blob of data. It never picks a
compression algorithm or a storage tier; that's the Storage Strategy
Engine's job (a later phase), reading a profile rather than computing
one.
"""

from __future__ import annotations

import math
from collections import Counter

from pydantic import BaseModel

from mdc.classification.data_type import DataType

# Types the classifier can already say something concrete about being
# structured/matrix-shaped, even before a Storage Strategy Engine exists
# to act on it.
_STRUCTURED_TYPES = {DataType.DATABASE_RECORD, DataType.TABULAR, DataType.TIME_SERIES, DataType.TENSOR, DataType.AI_MODEL}
_MATRIX_TENSOR_TYPES = {DataType.TENSOR, DataType.AI_MODEL}
# Once written, these are normally treated as immutable blobs rather than
# edited in place - a default, not a guarantee; callers may override it.
_TYPICALLY_IMMUTABLE_TYPES = {DataType.AI_MODEL, DataType.TENSOR, DataType.IMAGE, DataType.VIDEO, DataType.AUDIO, DataType.ARCHIVE}

# Compression is only worth attempting below this normalized-entropy
# threshold - above it the data already looks close to random (already
# compressed, or encrypted), so most compressors will not shrink it.
_COMPRESSION_CANDIDATE_ENTROPY_THRESHOLD = 0.9


class DataProfile(BaseModel):
    data_type: DataType
    size_bytes: int
    structured: bool = False
    dimensions: list[int] | None = None
    entropy_estimate: float | None = None
    compressibility_estimate: float | None = None
    compression_candidate: bool = False
    matrix_candidate: bool = False
    tensor_candidate: bool = False
    random_access_required: bool = False
    archive_candidate: bool = False
    mutable: bool = True


def shannon_entropy(content: bytes) -> float:
    """Byte-frequency Shannon entropy, normalized to [0, 1] (1.0 = 8
    bits/byte, i.e. indistinguishable from random). A real, measured
    quantity - not a guess - computed over the actual bytes given."""
    if not content:
        return 0.0
    counts = Counter(content)
    length = len(content)
    bits = -sum((n / length) * math.log2(n / length) for n in counts.values())
    return bits / 8.0


def build_profile(data_type: DataType, content: bytes, *, dimensions: list[int] | None = None) -> DataProfile:
    entropy = shannon_entropy(content)
    structured = data_type in _STRUCTURED_TYPES
    matrix_tensor_candidate = data_type in _MATRIX_TENSOR_TYPES

    return DataProfile(
        data_type=data_type,
        size_bytes=len(content),
        structured=structured,
        dimensions=dimensions,
        entropy_estimate=round(entropy, 4),
        compressibility_estimate=round(1.0 - entropy, 4),
        compression_candidate=entropy < _COMPRESSION_CANDIDATE_ENTROPY_THRESHOLD,
        matrix_candidate=matrix_tensor_candidate,
        tensor_candidate=matrix_tensor_candidate,
        random_access_required=data_type in (DataType.AI_MODEL, DataType.TENSOR, DataType.VIDEO, DataType.DOCUMENT),
        archive_candidate=False,  # requires access-pattern history - not yet tracked (later phase)
        mutable=data_type not in _TYPICALLY_IMMUTABLE_TYPES,
    )
