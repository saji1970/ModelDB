"""Conversational state (CLAUDE.md sections 19-21, 24-27, 58).

Three kinds of continuity across turns:

- Pending clarification (section 19, 24): the previous turn asked
  "which balance did you mean?" - this turn's input is the answer to
  that question, not a new query, until it resolves. When the
  question arose from an UPDATE ("Change ABC Store balance to
  15000"), `PendingClarification.crud` carries what was already
  parsed out of the original sentence (the name, the new value) so
  the answer can complete an `UpdateOperation` instead of a read.
- Pending confirmation (section 58): a destructive DELETE is not
  executed until the user confirms it.
- Context modification (sections 25-27): a follow-up utterance with no
  entity of its own ("Only India", "Sort highest first") layers onto
  the previous turn's context instead of being interpreted - and
  failing to resolve - on its own.

`mdc.cql.interpreter.process_turn` is what actually drives these
transitions; this module just holds the state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from mdc.mce.ambiguity import ClarificationQuestion
from mdc.mce.context import QueryContext
from mdc.model.operation import DeleteOperation, Filter


@dataclass(frozen=True)
class PendingCrudContinuation:
    kind: Literal["UPDATE"]
    collection: str
    name_filter: Filter
    value: Any


@dataclass(frozen=True)
class PendingClarification:
    question: ClarificationQuestion
    original_text: str
    base_context: QueryContext
    crud: PendingCrudContinuation | None = None


@dataclass(frozen=True)
class PendingConfirmation:
    operation: DeleteOperation
    description: str


@dataclass
class ConversationState:
    session_id: str = "default"
    context: QueryContext | None = None
    pending: PendingClarification | None = None
    confirmation: PendingConfirmation | None = None

    def reset(self) -> None:
        self.context = None
        self.pending = None
        self.confirmation = None
