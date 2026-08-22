"""Minimal pure-Python matrix operations - no numpy (the project has
zero third-party dependencies). Just enough to support real block
splitting/reconstruction and a genuine truncated-SVD low-rank
approximation (CLAUDE-STORAGE.md sections 17-18); not a general
linear-algebra library.
"""

from __future__ import annotations

import math

Matrix = list[list[float]]
Vector = list[float]


def zeros(rows: int, cols: int) -> Matrix:
    return [[0.0] * cols for _ in range(rows)]


def transpose(a: Matrix) -> Matrix:
    if not a or not a[0]:
        return []
    return [[a[r][c] for r in range(len(a))] for c in range(len(a[0]))]


def matmul(a: Matrix, b: Matrix) -> Matrix:
    rows_a = len(a)
    cols_a = len(a[0]) if a else 0
    cols_b = len(b[0]) if b else 0
    result = zeros(rows_a, cols_b)
    for i in range(rows_a):
        row_a = a[i]
        row_r = result[i]
        for k in range(cols_a):
            aik = row_a[k]
            if aik == 0.0:
                continue
            row_b = b[k]
            for j in range(cols_b):
                row_r[j] += aik * row_b[j]
    return result


def mat_vec(a: Matrix, v: Vector) -> Vector:
    return [dot(row, v) for row in a]


def dot(u: Vector, v: Vector) -> float:
    return sum(x * y for x, y in zip(u, v))


def norm(v: Vector) -> float:
    return math.sqrt(dot(v, v))


def scale(v: Vector, s: float) -> Vector:
    return [x * s for x in v]


def subtract(a: Matrix, b: Matrix) -> Matrix:
    return [[a[i][j] - b[i][j] for j in range(len(a[0]))] for i in range(len(a))]


def outer(u: Vector, v: Vector) -> Matrix:
    return [[ui * vj for vj in v] for ui in u]


def frobenius_norm(a: Matrix) -> float:
    return math.sqrt(sum(x * x for row in a for x in row))
