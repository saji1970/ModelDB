"""Application entry point and Typer CLI wiring (CLAUDE.md section 73)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from mdc.cli.shell import run_shell
from mdc.storage.duckdb_store import DuckDBStore

DEFAULT_DB_PATH = Path("data/mdc.duckdb")

app = typer.Typer(add_completion=False, no_args_is_help=False)
db_app = typer.Typer(help="Database management commands.")
token_app = typer.Typer(help="API bearer-token management (for custom UI/NLU integrations calling the REST API).")
app.add_typer(db_app, name="db")
app.add_typer(token_app, name="token")


@token_app.command("issue")
def token_issue(name: str = typer.Argument(..., help="A label for this token, e.g. the integration's name.")) -> None:
    """Issue a new API bearer token and print it once - it is not
    recoverable afterward, only the store's hash of it."""
    from mdc.security.tokens import TokenStore

    console = Console()
    token = TokenStore().issue(name)
    console.print(f"[bold green]{token}[/bold green]")
    console.print(f'Store this now - it will not be shown again. Use it as: Authorization: Bearer {token}')


@token_app.command("list")
def token_list() -> None:
    """List the names of every issued (unrevoked) token."""
    from mdc.security.tokens import TokenStore

    console = Console()
    names = TokenStore().list_names()
    if not names:
        console.print("No tokens issued yet. Create one with `mdc token issue <name>`.")
        return
    for name in names:
        console.print(f"  {name}")


@token_app.command("revoke")
def token_revoke(name: str = typer.Argument(..., help="The name passed to `mdc token issue` originally.")) -> None:
    """Revoke every token issued under `name`."""
    from mdc.security.tokens import TokenStore

    console = Console()
    removed = TokenStore().revoke(name)
    if removed:
        console.print(f"Revoked {removed} token(s) named {name!r}.")
    else:
        console.print(f"No token named {name!r} found.")


def _open_store(database: Path, scale: str, force: bool, console: Console) -> DuckDBStore:
    if force and database.exists():
        database.unlink()
    is_new = not database.exists()
    store = DuckDBStore(database)
    store.init_schema()
    if is_new or not store.is_seeded():
        console.print(f"Seeding database (scale={scale})...")
        store.seed(scale=scale)
    return store


@db_app.command("init")
def db_init(
    scale: str = typer.Option("full", help="Dataset scale: full (spec minimums) or small (fast, for dev/tests)."),
    force: bool = typer.Option(False, help="Drop and recreate the database from scratch."),
    database: Path = typer.Option(DEFAULT_DB_PATH, help="Path to the DuckDB file."),
) -> None:
    """Create the schema and seed deterministic synthetic payments data."""
    console = Console()
    store = _open_store(database, scale, force, console)
    console.print("Row counts:")
    for table, count in store.table_counts().items():
        console.print(f"  {table}: {count:,}")
    store.close()


@app.command("serve")
def serve(
    database: Path = typer.Option(DEFAULT_DB_PATH, help="Path to the DuckDB file."),
    host: str = typer.Option("127.0.0.1", help="Host to bind."),
    port: int = typer.Option(8000, help="Port to bind."),
) -> None:
    """Start the MDC Storage Explorer: the Universal Object API plus its
    browser UI (Windows-Explorer-style browsing + a chat panel wired to
    the Object API, see CLAUDE.md sections 34-38)."""
    # Imported here, not at module level: fastapi/uvicorn are only needed
    # for this one command, not for the conversational shell or `db init`.
    import uvicorn

    from mdc.api.app import create_app
    from mdc.databases.manager import DatabaseManager
    from mdc.schema.loader import load_default_registry

    console = Console()
    database.parent.mkdir(parents=True, exist_ok=True)
    store = DuckDBStore(database)
    store.init_schema()
    manager = DatabaseManager(database.parent / "databases", store, load_default_registry())
    web_app = create_app(manager)

    console.print(f"MDC Storage Explorer: http://{host}:{port}")
    uvicorn.run(web_app, host=host, port=port)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    database: Path = typer.Option(DEFAULT_DB_PATH, "--database", help="Path to the DuckDB file."),
) -> None:
    """Start the MDC conversational shell when no subcommand is given."""
    if ctx.invoked_subcommand is not None:
        return
    console = Console()
    # Auto-init at "small" scale so interactive startup stays fast; run
    # `mdc db init` explicitly for the full spec-minimum dataset (section 13).
    store = _open_store(database, scale="small", force=False, console=console)
    try:
        run_shell(store, console)
    finally:
        store.close()


def main_cli() -> None:
    app()


if __name__ == "__main__":
    main_cli()
