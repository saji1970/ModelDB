"""Conversational CLI shell (CLAUDE.md sections 12, 16-19, 24, 39, 46, 58, 73;
CLAUDE-STORAGE.md sections 36-38, 49-50).

The REPL loop, slash commands, and history are the original scaffold.
Free-text input is tried against the polymorphic-storage NLP layer
first (`conversation.interpreter.process_turn` - STORE/RETRIEVE/
ARCHIVE/RESTORE/OPTIMIZE/MOVE/DELETE/SEARCH/INSPECT/DESCRIBE); when
that returns `None` (not a recognized storage command), it falls
through to `mdc.cql.interpreter.process_turn`, which routes
CREATE/UPDATE/DELETE through the `MDCDataEngine` (never SQL written by
the LLM - section 3) and everything else through the read-oriented MCE
pipeline, including the interactive clarification dialogue (section
24), a destructive-action confirmation prompt (section 58), and
multi-turn conversational state (sections 25-27). Both systems keep
their own conversational state and vocabulary on purpose - see
`nlp.command`'s module docstring for why sharing one classifier across
both domains was rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from mdc.conversation.interpreter import process_turn as process_storage_turn
from mdc.conversation.state import StorageConversationState
from mdc.cql.dialogue import format_question, format_unresolved
from mdc.cql.interpreter import process_turn
from mdc.databases.manager import DatabaseManager
from mdc.engine.base import DataEngine, OperationResult
from mdc.engine.data_engine import MDCDataEngine
from mdc.llm.interface import LLMProvider
from mdc.llm.mock_provider import MockLLMProvider
from mdc.mce.context import QueryContext
from mdc.mce.state import ConversationState
from mdc.ontology.loader import load_ontology
from mdc.ontology.ontology import Ontology
from mdc.schema.loader import load_default_registry
from mdc.storage.duckdb_store import DuckDBStore

BANNER = (
    "Molecular Data Center\n"
    "Conversational Molecular Data Operating System"
)

HELP_TEXT = """\
MDC Conversational Query Language

Query examples:
  Show all merchants
  Show merchants in India
  Show merchants with settlement balance above 10K USD
  Count merchants
  Show top 20 merchants by settlement balance

CRUD examples:
  Create a merchant called ABC Store in India
  Show me ABC Store
  Change ABC Store balance to 15000
  Delete merchant ABC Store

Storage examples:
  store ./model.safetensors
  archive AIM-1234567890
  retrieve tensor layer_0.attention.q from AIM-1234567890
  move AIM-1234567890 to hot
  list images
  search for revenue

Database examples:
  create database mytest
  use database mytest
  list databases
  create table products with sku string, name string, price decimal
  show data in products
  insert into products sku=ABC123, name=Widget, price=9.99

Commands:
  /help
  /context
  /explain
  /mdql
  /sql
  /debug
  /history
  /reset
  /exit\
