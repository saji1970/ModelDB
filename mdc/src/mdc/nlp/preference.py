"""OptimizationPreference (CLAUDE-STORAGE.md section 38).

    "store this as compactly as possible"
         -> NLP produces: optimization_preference = MAX_COMPRESSION
         -> the Storage Intelligence Engine makes the actual decision

`extract_preference` only ever produces a *label describing what was
asked for* - it is never used to set `representation`/`compression`
directly anywhere in this codebase. `conversation.interpreter` turns a
preference into an `AccessProfile` hint (a legitimate, already-existing
decision input to `StorageStrategyEngine`) and separately reports what
was requested versus what was actually decided, so the "propose vs.
decide" boundary stays visible to the user rather than silently
collapsing into "NLP picked the compression."
"""

from __future__ import annotations

from enum import Enum

_FAST_HINTS = ("fast", "quick", "frequently", "hot", "speed", "responsive")
_COMPRESSION_HINTS = ("compact", "efficient", "efficiently", "small", "compress", "minimize", "space", "shrink")


class OptimizationPreference(str, Enum):
    NONE = "NONE"
    MAX_COMPRESSION = "MAX_COMPRESSION"
    FAST_ACCESS = "FAST_ACCESS"


def extract_preference(text: str) -> OptimizationPreference:
    lower = text.lower()
    if any(hint in lower for hint in _FAST_HINTS):
        return OptimizationPreference.FAST_ACCESS
    if any(hint in lower for hint in _COMPRESSION_HINTS):
        return OptimizationPreference.MAX_COMPRESSION
    return OptimizationPreference.NONE
