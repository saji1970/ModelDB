"""Phase G: Matrix storage (CLAUDE-STORAGE.md sections 17-18, required
tests in section 48: test_matrix_blocking, test_matrix_reconstruction).
"""

import random
from pathlib import Path

import pytest

from mdc.matrix.errors import MatrixNotFoundError
from mdc.matrix.linalg import frobenius_norm, matmul, subtract
from mdc.matrix.low_rank import low_rank_approximation, reconstruct
from mdc.matrix.matrix_block import reconstruct_matrix_from_blocks, split_matrix_into_blocks
from mdc.matrix.matrix_store import MatrixStore
from mdc.storage.duckdb_store import DuckDBStore
from mdc.storage_intelligence.policy import StoragePolicy
from mdc.storage_intelligence.router import build_default_router
from mdc.storage_intelligence.strategy import StorageStrategyEngine


def _random_matrix(rows: int, columns: int, seed: int = 1):
    rng = random.Random(seed)
    return [[rng.random() for _ in range(columns)] for _ in range(rows)]


def _rank_k_matrix(rows: int, columns: int, rank: int, seed: int = 1):
    rng = random.Random(seed)
    u_vectors = [[rng.random() for _ in range(rows)] for _ in range(rank)]
    v_vectors = [[rng.random() for _ in range(columns)] for _ in range(rank)]
    matrix = [[0.0] * columns for _ in range(rows)]
    for u, v in zip(u_vectors, v_vectors):
        for i in range(rows):
            for j in range(columns):
                matrix[i][j] += u[i] * v[j]
    return matrix


# -- section 48 required tests: blocking + reconstruction -----------------------

def test_matrix_blocking():
    matrix = _random_matrix(17, 13)
    blocks = split_matrix_into_blocks(matrix, max_block_bytes=200)
    assert len(blocks) > 1
    for block in blocks:
        assert block.row_end > block.row_start
        assert block.column_end > block.column_start
        expected_elements = (block.row_end - block.row_start) * (block.column_end - block.column_start)
        assert len(block.payload) == expected_elements * 8  # float64


def test_matrix_reconstruction():
    matrix = _random_matrix(17, 13)
    blocks = split_matrix_into_blocks(matrix, max_block_bytes=200)
    reconstructed = reconstruct_matrix_from_blocks(blocks, rows=17, columns=13)
    assert reconstructed == matrix


def test_single_block_when_matrix_fits_within_budget():
    matrix = _random_matrix(3, 3)
    blocks = split_matrix_into_blocks(matrix, max_block_bytes=1_000_000)
    assert len(blocks) == 1
    assert (blocks[0].row_start, blocks[0].row_end) == (0, 3)
    assert (blocks[0].column_start, blocks[0].column_end) == (0, 3)


def test_empty_matrix_produces_no_blocks():
    assert split_matrix_into_blocks([], max_block_bytes=1000) == []


def test_block_checksums_are_real_and_independent():
    matrix = _random_matrix(10, 10)
    blocks = split_matrix_into_blocks(matrix, max_block_bytes=100)
    checksums = {b.checksum for b in blocks}
    assert len(checksums) == len(blocks)  # every block's checksum is distinct


# -- low-rank approximation: real SVD, never claiming lossless -------------------

def test_rank1_matrix_reconstructs_to_near_zero_error_at_rank1():
    from mdc.matrix.linalg import outer

    matrix = outer([1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0])
    approx = low_rank_approximation(matrix, rank=1)
    assert approx.rank == 1
    assert approx.reconstruction_error < 1e-9


def test_reconstruction_error_decreases_as_rank_increases():
    matrix = _random_matrix(8, 6, seed=3)
    errors = [low_rank_approximation(matrix, rank=r).reconstruction_error for r in range(1, 7)]
    assert all(errors[i] >= errors[i + 1] - 1e-9 for i in range(len(errors) - 1))
    assert errors[-1] < 1e-9  # full rank (6) reconstructs essentially exactly


