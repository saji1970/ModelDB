"""The HTTP-facing chat panel's entry point (CLAUDE-STORAGE.md sections
36-38).

A thin, per-session wrapper over two turn-processing pipelines, mirroring
exactly what `cli.shell.handle_query` already does for the terminal:
database/table administration and the polymorphic object-storage NLP
(`conversation.interpreter.process_turn`) are tried first; anything that
doesn't match falls through to the merchants CRUD/analytics pipeline
(`cql.interpreter.process_turn`), which never itself returns "not
recognized" - the mock LLM's classifier always resolves *something* (see
`llm.mock_provider`), even if that's just "I need more information."
Both pipelines share the same underlying engine/store as everything
else in this process (CLAUDE.md section 28: one Data Engine, no matter
the caller) - the merchants fallback below reuses
`manager.get(DEFAULT_DATABASE).engine` rather than building a second,
separate `MDCDataEngine` instance, so `insert into merchants ...` (the
database-admin path) and "Create a merchant called ..." (this path)
read and write the exact same rows.

`read_file` is never wired into the storage pipeline here - see
`conversation.interpreter`'s module docstring for why letting a remote
caller trigger a local filesystem read would be unsafe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mdc.conversation.db_interpreter import apply_pending_schema
from mdc.conversation.interpreter import process_turn
from mdc.conversation.state import StorageConversationState
from mdc.cql.dialogue import format_question, format_unresolved
from mdc.cql.interpreter import TurnResult as CqlTurnResult
from mdc.cql.interpreter import process_turn as process_merchants_turn
from mdc.databases.manager import DEFAULT_DATABASE, DatabaseManager
from mdc.engine.base import OperationResult
from mdc.llm.mock_provider import MockLLMProvider
from mdc.mce.context import QueryContext
from mdc.mce.state import ConversationState as MerchantsConversationState
from mdc.ontology.loader import load_ontology
from mdc.security.roles import Role

_HELP_TEXT = (
    'Try: "list models", "list images", "show objects in archive", '
    '"show <object_id>", "read <object_id>", "explain <object_id>", '
    '"archive <object_id>", "move <object_id> to hot", '
    '"delete <object_id>", "search for <text>", "create database <name>", '
    '"use database <name>", "list databases", "create table <name> with '
    '<field> <type>, ...", "show data in <table>", "show data in <table> '
    'in database <name>" (works without switching), "insert into '
    '<table> <field>=<value>, ...", "find <text> [under/over <amount>]" '
    '(searches every database\'s tables and documents at once), '
    '"export database as csv|sql|excel|markdown", "export schema as er diagram", '
    '"export database with data" (an MDC export file), attach a file with the paperclip to '
    'import a .md/.csv/.sql/MDC export, or a merchants query/CRUD sentence, e.g. "Show all merchants", "Create a '
    'merchant called ABC Store in India".'
)


# Words that drop whatever question the chat is waiting on. Without these
# the browser chat had no way out of a pending clarification (the CLI has
# `/reset`; HTTP had nothing): every prompt the storage pipeline did not
# recognize was read as an answer to "1. Ledger 2. Available 3. Settlement"
# and got the same "I didn't understand that answer" back.
_CANCEL_WORDS = {"cancel", "never mind", "nevermind", "start over", "forget it", "stop"}
_YES_WORDS = {"yes", "y", "yep", "yeah", "ok", "okay", "confirm", "create", "create it", "do it", "go ahead"}
_NO_WORDS = {"no", "n", "nope", "don't", "dont"}
_STUCK_HINT = '\n(Say "cancel" to drop this question and ask something else.)'


@dataclass(frozen=True)
class ChatReply:
    message: str
    data: Any = None
    download: dict[str, str] | None = None


def _format_fields(fields: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in fields.items())


def _format_operation_result(result: OperationResult) -> ChatReply:
    if result.kind == "CREATE":
        record = result.records[0]
        return ChatReply(f"Created {record.record_id} ({_format_fields(record.fields)})")
    if result.kind == "UPDATE":
        record = result.records[0]
        return ChatReply(f"Updated {record.record_id} ({_format_fields(record.fields)})")
    if result.kind == "DELETE":
        return ChatReply(f"Deleted {result.count} record(s).")

    # READ
    if result.count == 0:
        return ChatReply("No matching records.")
    rows = [{"record_id": record.record_id, **record.fields} for record in result.records]
    return ChatReply(f"{result.count} row(s).", data=rows)


def _format_context_summary(ctx: QueryContext) -> ChatReply:
    if ctx.clarification_required:
        return ChatReply(
            "I need more information before I can answer that. "
            "Which entity are you asking about? (e.g. merchants, customers, transactions)"
        )
    lines = [f"Intent: {ctx.intent}    Entity: {ctx.entity}"]
    if ctx.fields:
        lines.append(f"Fields: {', '.join(ctx.fields)}")
    for condition in ctx.conditions:
        lines.append(f"WHERE {condition.field} {condition.operator} {condition.value}")
    for order in ctx.order_by:
        lines.append(f"ORDER BY {order.field} {order.direction}")
    if ctx.limit is not None:
        lines.append(f"LIMIT {ctx.limit}")
    lines.append("(Analytics reads are not yet compiled to SQL against the synthetic payments dataset.)")
    return ChatReply("\n".join(lines))


def _format_merchants_reply(result: CqlTurnResult) -> ChatReply:
    if result.error is not None:
        return ChatReply(result.error)
    if result.confirmation_prompt is not None:
        return ChatReply(result.confirmation_prompt)
    if result.unresolved_answer and result.question is not None:
        return ChatReply("\n".join(format_unresolved(result.question)) + _STUCK_HINT)
    if result.question is not None:
        return ChatReply("\n".join(format_question(result.question)) + _STUCK_HINT)
    if result.operation_result is not None:
        return _format_operation_result(result.operation_result)
    return _format_context_summary(result.context)


@dataclass
class ChatEngine:
    manager: DatabaseManager
    _sessions: dict[str, StorageConversationState] = field(default_factory=dict)
    _merchants_sessions: dict[str, MerchantsConversationState] = field(default_factory=dict)
    _ontology: Any = field(default_factory=load_ontology)
    _llm: Any = field(default_factory=MockLLMProvider)

    def session_database(self, session_id: str) -> str:
        """The database this chat session is currently working in."""
        state = self._sessions.get(session_id)
        return state.current_database if state else DEFAULT_DATABASE

    def switch_session_database(self, session_id: str, name: str) -> None:
        # Same bookkeeping as "use database <name>": an object reference from
        # the previous database means nothing in the new one.
        state = self._sessions.setdefault(session_id, StorageConversationState(session_id=session_id))
        state.current_database = name
        state.last_object_id = None
        state.pending_delete = None

    def handle(self, session_id: str, text: str, role: Role | None = None) -> ChatReply:
        if not text.strip():
            return ChatReply(_HELP_TEXT)

        state = self._sessions.setdefault(session_id, StorageConversationState(session_id=session_id))
        merchants_state = self._merchants_sessions.setdefault(session_id, MerchantsConversationState(session_id=session_id))

        state.role = role  # who is asking this turn; None = unrestricted (CLI, tests)

        command = text.strip().lower()
        if command == "/reset":
            # Same as the CLI's /reset: forget context, pending questions and
            # the current database.
            state.reset()
            merchants_state.reset()
            return ChatReply("Context cleared. Ask me anything.")
        if command in _CANCEL_WORDS:
            had_pending = (
                merchants_state.pending is not None
                or merchants_state.confirmation is not None
                or state.pending_delete is not None
                or state.pending_schema is not None
            )
            merchants_state.pending = None
            merchants_state.confirmation = None
            state.pending_delete = None
            state.pending_schema = None
            return ChatReply("Okay, dropped that. What would you like to do?" if had_pending else "Nothing is pending. What would you like to do?")

        if state.pending_schema is not None:
            # A proposed design is waiting. "yes" creates it, "no" drops it, and
            # anything else drops it and is read as a new request - a pending
            # question must never trap the chat (see _CANCEL_WORDS).
            if command in _YES_WORDS:
                created = apply_pending_schema(state, self.manager)
                return ChatReply(created.message, data=created.data)
            state.pending_schema = None
            if command in _NO_WORDS:
                return ChatReply("Okay, nothing was created.")

        result = process_turn(state, text, self.manager, read_file=None)
        if result is not None:
            return ChatReply(result.message, data=result.data, download=result.download)

        engine = self.manager.get(DEFAULT_DATABASE).engine
        merchants_result = process_merchants_turn(merchants_state, text, self._ontology, self._llm, engine)
        return _format_merchants_reply(merchants_result)
