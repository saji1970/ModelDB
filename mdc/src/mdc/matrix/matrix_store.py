"""MatrixStore (CLAUDE-STORAGE.md section 17).

The concrete backing for `Representation.MATRIX` (reserved since Phase
B, never auto-selected): splits a matrix into blocks, stores each
through the same `StorageRouter` everything else uses, and reassembles
them on retrieval. Per-block integrity is already guaranteed by
`router.retrieve()` (Phase F's checksum-on-decompress) - no separate
verification is duplicated here.
"""

from __future__ import annotations

import hashlib
import struct
from datetime import datetime, timezone

from mdc.classification.data_type import DataType
from mdc.classification.metadata import build_profile
from mdc.matrix.errors import MatrixNotFoundError
from mdc.matrix.linalg import Matrix
from mdc.matrix.manifest import MatrixBlockRef, MatrixManifest
from mdc.matrix.matrix_block import DEFAULT_DTYPE, MatrixBlock, reconstruct_matrix_from_blocks, split_matrix_into_blocks
from mdc.model.object import MDCObject, generate_object_id
from mdc.storage_intelligence.analyzer import AccessProfile
from mdc.storage_intelligence.policy import DEFAULT_POLICY
from mdc.storage_intelligence.router import ObjectNotFoundError, StorageRouter
from mdc.storage_intelligence.strategy import StorageStrategyEngine


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MatrixStore:
    def __init__(self, router: StorageRouter, strategy_engine: StorageStrategyEngine | None = None):
        self.router = router
        self.strategy_engine = strategy_engine or StorageStrategyEngine()

    def store_matrix(self, matrix: Matrix, dtype: str = DEFAULT_DTYPE, access: AccessProfile | None = None) -> MatrixManifest:
        access = access or AccessProfile()
        rows = len(matrix)
        columns = len(matrix[0]) if matrix else 0
        matrix_id = generate_object_id(DataType.TENSOR)

        flat = [value for row in matrix for value in row]
        flat_bytes = struct.pack(f"<{len(flat)}d", *flat)
        profile = build_profile(DataType.TENSOR, flat_bytes, dimensions=[rows, columns])
        strategy = self.strategy_engine.select(profile, access)
        chunk_budget = strategy.chunk_size or DEFAULT_POLICY.chunk_size_bytes

        blocks = split_matrix_into_blocks(matrix, chunk_budget, dtype=dtype, block_id_prefix=matrix_id)
        refs: list[MatrixBlockRef] = []
        for block in blocks:
            self._store_block(matrix_id, block, strategy)
            refs.append(
                MatrixBlockRef(
                    object_id=block.block_id,
                    row_start=block.row_start,
                    row_end=block.row_end,
                    column_start=block.column_start,
                    column_end=block.column_end,
                    checksum=block.checksum,
                )
            )

        manifest = MatrixManifest(
            matrix_id=matrix_id,
            rows=rows,
            columns=columns,
            dtype=dtype,
            block_count=len(blocks),
            checksum=hashlib.sha256(flat_bytes).hexdigest(),
            blocks=refs,
        )
        self._store_manifest(manifest, access)
        return manifest

    def _store_block(self, matrix_id: str, block: MatrixBlock, strategy) -> None:
        block_object = MDCObject(
            object_id=block.block_id,
            object_type=DataType.TENSOR,
            size=len(block.payload),
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        self.router.store(block_object, block.payload, strategy, tensor_id=matrix_id, tensor_name="matrix", block_id=block.block_id)

    def _store_manifest(self, manifest: MatrixManifest, access: AccessProfile) -> None:
        payload = manifest.model_dump_json().encode("utf-8")
        profile = build_profile(DataType.TENSOR, payload)
        strategy = self.strategy_engine.select(profile, access)
        manifest_object = MDCObject(object_id=manifest.matrix_id, object_type=DataType.TENSOR, size=len(payload), created_at=_utcnow(), updated_at=_utcnow())
        self.router.store(manifest_object, payload, strategy)

    def get_manifest(self, matrix_id: str) -> MatrixManifest:
        try:
            payload = self.router.retrieve(matrix_id)
        except ObjectNotFoundError:
            raise MatrixNotFoundError(matrix_id) from None
        return MatrixManifest.model_validate_json(payload)

    def retrieve_matrix(self, matrix_id: str) -> Matrix:
        manifest = self.get_manifest(matrix_id)
        blocks = [
            MatrixBlock(
                block_id=ref.object_id,
                row_start=ref.row_start,
                row_end=ref.row_end,
                column_start=ref.column_start,
                column_end=ref.column_end,
                dtype=manifest.dtype,
                payload=self.router.retrieve(ref.object_id),
                checksum=ref.checksum,
            )
            for ref in manifest.blocks
        ]
        return reconstruct_matrix_from_blocks(blocks, manifest.rows, manifest.columns)
