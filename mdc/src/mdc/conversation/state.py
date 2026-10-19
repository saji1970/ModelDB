"""Conversational state for the polymorphic storage NLP layer
(CLAUDE-STORAGE.md sections 25-27's style of state, applied to objects
instead of merchant records).

`last_object_id` is what "it"/"this model"/"the current object" (the
pronoun vocabulary in `nlp.command.PRONOUNS`) resolves to - the object
most recently stored, inspected, or otherwise referenced in this
session. `pending_delete` mirrors the merchants CRUD confirmation
pattern (`mce.state.PendingConfirmation`): a destructive DELETE is
never executed on the turn it's requested. `current_database` is which
named database (`databases.manager.DatabaseManager`) this session's
object-storage and table commands operate against - "default" until a
`create database`/`use database` command changes it; merchants-CRUD
natural language (`cql`/`mce`) is intentionally unaffected by this and
always stays on the original default database (see
`conversation.interpreter`'s module docstring).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mdc.databases.manager import DEFAULT_DATABASE

if TYPE_CHECKING:
    from mdc.portability.plan import ImportPlan
    from mdc.security.roles import Role


@dataclass
class PendingSchema:
    """A proposed database design (from a chat prompt or an ER diagram)
    waiting for the user's "yes" - nothing is created before that."""

    plan: "ImportPlan"
    database: str


@dataclass
class PendingDrop:
    """A permanent deletion waiting for confirmation - nothing is deleted on
    the turn it is asked for. Confirming takes "yes", except for a whole
    database, where the user has to type its name."""

    kind: str  # "database" | "table" | "stream" | "row"
    database: str
    name: str  # the database, table or stream; for a row, its table
    record_id: str | None = None


@dataclass
class StorageConversationState:
    session_id: str = "default"
    last_object_id: str | None = None
    pending_delete: str | None = None
    current_database: str = DEFAULT_DATABASE
    pending_schema: PendingSchema | None = None
    pending_drop: PendingDrop | None = None
    # The role of whoever is sending this turn - set by the HTTP chat from the
    # caller's token. None (the CLI, tests) means no restriction.
    role: "Role | None" = None

    def reset(self) -> None:
        self.last_object_id = None
        self.pending_delete = None
        self.pending_schema = None
        self.pending_drop = None
        self.current_database = DEFAULT_DATABASE
