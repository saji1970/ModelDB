"""TurnResult: the shared conversational-turn result shape, used by
both `conversation.interpreter` (object-storage commands) and
`conversation.db_interpreter` (database/table administration). Split
into its own module so those two can both produce it without either
importing the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TurnResult:
    message: str
    data: Any = None
