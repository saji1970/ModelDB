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
user_app = typer.Typer(help="User accounts and roles (CLAUDE.md section 49: AuthProvider).")
app.add_typer(db_app, name="db")
app.add_typer(token_app, name="token")
app.add_typer(user_app, name="user")

_ROLE_HELP = "One of: viewer (read-only), editor (+ create tables/write rows), db_admin (+ create databases), admin (+ manage users)."


@token_app.command("issue")
def token_issue(
    name: str = typer.Argument(..., help="A label for this token, e.g. the integration's name."),
    role: str = typer.Option("admin", help=_ROLE_HELP),
) -> None:
    """Issue a new API bearer token and print it once - it is not
    recoverable afterward, only the store's hash of it."""
    from mdc.security.roles import Role
    from mdc.security.tokens import TokenStore

    console = Console()
    try:
        parsed_role = Role(role)
    except ValueError:
        console.print(f"[bold red]Unknown role {role!r}.[/bold red] {_ROLE_HELP}")
        raise typer.Exit(code=1)
    token = TokenStore().issue(name, role=parsed_role)
    console.print(f"[bold green]{token}[/bold green]")
    console.print(f'Store this now - it will not be shown again. Use it as: Authorization: Bearer {token}')
    console.print(f"Role: {parsed_role.value}")


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


def _parse_role(role: str, console: Console):
    from mdc.security.roles import Role

    try:
        return Role(role)
    except ValueError:
        console.print(f"[bold red]Unknown role {role!r}.[/bold red] {_ROLE_HELP}")
        raise typer.Exit(code=1)


@user_app.command("create")
def user_create(
    username: str = typer.Argument(...),
    role: str = typer.Option("viewer", help=_ROLE_HELP),
) -> None:
    """Create a user account, prompting for its password (never taken
    as a plain command-line argument, so it never lands in shell
    history or a process list)."""
    from mdc.security.users import UserAlreadyExistsError, UserStore

    console = Console()
    parsed_role = _parse_role(role, console)
    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        UserStore().create(username, password, parsed_role)
    except UserAlreadyExistsError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=1)
    console.print(f"Created user [bold]{username}[/bold] with role [bold]{parsed_role.value}[/bold].")


@user_app.command("list")
def user_list() -> None:
    """List every user account and its role."""
    from mdc.security.users import UserStore

    console = Console()
    users = UserStore().list_users()
    if not users:
        console.print("No user accounts yet. Create one with `mdc user create <username>`.")
        return
    for user in users:
        console.print(f"  {user.username}  ({user.role.value})")


@user_app.command("set-role")
def user_set_role(username: str = typer.Argument(...), role: str = typer.Argument(..., help=_ROLE_HELP)) -> None:
    """Change an existing user's role."""
    from mdc.security.users import UserNotFoundError, UserStore

    console = Console()
    parsed_role = _parse_role(role, console)
    try:
        UserStore().set_role(username, parsed_role)
    except UserNotFoundError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=1)
    console.print(f"{username} is now [bold]{parsed_role.value}[/bold].")


@user_app.command("delete")
def user_delete(username: str = typer.Argument(...)) -> None:
    """Delete a user account."""
    from mdc.security.users import UserStore

    console = Console()
    if UserStore().delete(username):
        console.print(f"Deleted user {username!r}.")
    else:
        console.print(f"No user named {username!r} found.")


def _ensure_admin_bootstrapped(console: Console) -> None:
    """First-run setup (CLAUDE.md section 49): if no user account exists
    yet, interactively create the first one as ADMIN before continuing.
    Skipped when stdin isn't a TTY (tests, CI, scripted/non-interactive
    invocations) rather than hanging on a prompt nobody can answer -
    `mdc user create <name> --role admin` is the explicit equivalent for
    those contexts."""
    import sys

    from mdc.security.roles import Role
    from mdc.security.users import UserStore

    store = UserStore()
    if not store.is_empty():
        return
    if not sys.stdin.isatty():
        console.print("[yellow]No admin user configured yet, and this isn't an interactive session - skipping setup. Run `mdc user create <name> --role admin` to create one.[/yellow]")
        return

    console.print("[bold]No admin user configured yet - let's set one up.[/bold]")
    username = typer.prompt("Admin username")
    password = typer.prompt("Admin password", hide_input=True, confirmation_prompt=True)
    store.create(username, password, Role.ADMIN)
    console.print(f"Created admin user [bold]{username}[/bold]. Manage accounts later with `mdc user`.")


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
    _ensure_admin_bootstrapped(console)
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
    _ensure_admin_bootstrapped(console)
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
