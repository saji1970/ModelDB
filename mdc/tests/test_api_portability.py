"""REST + chat surface of database import/export: /databases/{name}/export,
/databases/import, /chat/attach, and the chat's export commands.
"""

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mdc.api.app as app_module
from mdc.api.app import create_app
from mdc.databases.manager import DatabaseManager
from mdc.schema.loader import load_default_registry
from mdc.security.roles import Role
from mdc.security.tokens import TokenStore
from mdc.storage.duckdb_store import DuckDBStore


def build(tmp_path: Path):
    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    manager = DatabaseManager(tmp_path / "databases", store, load_default_registry())
    tokens = TokenStore(tmp_path / "api_tokens.json")
    return TestClient(create_app(manager, token_store=tokens)), tokens


def as_role(client: TestClient, tokens: TokenStore, role: Role) -> TestClient:
    other = TestClient(client.app)
    other.headers.update({"Authorization": f"Bearer {tokens.issue(f'{role.value}-client', role=role)}"})
    return other


@pytest.fixture
def parts(tmp_path: Path):
    client, tokens = build(tmp_path)
    admin = as_role(client, tokens, Role.ADMIN)
    return client, tokens, admin


def upload(c: TestClient, filename: str, body: bytes | str, **params):
    query = "&".join([f"filename={filename}", *(f"{k}={v}" for k, v in params.items())])
    data = body.encode() if isinstance(body, str) else body
    return c.post(f"/databases/import?{query}", content=data)


# -- import ---------------------------------------------------------------------------

def test_import_csv_creates_a_table_in_the_named_database(parts):
    _, _, admin = parts
    response = upload(admin, "people.csv", "name,age\nAda,36\nBob,41\n", database="crm")
    assert response.status_code == 201
    body = response.json()
    assert body["database"] == "crm" and body["created_database"] and body["rows_inserted"] == {"people": 2}
    rows = admin.get("/databases/crm/tables/people/rows").json()["rows"]
    assert sorted(r["name"] for r in rows) == ["Ada", "Bob"]
    assert {f["name"]: f["type"] for f in admin.get("/databases/crm/tables/people").json()["fields"]}["age"] == "integer"


def test_import_markdown_design_document_creates_an_empty_database(parts):
    _, _, admin = parts
    doc = "# Database: lib\n\n## Table: books\n\n| Field | Type | Required |\n|--|--|--|\n| title | string | yes |\n| pages | integer | no |\n"
    response = upload(admin, "design.md", doc)
    assert response.status_code == 201 and response.json()["database"] == "lib"
    assert "lib" in admin.get("/databases").json()["databases"]
    assert [t["name"] for t in admin.get("/databases/lib/tables").json()["tables"]] == ["books"]


def test_import_sql_script_runs_nothing_but_loads_tables_and_rows(parts):
    _, _, admin = parts
    script = "DROP TABLE merchants; CREATE TABLE t (a INT, b TEXT); INSERT INTO t VALUES (1, 'x'), (2, 'y');"
    response = upload(admin, "s.sql", script, database="sqldb")
    assert response.status_code == 201
    assert any("never" in w or "ever executed" in w for w in response.json()["warnings"])
    assert len(admin.get("/databases/sqldb/tables/t/rows").json()["rows"]) == 2
    assert admin.get("/databases/default/tables/merchants").status_code == 200  # untouched


def test_import_errors_map_to_422_409_and_nothing_is_created(parts):
    _, _, admin = parts
    assert upload(admin, "notes.docx", "x").status_code == 422
    assert upload(admin, "bad.sql", "CREATE TABLE t (a INT); INSERT INTO t VALUES (NOW());", database="x1").status_code == 422
    typed = upload(admin, "typed.sql", "CREATE TABLE t (n INT); INSERT INTO t VALUES ('abc');", database="x2")
    assert typed.status_code == 422 and "integer" in typed.json()["detail"]
    assert "x1" not in admin.get("/databases").json()["databases"] and "x2" not in admin.get("/databases").json()["databases"]
    assert upload(admin, "t.csv", "a\n1\n", database="x3").status_code == 201
    again = upload(admin, "t.csv", "a\n2\n", database="x3")
    assert again.status_code == 409 and "already exists" in again.json()["detail"]
    assert upload(admin, "empty.csv", "").status_code == 422
    assert upload(admin, "t.csv", "a\n1\n", database="../etc").status_code == 422


def test_import_over_the_size_limit_is_413(parts, monkeypatch):
    _, _, admin = parts
    monkeypatch.setattr(app_module, "_MAX_IMPORT_BYTES", 50)
    assert upload(admin, "big.csv", "a\n" + "1\n" * 100, database="big").status_code == 413


def test_import_requires_a_token(parts):
    client, _, _ = parts
    assert upload(client, "t.csv", "a\n1\n", database="nope").status_code == 401


# -- roles: an import is only allowed to do what the caller's role permits ------------

def test_viewer_cannot_import_anything(parts):
    client, tokens, _ = parts
    viewer = as_role(client, tokens, Role.VIEWER)
    assert upload(viewer, "t.csv", "a\n1\n", database="default").status_code == 403


def test_editor_can_add_tables_to_an_existing_database_but_not_create_a_database(parts):
    client, tokens, admin = parts
    admin.post("/databases", json={"name": "shared"})
    editor = as_role(client, tokens, Role.EDITOR)
    assert upload(editor, "t.csv", "a\n1\n", database="shared").status_code == 201
    denied = upload(editor, "u.csv", "a\n1\n", database="brand-new")
    assert denied.status_code == 403 and "create_database" in denied.json()["detail"]
    assert "brand-new" not in admin.get("/databases").json()["databases"]