def test_reconstruction_error_is_never_silently_claimed_zero_for_a_lossy_approximation():
    matrix = _random_matrix(10, 10, seed=5)  # full rank 10
    approx = low_rank_approximation(matrix, rank=2)
    assert approx.reconstruction_error > 0.01  # real, measured, non-trivial error


def test_reconstruct_matches_the_recorded_error():
    matrix = _rank_k_matrix(6, 5, rank=3, seed=9)
    approx = low_rank_approximation(matrix, rank=3)
    reconstructed = reconstruct(approx)
    measured_error = frobenius_norm(subtract(matrix, reconstructed)) / frobenius_norm(matrix)
    assert measured_error == pytest.approx(approx.reconstruction_error, abs=1e-9)


def test_low_rank_compressed_size_is_smaller_for_small_rank():
    matrix = _random_matrix(50, 50, seed=11)
    approx = low_rank_approximation(matrix, rank=3)
    assert approx.compressed_size < approx.original_size


def test_effective_rank_caps_at_the_matrix_actual_rank():
    matrix = _rank_k_matrix(10, 10, rank=2, seed=13)
    approx = low_rank_approximation(matrix, rank=8)  # ask for more rank than exists
    assert approx.rank <= 2 + 1  # the residual runs out at (or near) the true rank
    assert approx.reconstruction_error < 1e-6


def test_low_rank_requires_rank_at_least_one():
    with pytest.raises(ValueError):
        low_rank_approximation(_random_matrix(3, 3), rank=0)


def test_a_times_b_equals_the_reconstruction():
    matrix = _random_matrix(5, 4, seed=2)
    approx = low_rank_approximation(matrix, rank=2)
    assert matmul(approx.a, approx.b) == reconstruct(approx)


# -- MatrixStore: real storage/retrieval through the router -----------------------

@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    duckdb_store = DuckDBStore(tmp_path / "mdc.duckdb")
    duckdb_store.init_schema()
    return duckdb_store


@pytest.fixture
def matrix_store(store: DuckDBStore) -> MatrixStore:
    router = build_default_router(store)
    policy = StoragePolicy(chunk_threshold_bytes=1, chunk_size_bytes=256)  # force multi-block matrices
    return MatrixStore(router, StorageStrategyEngine(policy=policy))


def test_store_and_retrieve_matrix_round_trips_exactly(matrix_store: MatrixStore):
    matrix = _random_matrix(20, 15, seed=7)
    manifest = matrix_store.store_matrix(matrix)
    assert manifest.block_count > 1  # actually exercised multi-block storage
    assert matrix_store.retrieve_matrix(manifest.matrix_id) == matrix


def test_manifest_round_trips(matrix_store: MatrixStore):
    matrix = _random_matrix(6, 6, seed=1)
    manifest = matrix_store.store_matrix(matrix)
    assert matrix_store.get_manifest(manifest.matrix_id) == manifest


def test_manifest_records_correct_dimensions_and_block_count(matrix_store: MatrixStore):
    matrix = _random_matrix(20, 15, seed=7)
    manifest = matrix_store.store_matrix(matrix)
    assert manifest.rows == 20
    assert manifest.columns == 15
    assert manifest.block_count == len(manifest.blocks)


def test_unknown_matrix_id_raises(matrix_store: MatrixStore):
    with pytest.raises(MatrixNotFoundError):
        matrix_store.get_manifest("nope")
    with pytest.raises(MatrixNotFoundError):
        matrix_store.retrieve_matrix("nope")


def test_small_matrix_fits_in_a_single_block(store: DuckDBStore):
    router = build_default_router(store)
    matrix_store = MatrixStore(router)  # default (large) chunk budget
    matrix = _random_matrix(3, 3, seed=4)
    manifest = matrix_store.store_matrix(matrix)
    assert manifest.block_count == 1
    assert matrix_store.retrieve_matrix(manifest.matrix_id) == matrix
