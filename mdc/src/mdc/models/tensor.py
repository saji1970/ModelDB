"""TensorDescriptor + TensorBlock (CLAUDE-STORAGE.md sections 12, 14)."""

from __future__ import annotations

from pydantic import BaseModel

from mdc.storage_intelligence.strategy_types import CompressionAlgorithm

# Element byte width by safetensors dtype string - real values, used to
# compute genuine parameter counts and row-aligned block boundaries,
# not estimates.
DTYPE_BYTE_WIDTH: dict[str, int] = {
    "F64": 8, "F32": 4, "F16": 2, "BF16": 2,
    "I64": 8, "I32": 4, "I16": 2, "I8": 1, "U8": 1, "BOOL": 1,
}


class TensorDescriptor(BaseModel):
    tensor_id: str
    tensor_name: str
    shape: list[int]
    dtype: str
    quantization: str | None = None
    compression: CompressionAlgorithm
    block_size: int
    checksum: str


class TensorBlock(BaseModel):
    block_id: str
    tensor_id: str
    offset: int
    # A real sub-tensor shape when blocks are split along whole rows of
    # the outer dimension (the common case); None only when the tensor's
    # dtype/shape don't support a row-aligned split (section 14 doesn't
    # require every block to carry a meaningful shape, and fabricating
    # one here would violate section 45's "no unverified claims" rule).
    shape: list[int] | None
    dtype: str
    compressed_size: int
    original_size: int
    checksum: str