def test_db_admin_can_create_a_database_by_importing(parts):
    client, tokens, _ = parts
    assert upload(as_role(client, tokens, Role.DB_ADMIN), "t.csv", "a\n1\n", database="made-by-dba").status_code == 201


# -- export ---------------------------------------------------------------------------

@pytest.fixture
def loaded(parts):
    _, _, admin = parts
    upload(admin, "people.csv", "name,age\nAda,36\nBob,41\n", database="crm")
    return admin


@pytest.mark.parametrize("fmt, scope, media, ext", [
    ("csv", None, "application/zip", "-csv.zip"),
    ("sql", None, "application/sql", ".sql"),
    ("xlsx", None, "spreadsheetml", ".xlsx"),
    ("md", None, "text/markdown", "-design.md"),
    ("er", None, "text/plain", "-er.mmd"),
    ("dnadb", None, "application/json", ".dnadb.json"),
    ("dnadb", "schema", "application/json", "-structure.dnadb.json"),
])
def test_export_download_headers(loaded, fmt, scope, media, ext):
    url = f"/databases/crm/export?format={fmt}" + (f"&scope={scope}" if scope else "")
    response = loaded.get(url)
    assert response.status_code == 200
    assert media in response.headers["content-type"]
    assert f'filename="crm{ext}"' in response.headers["content-disposition"]
    assert response.content


def test_export_single_table_csv(loaded):
    response = loaded.get("/databases/crm/export?format=csv&table=people")
    assert response.headers["content-disposition"].endswith('filename="people.csv"')
    assert response.text.splitlines()[0] == "record_id,name,age"
    assert len(response.text.splitlines()) == 3


def test_export_csv_zip_has_a_file_per_table(loaded):
    archive = zipfile.ZipFile(io.BytesIO(loaded.get("/databases/crm/export?format=csv").content))
    assert archive.namelist() == ["people.csv"]


def test_export_errors(loaded):
    assert loaded.get("/databases/nope/export").status_code == 404
    assert loaded.get("/databases/crm/export?format=pdf").status_code == 422
    assert loaded.get("/databases/crm/export?format=csv&scope=schema").status_code == 422
    assert loaded.get("/databases/crm/export?format=csv&table=ghost").status_code == 404


def test_export_needs_a_token_but_only_read_permission(parts, loaded):
    client, tokens, _ = parts
    assert client.get("/databases/crm/export").status_code == 401
    assert as_role(client, tokens, Role.VIEWER).get("/databases/crm/export?format=sql").status_code == 200


def test_export_then_import_into_a_second_instance(parts, tmp_path):
    _, _, admin = parts
    upload(admin, "people.csv", "name,age\nAda,36\n", database="crm")
    bundle = admin.get("/databases/crm/export?format=dnadb")

    other_client, other_tokens = build(tmp_path / "second")
    other_admin = as_role(other_client, other_tokens, Role.ADMIN)
    imported = other_admin.post("/databases/import?filename=crm.dnadb.json", content=bundle.content)
    assert imported.status_code == 201 and imported.json()["database"] == "crm"
    assert other_admin.get("/databases/crm/tables/people/rows").json()["rows"] == admin.get("/databases/crm/tables/people/rows").json()["rows"]


# -- chat: attach + export commands ---------------------------------------------------

def attach(c: TestClient, filename: str, body: str, session: str = "s1"):
    return c.post(f"/chat/attach?filename={filename}&session_id={session}", content=body.encode())


def test_chat_attach_csv_goes_into_the_sessions_current_database(parts):
    _, _, admin = parts
    admin.post("/chat", json={"message": "create database work", "session_id": "s1"})
    response = attach(admin, "tasks.csv", "title\nwrite tests\n")
    assert response.status_code == 200 and response.json()["database"] == "work"
    assert len(admin.get("/databases/work/tables/tasks/rows").json()["rows"]) == 1


def test_chat_attach_of_a_database_file_switches_the_session_to_it(parts):
    _, _, admin = parts
    doc = "# Database: lib\n\n## Table: books\n\n| Field | Type |\n|--|--|\n| title | string |\n"
    assert attach(admin, "design.md", doc).json()["created_database"]
    admin.post("/chat", json={"message": "insert into books title=Dune", "session_id": "s1"})
    assert [r["title"] for r in admin.get("/databases/lib/tables/books/rows").json()["rows"]] == ["Dune"]


def test_chat_attach_failure_is_a_clear_http_error(parts):
    _, _, admin = parts
    response = attach(admin, "bad.sql", "SELECT 1;")
    assert response.status_code == 422 and "no CREATE TABLE" in response.json()["detail"]


def test_chat_export_command_returns_a_download_that_works(loaded):
    reply = loaded.post("/chat", json={"message": "export database crm as sql", "session_id": "s2"}).json()
    assert reply["download"] == {"url": "/databases/crm/export?format=sql&scope=all", "filename": "crm.sql"}
    fetched = loaded.get(reply["download"]["url"])
    assert fetched.status_code == 200 and "CREATE TABLE" in fetched.text


def test_chat_export_uses_the_current_database_when_none_is_named(loaded):
    loaded.post("/chat", json={"message": "use database crm", "session_id": "s3"})
    reply = loaded.post("/chat", json={"message": "export schema as markdown", "session_id": "s3"}).json()
    assert reply["download"]["filename"] == "crm-design.md"


def test_chat_export_explains_a_bad_combination_instead_of_failing(loaded):
    reply = loaded.post("/chat", json={"message": "export database crm schema as csv", "session_id": "s4"}).json()
    assert reply["download"] is None and "CSV holds data only" in reply["message"]


def test_chat_other_replies_have_no_download(loaded):
    assert loaded.post("/chat", json={"message": "list databases", "session_id": "s5"}).json()["download"] is None
