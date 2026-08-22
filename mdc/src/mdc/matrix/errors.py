"""Matrix storage errors."""

from __future__ import annotations


class MatrixNotFoundError(Exception):
    def __init__(self, matrix_id: str):
        super().__init__(f"No matrix found with matrix_id={matrix_id!r}")
        self.matrix_id = matrix_id
