"""DNA sequence corruption simulator (CLAUDE-STORAGE.md section 25/37:
"research into reliability").

A simulation only - per-base/per-sequence probabilities applied with a
caller-supplied seeded `random.Random`, so results are reproducible.
This models the shape of real sequencing error classes
(substitution/insertion/deletion/dropout), not measured error
statistics from any real platform.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

_BASES = "ACGT"


@dataclass(frozen=True)
class CorruptionRates:
    substitution_rate: float = 0.0
    insertion_rate: float = 0.0
    deletion_rate: float = 0.0
    dropout_rate: float = 0.0  # probability the whole sequence/read is lost

    def __post_init__(self) -> None:
        for name in ("substitution_rate", "insertion_rate", "deletion_rate", "dropout_rate"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0.0 and 1.0, got {value}")


def corrupt_sequence(sequence: str, rates: CorruptionRates, rng: random.Random) -> str | None:
    """Returns None when the whole sequence is dropped (dropout)."""
    if rng.random() < rates.dropout_rate:
        return None
    result: list[str] = []
    for base in sequence:
        if rng.random() < rates.deletion_rate:
            continue
        if rng.random() < rates.substitution_rate:
            base = rng.choice([b for b in _BASES if b != base])
        result.append(base)
        if rng.random() < rates.insertion_rate:
            result.append(rng.choice(_BASES))
    return "".join(result)
