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

# The token callers present. MODELDB_TOKEN is accepted too, the name AgentBuilder's services use.
if not os.environ.get("MDC_API_TOKENS") and os.environ.get("MODELDB_TOKEN"):
    os.environ["MDC_API_TOKENS"] = os.environ["MODELDB_TOKEN"]
if not os.environ.get("MDC_API_TOKENS"):
    raise SystemExit(
        "ModelDB needs an API token and none is set, so it will not start.\n"
        "  On this service add the variable MDC_API_TOKENS = a long random value\n"
        "  (for example the output of: openssl rand -base64 32).\n"
        "  Services that use ModelDB present that token; on Railway give the Studio and Runtime\n"
        "  MODELDB_TOKEN = ${{ModelDB.MDC_API_TOKENS}} and\n"
        "  MODELDB_URL = http://${{ModelDB.RAILWAY_PRIVATE_DOMAIN}}:8000"
    )
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
