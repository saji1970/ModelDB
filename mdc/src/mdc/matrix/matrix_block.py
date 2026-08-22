"""MatrixBlock (CLAUDE-STORAGE.md section 17): tensor -> matrix blocks.

Splits a 2D matrix into a grid of row/column-range sub-rectangles
(not row-only bands like Phase D's tensor blocks - a matrix block is
genuinely addressed by both `row_start:row_end` and
`column_start:column_end`), each independently storable/retrievable.
"""

from __future__ import annotations

import hashlib
import struct

from pydantic import BaseModel

from mdc.matrix.linalg import Matrix, zeros

DEFAULT_DTYPE = "float64"
_DTYPE_STRUCT_CODE = {"float64": "d", "float32": "f"}
_DTYPE_BYTE_WIDTH = {"float64": 8, "float32": 4}


class MatrixBlock(BaseModel):
    block_id: str
    row_start: int
    row_end: int
    column_start: int
    column_end: int
    dtype: str
    payload: bytes
    checksum: str


def _pack(values: list[float], dtype: str) -> bytes:
    code = _DTYPE_STRUCT_CODE[dtype]
    return struct.pack(f"<{len(values)}{code}", *values)


def _unpack(payload: bytes, dtype: str) -> list[float]:
    code = _DTYPE_STRUCT_CODE[dtype]
    width = _DTYPE_BYTE_WIDTH[dtype]
    count = len(payload) // width
    return list(struct.unpack(f"<{count}{code}", payload))


def split_matrix_into_blocks(matrix: Matrix, max_block_bytes: int, dtype: str = DEFAULT_DTYPE, block_id_prefix: str = "block") -> list[MatrixBlock]:
    rows = len(matrix)
    columns = len(matrix[0]) if matrix else 0
    if rows == 0 or columns == 0:
        return []

    dtype_width = _DTYPE_BYTE_WIDTH[dtype]
    target_elements = max(1, max_block_bytes // dtype_width)
    block_rows = max(1, min(rows, int(target_elements**0.5)))
    block_columns = max(1, min(columns, target_elements // block_rows))

    blocks: list[MatrixBlock] = []
    index = 0
    for row_start in range(0, rows, block_rows):
        row_end = min(rows, row_start + block_rows)
        for column_start in range(0, columns, block_columns):
            column_end = min(columns, column_start + block_columns)
            flat = [matrix[r][c] for r in range(row_start, row_end) for c in range(column_start, column_end)]
            payload = _pack(flat, dtype)
            blocks.append(
                MatrixBlock(
                    block_id=f"{block_id_prefix}_{index:04d}",
                    row_start=row_start,
                    row_end=row_end,
                    column_start=column_start,
                    column_end=column_end,
                    dtype=dtype,
                    payload=payload,
                    checksum=hashlib.sha256(payload).hexdigest(),
                )
            )
            index += 1
    return blocks


def reconstruct_matrix_from_blocks(blocks: list[MatrixBlock], rows: int, columns: int) -> Matrix:
    result = zeros(rows, columns)
    for block in blocks:
        values = _unpack(block.payload, block.dtype)
        width = block.column_end - block.column_start
        for offset, value in enumerate(values):
            r = block.row_start + offset // width
            c = block.column_start + offset % width
            result[r][c] = value
    return result
