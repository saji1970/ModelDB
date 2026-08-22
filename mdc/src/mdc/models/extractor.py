"""Safetensors extraction and tensor block-splitting (CLAUDE-STORAGE.md
sections 12, 14).

`parse_safetensors` is a real parser (header length -> JSON header ->
per-tensor byte slices by the header's own `data_offsets`), not a
sniff - `classification.detector.detect_safetensors` only *validates*
the header shape; this module actually walks it to pull out every
tensor's real bytes.

`split_tensor_into_blocks` splits along whole rows of the tensor's
outer dimension when the dtype/shape allow it, so each block is a real,
independently-shaped sub-tensor - not an arbitrary byte range with a
shape nobody can vouch for.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass

from mdc.models.tensor import DTYPE_BYTE_WIDTH


class InvalidSafetensorsError(ValueError):
    pass


@dataclass(frozen=True)
class ExtractedTensor:
    name: str
    dtype: str
    shape: list[int]
    data: bytes


@dataclass(frozen=True)
class ParsedSafetensors:
    tensors: list[ExtractedTensor]
    metadata: dict[str, str]


def parse_safetensors(content: bytes) -> ParsedSafetensors:
    if len(content) < 8:
        raise InvalidSafetensorsError("too short to contain a safetensors header")
    header_len = struct.unpack("<Q", content[:8])[0]
    if header_len <= 0 or 8 + header_len > len(content):
        raise InvalidSafetensorsError("invalid header length")
    try:
        header = json.loads(content[8 : 8 + header_len])
    except json.JSONDecodeError as exc:
        raise InvalidSafetensorsError(f"header is not valid JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise InvalidSafetensorsError("header is not a JSON object")

    metadata = header.pop("__metadata__", {}) or {}
    body_start = 8 + header_len

    tensors: list[ExtractedTensor] = []
    for name, info in header.items():
        try:
            dtype = info["dtype"]
            shape = list(info["shape"])
            start, end = info["data_offsets"]
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidSafetensorsError(f"malformed tensor entry {name!r}: {exc}") from exc
        tensors.append(ExtractedTensor(name=name, dtype=dtype, shape=shape, data=content[body_start + start : body_start + end]))

    return ParsedSafetensors(tensors=tensors, metadata=metadata)


@dataclass(frozen=True)
class BlockPlan:
    offset: int
    shape: list[int] | None
    data: bytes


def split_tensor_into_blocks(tensor: ExtractedTensor, chunk_size_bytes: int) -> list[BlockPlan]:
    dtype_width = DTYPE_BYTE_WIDTH.get(tensor.dtype)
    if not dtype_width or not tensor.shape:
        return [BlockPlan(offset=0, shape=list(tensor.shape) or None, data=tensor.data)]

    row_size = dtype_width
    for dim in tensor.shape[1:]:
        row_size *= dim
    if row_size <= 0:
        return [BlockPlan(offset=0, shape=list(tensor.shape), data=tensor.data)]

    rows_per_block = max(1, chunk_size_bytes // row_size)
    total_rows = tensor.shape[0]

    blocks: list[BlockPlan] = []
    offset = 0
    row = 0
    while row < total_rows:
        rows_here = min(rows_per_block, total_rows - row)
        length = rows_here * row_size
        blocks.append(BlockPlan(offset=offset, shape=[rows_here, *tensor.shape[1:]], data=tensor.data[offset : offset + length]))
        offset += length
        row += rows_here
    return blocks or [BlockPlan(offset=0, shape=list(tensor.shape), data=tensor.data)]
