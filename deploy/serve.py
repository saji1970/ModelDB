"""Starts the MDC server in a container (Railway, Docker): the same app `python -m mdc serve` runs, without
its interactive first-run prompt, listening on every address (Railway's private network is
IPv6) and with a /healthz route for Railway's health check. AgentBuilder authenticates with the
token in MDC_API_TOKENS (made and kept on the volume when that is not set)."""
import os
import secrets
import socket
from pathlib import Path

import uvicorn
from fastapi.responses import HTMLResponse

import mdc.api.app

from mdc.api.app import create_app
from mdc.databases.manager import DatabaseManager
from mdc.schema.loader import load_default_registry
from mdc.storage.duckdb_store import DuckDBStore

# The token callers present: MDC_API_TOKENS, or MODELDB_TOKEN (the name AgentBuilder's services
# use). With neither set, ModelDB still starts: it makes a token once, keeps it on the volume
# (next to the database) and prints it, so a missing variable can't stop the service.
def _token() -> str:
    for name in ("MDC_API_TOKENS", "MODELDB_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            print(f"ModelDB: using the API token from {name}.", flush=True)
            return value
    saved = Path(os.environ.get("MODELDB_TOKEN_FILE", "/data/modeldb-api-token"))
    if saved.exists() and saved.read_text().strip():
        token, made = saved.read_text().strip(), False
    else:
        token, made = "mdb_" + secrets.token_urlsafe(32), True
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(token + "\n")
        saved.chmod(0o600)
    print(
        f"ModelDB: no MDC_API_TOKENS variable is set, so it uses the token {'it just made and saved' if made else 'saved'} in {saved}:\n"
        f"    {token}\n"
        "  Give that value to the services that use ModelDB (on Railway: MODELDB_TOKEN on the Studio\n"
        "  and Runtime). To choose the token yourself instead, set MDC_API_TOKENS on this service.",
        flush=True,
    )
    return token


os.environ["MDC_API_TOKENS"] = _token()
database = Path(os.environ.get("MODELDB_DATABASE", "/data/mdc.duckdb"))
database.parent.mkdir(parents=True, exist_ok=True)
store = DuckDBStore(database)
store.init_schema()
app = create_app(DatabaseManager(database.parent / "databases", store, load_default_registry()))


# The Storage Explorer at "/". MDC's own page has an admin token written into it, which is right
# for `mdc serve` on your own computer but would hand the whole database to anyone who opens a
# public domain. Here the page asks for the API token instead and keeps it for the browser tab.
_SIGN_IN = """const token = (() => {
      let t = sessionStorage.getItem("modeldb-token");
      if (!t) {
        t = (prompt("ModelDB API token (MDC_API_TOKENS on the ModelDB service, or the token printed in its deploy logs)") || "").trim();
        if (t) sessionStorage.setItem("modeldb-token", t);
      }
      return t;
    })();"""
_SIGN_OUT_ON_401 = """return originalFetch(input, { ...init, headers }).then((r) => {
        if (r.status === 401) { sessionStorage.removeItem("modeldb-token"); location.reload(); }
        return r;
      });"""
app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) != "/"]


@app.get("/", response_class=HTMLResponse)
def explorer() -> HTMLResponse:
    html = (Path(mdc.api.app.__file__).parent / "static" / "index.html").read_text()
    html = html.replace('const token = "__MDC_LOCAL_UI_TOKEN__";', _SIGN_IN)
    html = html.replace("return originalFetch(input, { ...init, headers });", _SIGN_OUT_ON_401)
    if "__MDC_LOCAL_UI_TOKEN__" in html:  # never serve the page if it would still carry a token slot
        return HTMLResponse("The Storage Explorer page changed shape; use the API with a token.", status_code=503)
    return HTMLResponse(html)


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
