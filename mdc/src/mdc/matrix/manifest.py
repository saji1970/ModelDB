"""MatrixManifest - tracks where each MatrixBlock landed (a matrix's
counterpart to Phase D's ModelManifest). Only ranges/checksums are
kept here; the actual payload lives wherever the router put it, same
as everything else in this system.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MatrixBlockRef(BaseModel):
    object_id: str
    row_start: int
    row_end: int
    column_start: int
    column_end: int
    checksum: str


class MatrixManifest(BaseModel):
    matrix_id: str
    rows: int
    columns: int
    dtype: str
    block_count: int
    checksum: str
    blocks: list[MatrixBlockRef] = Field(default_factory=list)
