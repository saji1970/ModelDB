"""Phase 1 acceptance: CLI shell REPL and `mdc db init`."""

from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from mdc.app import app
from mdc.cli.shell import ShellState, process_line

runner = CliRunner()


def test_db_init_command_seeds_small_scale(tmp_path: Path):
    db_path = tmp_path / "mdc.duckdb"
    result = runner.invoke(app, ["db", "init", "--scale", "small", "--database", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "merchant:" in result.output
    assert db_path.exists()


def test_db_init_is_idempotent_without_force(tmp_path: Path):
    db_path = tmp_path / "mdc.duckdb"
    runner.invoke(app, ["db", "init", "--scale", "small", "--database", str(db_path)])
    result = runner.invoke(app, ["db", "init", "--scale", "small", "--database", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "Seeding database" not in result.output


def _run(lines: list[str]) -> tuple[ShellState, str]:
    console = Console(record=True)
    state = ShellState()
    for line in lines:
        if not state.running:
            break
        process_line(state, console, line)
    return state, console.export_text()


def test_help_command_lists_commands():
    _, output = _run(["/help"])
    assert "/help" in output
    assert "/exit" in output


def test_exit_command_stops_shell():
    state, _ = _run(["/exit", "this should not run"])
    assert state.running is False
    assert state.history == []


def test_free_text_is_recorded_in_history():
    state, output = _run(["Show all merchants", "/history"])
    assert state.history == ["Show all merchants"]
    assert "1. Show all merchants" in output


def test_reset_clears_history():
    state, _ = _run(["Show all merchants", "/reset"])
    assert state.history == []


def test_unknown_command_reports_error():
    _, output = _run(["/bogus"])
    assert "Unknown command" in output


# -- Phase J: storage NLP commands routed through the same shell ----------------

def test_storage_command_does_not_reach_the_merchants_pipeline():
    # If this fell through, `state.conversation.context` would be set
    # (mce.resolver runs on every merchants-CRUD turn); a real storage
    # command must never trigger that path at all.
    state, output = _run(["list images"])
    assert "0 IMAGE object(s)" in output
    assert state.conversation.context is None


def test_merchants_command_still_falls_through_when_storage_nlp_does_not_match():
    state, output = _run(["Show all merchants"])
    assert "Intent: FETCH" in output
    assert state.storage_conversation.last_object_id is None


def test_store_by_path_then_archive_it_via_pronoun(tmp_path: Path):
    sample = tmp_path / "notes.md"
    sample.write_text("# Report\n\nRevenue grew this quarter.\n")

    state, output = _run([f"store {sample}", "archive it"])
    assert "Stored" in output
    assert "Archived" in output
    assert state.storage_conversation.last_object_id is not None


def test_mixed_session_storage_and_merchants_commands_do_not_cross_contaminate(tmp_path: Path):
    sample = tmp_path / "notes.md"
    sample.write_text("# Report\n\nBody.\n")

    state, output = _run([
        f"store {sample}",
        "Show all merchants",
        "Create a merchant called ABC Store in India",
        "archive it",  # "it" must still resolve to the stored document, not the merchant
    ])
    assert "Created MER-" in output
    assert "Archived" in output
    assert state.conversation.context is not None  # merchants context was set
    assert state.storage_conversation.last_object_id is not None  # and storage state independently too


def test_reset_also_clears_storage_conversation_state(tmp_path: Path):
    sample = tmp_path / "notes.md"
    sample.write_text("# Report\n\nBody.\n")

    state, _ = _run([f"store {sample}", "/reset"])
    assert state.storage_conversation.last_object_id is None
