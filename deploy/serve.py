"""Starts the MDC server in a container (Railway, Docker): the same app `python -m mdc serve` runs, without
its interactive first-run prompt, listening on every address (Railway's private network is
IPv6) and with a /healthz route for Railway's health check. AgentBuilder authenticates with the
token in MDC_API_TOKENS."""
import os
import socket
from pathlib import Path

import uvicorn

from mdc.api.app import create_app
from mdc.databases.manager import DatabaseManager
from mdc.schema.loader import load_default_registry
from mdc.storage.duckdb_store import DuckDBStore

if not os.environ.get("MDC_API_TOKENS"):
    raise SystemExit("Set MDC_API_TOKENS to the token AgentBuilder uses (MODELDB_TOKEN on the Studio and Runtime)")
database = Path(os.environ.get("MODELDB_DATABASE", "/data/mdc.duckdb"))
database.parent.mkdir(parents=True, exist_ok=True)
store = DuckDBStore(database)
store.init_schema()
app = create_app(DatabaseManager(database.parent / "databases", store, load_default_registry()))


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


def _ipv6() -> bool:
    """"::" takes IPv6 and IPv4 where the host has IPv6; some hosts have only IPv4."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::", 0))
        return True
    except OSError:
        return False


uvicorn.run(app, host=os.environ.get("HOST") or ("::" if _ipv6() else "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))
