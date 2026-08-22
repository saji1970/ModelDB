"""The HTTP-facing chat panel's entry point (CLAUDE-STORAGE.md sections
36-38).

A thin, per-session wrapper over `conversation.interpreter.process_turn`
- the real NLP integration (Phase J, plus database/table administration)
lives there, shared with the CLI shell. This module's only remaining
job is (a) keeping one `StorageConversationState` per browser session
and (b) turning an unrecognized command into a help message, since the
HTTP chat endpoint has no merchants-CRUD fallback to hand unrecognized
text to the way the CLI shell does. `read_file` is never wired here -
see `conversation.interpreter`'s module docstring for why letting a
remote caller trigger a local filesystem read would be unsafe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mdc.conversation.interpreter import process_turn
from mdc.conversation.state import StorageConversationState
from mdc.databases.manager import DatabaseManager

_HELP_TEXT = (
    'Try: "list models", "list images", "show objects in archive", '
    '"show <object_id>", "read <object_id>", "explain <object_id>", '
    '"archive <object_id>", "move <object_id> to hot", '
    '"delete <object_id>", "search for <text>", "create database <name>", '
    '"use database <name>", "list databases", "create table <name> with '
    '<field> <type>, ...", "show data in <table>", or "insert into '
    '<table> <field>=<value>, ...".'
)


@dataclass(frozen=True)
class ChatReply:
    message: str
    data: Any = None


@dataclass
class ChatEngine:
    manager: DatabaseManager
    _sessions: dict[str, StorageConversationState] = field(default_factory=dict)

    def handle(self, session_id: str, text: str) -> ChatReply:
        if not text.strip():
            return ChatReply(_HELP_TEXT)

        state = self._sessions.setdefault(session_id, StorageConversationState(session_id=session_id))
        result = process_turn(state, text, self.manager, read_file=None)
        if result is None:
            return ChatReply(f"I didn't understand that. {_HELP_TEXT}")
        return ChatReply(result.message, data=result.data)
