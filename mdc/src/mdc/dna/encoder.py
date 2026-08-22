"""Binary <-> DNA base encoding (CLAUDE-STORAGE.md section 25/34).

    00 -> A
    01 -> C
    10 -> G
    11 -> T

A prototype representation only - no physical synthesis or
sequencing. Every byte maps to exactly 4 bases (8 bits / 2 bits per
base), so there's no padding ambiguity to resolve on decode.
"""

from __future__ import annotations

_BASE_BY_BITS = {"00": "A", "01": "C", "10": "G", "11": "T"}
_BITS_BY_BASE = {base: bits for bits, base in _BASE_BY_BITS.items()}


class DNADecodeError(ValueError):
    """The sequence doesn't correspond to a valid encoding - either its
    length isn't a multiple of 4 bases (an insertion/deletion shifted
    it) or it contains a symbol outside A/C/G/T."""


def encode(data: bytes) -> str:
    bits = "".join(f"{byte:08b}" for byte in data)
    return "".join(_BASE_BY_BITS[bits[i : i + 2]] for i in range(0, len(bits), 2))


def decode(sequence: str) -> bytes:
    if len(sequence) % 4 != 0:
        raise DNADecodeError(f"sequence length {len(sequence)} is not a multiple of 4 bases")
    try:
        bits = "".join(_BITS_BY_BASE[base] for base in sequence)
    except KeyError as exc:
        raise DNADecodeError(f"sequence contains a non-ACGT symbol: {exc}") from exc
    return bytes(int(bits[i : i + 8], 2) for i in range(0, len(bits), 8))