"""

NOT_YET_IMPLEMENTED = {
    "/explain": "Nothing to explain yet - query explanation lands in a later phase.",
    "/mdql": "No MDQL to show yet - the MDCQL AST lands in a later phase.",
    "/sql": "No compiled SQL yet - reads are not yet compiled to SQL against the analytics dataset.",
    "/debug": "Debug telemetry lands in a later phase.",
}


def _default_engine() -> DataEngine:
    store = DuckDBStore(":memory:")
    store.init_schema()
    return MDCDataEngine(store, load_default_registry())


def _default_manager() -> DatabaseManager:
    import tempfile

    store = DuckDBStore(":memory:")
    store.init_schema()
    return DatabaseManager(Path(tempfile.mkdtemp()) / "databases", store, load_default_registry())


@dataclass
class ShellState:
    history: list[str] = field(default_factory=list)
    running: bool = True
    session_id: str = "default"
    ontology: Ontology = field(default_factory=load_ontology)
    llm: LLMProvider = field(default_factory=MockLLMProvider)
    engine: DataEngine = field(default_factory=_default_engine)
    conversation: ConversationState = field(default_factory=ConversationState)
    manager: DatabaseManager = field(default_factory=_default_manager)
    storage_conversation: StorageConversationState = field(default_factory=StorageConversationState)


def handle_command(state: ShellState, console: Console, line: str) -> None:
    command = line.strip().lower()
    if command == "/exit":
        state.running = False
        return
    if command == "/help":
        console.print(HELP_TEXT)
        return
    if command == "/history":
        if not state.history:
            console.print("No queries yet.")
        for i, entry in enumerate(state.history, start=1):
            console.print(f"{i}. {entry}")
        return
    if command == "/reset":
        state.history.clear()
        state.conversation.reset()
        state.storage_conversation.reset()
        console.print("Context and history cleared.")
        return
    if command == "/context":
        if state.conversation.context is None:
            console.print("No query context yet - ask a question first.")
        else:
            console.print(state.conversation.context.model_dump_json(indent=2, exclude_none=True))
        return
    if command in NOT_YET_IMPLEMENTED:
        console.print(NOT_YET_IMPLEMENTED[command])
        return
    console.print(f"Unknown command: {command}. Type /help for a list of commands.")


def _print_context_summary(console: Console, ctx: QueryContext) -> None:
    if ctx.clarification_required:
        console.print("I need more information before I can answer that.")
        console.print("  Which entity are you asking about? (e.g. merchants, customers, transactions)")
        return

    console.print(f"Intent: {ctx.intent}    Entity: {ctx.entity}")
    if ctx.fields:
        console.print(f"Fields: {', '.join(ctx.fields)}")
    for condition in ctx.conditions:
        console.print(f"  WHERE {condition.field} {condition.operator} {condition.value}")
    for order in ctx.order_by:
        console.print(f"  ORDER BY {order.field} {order.direction}")
    if ctx.limit is not None:
        console.print(f"  LIMIT {ctx.limit}")
    console.print("(Analytics reads are not yet compiled to SQL against the synthetic payments dataset.)")


def _format_fields(fields: dict) -> str:
    return ", ".join(f"{key}={value}" for key, value in fields.items())


def _print_operation_result(console: Console, result: OperationResult) -> None:
    if result.kind == "CREATE":
        record = result.records[0]
        console.print(f"Created {record.record_id} ({_format_fields(record.fields)})")
        return
    if result.kind == "UPDATE":
        record = result.records[0]
        console.print(f"Updated {record.record_id} ({_format_fields(record.fields)})")
        return
    if result.kind == "DELETE":
        console.print(f"Deleted {result.count} record(s).")
        return

    # READ
    if result.count == 0:
        console.print("No matching records.")
        return
    field_names = sorted({key for record in result.records for key in record.fields})
    table = Table()
    table.add_column("record_id")
    for name in field_names:
        table.add_column(name)
    for record in result.records:
        table.add_row(record.record_id, *[str(record.fields.get(name, "")) for name in field_names])
    console.print(table)
    console.print(f"{result.count} row(s)")


def _print_object_rows(console: Console, rows: list) -> None:
    if not rows or not isinstance(rows[0], dict):
        return
    columns = list(rows[0].keys())
    table = Table()
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*[str(row.get(column, "")) for column in columns])
    console.print(table)


def handle_query(state: ShellState, console: Console, line: str) -> None:
    state.history.append(line)

    storage_result = process_storage_turn(state.storage_conversation, line, state.manager, read_file=Path.read_bytes)
    if storage_result is not None:
        console.print(storage_result.message)
        if isinstance(storage_result.data, list):
            _print_object_rows(console, storage_result.data)
        return

    result = process_turn(state.conversation, line, state.ontology, state.llm, state.engine)

    if result.error is not None:
        console.print(result.error)
        return
    if result.confirmation_prompt is not None:
        console.print(result.confirmation_prompt)
        return
    if result.unresolved_answer and result.question is not None:
        for text_line in format_unresolved(result.question):
            console.print(text_line)
        return
    if result.question is not None:
        for text_line in format_question(result.question):
            console.print(text_line)
        return
    if result.operation_result is not None:
        _print_operation_result(console, result.operation_result)
        return

    _print_context_summary(console, result.context)


def process_line(state: ShellState, console: Console, line: str) -> None:
    if not line.strip():
        return
    if line.strip().startswith("/"):
        handle_command(state, console, line)
    else:
        handle_query(state, console, line)


def run_shell(store: DuckDBStore, console: Console | None = None) -> None:
    console = console or Console()
    console.print(BANNER)
    state = ShellState(
        engine=MDCDataEngine(store, load_default_registry()),
        manager=DatabaseManager(store.path.parent / "databases", store, load_default_registry()),
    )
    while state.running:
        prompt = "mdc> " if state.storage_conversation.current_database == "default" else f"mdc[{state.storage_conversation.current_database}]> "
        try:
            line = console.input(prompt)
        except (EOFError, KeyboardInterrupt):
            break
        process_line(state, console, line)
