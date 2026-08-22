"""LOW_RANK representation (CLAUDE-STORAGE.md section 18).

Research-only, explicit opt-in - `low_rank_approximation()` is never
called automatically by the Storage Intelligence Layer (Phase B never
selects `Representation.MATRIX`'s low-rank variant on its own), and
`reconstruction_error` is always computed from the actual
reconstructed matrix, never assumed to be zero. "W ~= A x B" is a real
truncated SVD via power iteration (deflating the residual after each
singular triplet) - no numpy is available, so this is plain Python,
not a stub.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from mdc.matrix.linalg import Matrix, Vector, frobenius_norm, mat_vec, matmul, norm, outer, scale, subtract, transpose, zeros

_POWER_ITERATIONS = 100
_MIN_SINGULAR_VALUE = 1e-12


@dataclass(frozen=True)
class LowRankApproximation:
    rank: int
    a: Matrix  # rows x rank
    b: Matrix  # rank x columns
    reconstruction_error: float  # relative Frobenius-norm error - measured, never assumed
    original_size: int  # bytes, as a dense float64 matrix
    compressed_size: int  # bytes, as A and B stored densely


def _dominant_singular_triplet(residual: Matrix, seed: int) -> tuple[float, Vector, Vector] | None:
    rows = len(residual)
    columns = len(residual[0]) if residual else 0
    if rows == 0 or columns == 0:
        return None

    rng = random.Random(seed)
    v = [rng.uniform(-1.0, 1.0) for _ in range(columns)]
    v_norm = norm(v)
    if v_norm < _MIN_SINGULAR_VALUE:
        return None
    v = scale(v, 1.0 / v_norm)

    residual_t = transpose(residual)
    for _ in range(_POWER_ITERATIONS):
        candidate = mat_vec(residual_t, mat_vec(residual, v))
        candidate_norm = norm(candidate)
        if candidate_norm < _MIN_SINGULAR_VALUE:
            return None  # residual has no remaining singular direction
        v = scale(candidate, 1.0 / candidate_norm)

    av = mat_vec(residual, v)
    sigma = norm(av)
    if sigma < _MIN_SINGULAR_VALUE:
        return None
    u = scale(av, 1.0 / sigma)
    return sigma, u, v


def low_rank_approximation(matrix: Matrix, rank: int, seed: int = 42) -> LowRankApproximation:
    if rank < 1:
        raise ValueError("rank must be >= 1")

    rows = len(matrix)
    columns = len(matrix[0]) if matrix else 0

    residual = [row[:] for row in matrix]
    triplets: list[tuple[float, Vector, Vector]] = []
    for k in range(rank):
        triplet = _dominant_singular_triplet(residual, seed=seed + k)
        if triplet is None:
            break  # the matrix's actual rank is lower than requested
        sigma, u, v = triplet
        triplets.append(triplet)
        residual = subtract(residual, outer(scale(u, sigma), v))

    effective_rank = len(triplets)
    a = zeros(rows, effective_rank)
    b = zeros(effective_rank, columns)
    for k, (sigma, u, v) in enumerate(triplets):
        for i in range(rows):
            a[i][k] = u[i] * sigma
        for j in range(columns):
            b[k][j] = v[j]

    reconstructed = matmul(a, b) if effective_rank else zeros(rows, columns)
    original_norm = frobenius_norm(matrix)
    error_norm = frobenius_norm(subtract(matrix, reconstructed))
    relative_error = (error_norm / original_norm) if original_norm > 0 else 0.0

    return LowRankApproximation(
        rank=effective_rank,
        a=a,
        b=b,
        reconstruction_error=relative_error,
        original_size=rows * columns * 8,
        compressed_size=effective_rank * (rows + columns) * 8,
    )


def reconstruct(approximation: LowRankApproximation) -> Matrix:
    return matmul(approximation.a, approximation.b)
