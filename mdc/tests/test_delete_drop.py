"""Deleting things: drop a database or table, delete a row or a stream - from
the engine up through REST and chat. Everything here is irreversible, so the
tests are mostly about what must NOT happen: no deletion without
confirmation, nothing deleted that should be protected, no deletion by a
role that may not.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mdc.api.app import create_app
from mdc.api.chat import ChatEngine
from mdc.databases.errors import CannotDropDefaultDatabaseError, DatabaseNotFoundError, InvalidDatabaseNameError, ProtectedTableError
from mdc.databases.manager import DatabaseManager
from mdc.engine.errors import RecordNotFoundError
from mdc.model.operation import CreateOperation, ReadOperation
from mdc.schema.loader import load_default_registry
from mdc.schema.registry import CollectionNotFoundError, FieldSchema
from mdc.security.roles import Permission, Role, role_has
from mdc.security.tokens import TokenStore
from mdc.storage.duckdb_store import DuckDBStore

KEY = b"\x33" * 32


def make_manager(root: Path) -> DatabaseManager:
    store = DuckDBStore(root / "mdc.duckdb")
    store.init_schema()
    return DatabaseManager(root / "databases", store, load_default_registry(), event_master_key=KEY)


@pytest.fixture
def manager(tmp_path: Path) -> DatabaseManager:
    return make_manager(tmp_path)


def shop(manager: DatabaseManager, name: str = "shop") -> None:
    """A database with two tables, rows, an object and a stream in it."""
    handle = manager.create(name)
    handle.schema_registry.create_collection("orders", {"sku": FieldSchema("sku", "string"), "qty": FieldSchema("qty", "integer")})
    handle.schema_registry.create_collection("notes", {"text": FieldSchema("text", "string")})
    manager.persist_schema(name)
    for sku, qty in (("A1", 2), ("B2", 5), ("C3", 1)):
        handle.engine.create(CreateOperation(collection="orders", data={"sku": sku, "qty": qty}))
    handle.engine.create(CreateOperation(collection="notes", data={"text": "hello"}))
    handle.object_service.upload(b"# a document\n\nbody text", filename="doc.md")
    manager.events(name).create_stream("agent-1", {"mood": "calm"})


# -- permission ----------------------------------------------------------------------

def test_only_db_admin_and_admin_may_drop():
    assert [r.value for r in Role if role_has(r, Permission.DROP)] == ["db_admin", "admin"]
    assert not role_has(Role.EDITOR, Permission.DROP) and role_has(Role.EDITOR, Permission.WRITE)


# -- engine --------------------------------------------------------------------------

def test_count_delete_record_and_drop_collection(manager: DatabaseManager):
    shop(manager)
    engine = manager.get("shop").engine
    assert engine.count_records("orders") == 3
    record = engine.read(ReadOperation(collection="orders", filters=[])).records[0]
    engine.delete_record("orders", record.record_id)
    assert engine.count_records("orders") == 2 and not engine.has_record("orders", record.record_id)
    with pytest.raises(RecordNotFoundError):
        engine.delete_record("orders", record.record_id)
    with pytest.raises(CollectionNotFoundError):
        engine.delete_record("ghost", "x")
    assert engine.drop_collection("orders") == 2
    with pytest.raises(CollectionNotFoundError):
        engine.count_records("orders")
    assert engine.count_records("notes") == 1  # a neighbouring table is untouched


# -- manager -------------------------------------------------------------------------

def test_summarize_reports_what_a_database_holds(manager: DatabaseManager):
    shop(manager)
    assert manager.summarize("shop") == {"tables": {"notes": 1, "orders": 3}, "rows": 4, "objects": 1, "streams": 1}


def test_dropping_a_database_removes_everything_and_frees_the_name(manager: DatabaseManager, tmp_path: Path):
    shop(manager)
    files = [p for p in (tmp_path / "databases").iterdir() if p.name.startswith("shop")]
    assert files
    held = manager.drop("shop")
    assert held["rows"] == 4 and held["streams"] == 1
    assert [p for p in (tmp_path / "databases").iterdir() if p.name.startswith("shop")] == []
    assert not manager.exists("shop") and "shop" not in manager.list_names()
    with pytest.raises(DatabaseNotFoundError):
        manager.get("shop")
    reborn = manager.create("shop")  # the name is free again, and starts empty
    assert reborn.schema_registry.list_collections() == [] and manager.events("shop").list_streams() == []


def test_a_dropped_database_stays_dropped_after_a_restart(tmp_path: Path):
    first = make_manager(tmp_path)
    shop(first)
    first.drop("shop")
    assert "shop" not in make_manager(tmp_path).list_names()


def test_other_databases_are_untouched_by_a_drop(manager: DatabaseManager):
    shop(manager, "keep")
    shop(manager, "lose")
    manager.drop("lose")
    assert manager.summarize("keep")["rows"] == 4 and manager.events("keep").exists("agent-1")


def test_the_default_database_and_unknown_or_invalid_names_are_refused(manager: DatabaseManager):
    with pytest.raises(CannotDropDefaultDatabaseError):
        manager.drop("default")
    with pytest.raises(DatabaseNotFoundError):
        manager.drop("nope")
    with pytest.raises(InvalidDatabaseNameError):
        manager.drop("../../etc")


def test_dropping_a_table_removes_its_rows_and_it_stays_gone_after_a_restart(tmp_path: Path):
    first = make_manager(tmp_path)
    shop(first)
    assert first.drop_table("shop", "orders") == 3
    assert first.get("shop").schema_registry.list_collections() == ["notes"]
    second = make_manager(tmp_path)
    assert second.get("shop").schema_registry.list_collections() == ["notes"]
    second.get("shop").schema_registry.create_collection("orders", {"sku": FieldSchema("sku", "string")})
    assert second.get("shop").engine.count_records("orders") == 0  # no ghost rows come back with the name


def test_the_built_in_merchants_table_cannot_be_dropped_but_other_default_tables_can(manager: DatabaseManager):
    with pytest.raises(ProtectedTableError):
        manager.drop_table("default", "merchants")
    manager.get("default").schema_registry.create_collection("scratch", {"a": FieldSchema("a", "string")})
    assert manager.drop_table("default", "scratch") == 0
    # and merchants may be called that in another database
    shop(manager)
    manager.get("shop").schema_registry.create_collection("merchants", {"a": FieldSchema("a", "string")})
    assert manager.drop_table("shop", "merchants") == 0


def test_a_table_dropped_from_default_does_not_return_after_restart(tmp_path: Path):
    first = make_manager(tmp_path)
    first.get("default").schema_registry.create_collection("scratch", {"a": FieldSchema("a", "string")})
    first.persist_schema("default")
    first.drop_table("default", "scratch")
    assert "scratch" not in make_manager(tmp_path).get("default").schema_registry.list_collections()


# -- REST ----------------------------------------------------------------------------

def client_for(manager: DatabaseManager, tmp_path: Path):
    tokens = TokenStore(tmp_path / "api_tokens.json")
    client = TestClient(create_app(manager, token_store=tokens))

    def as_role(role: Role) -> TestClient:
        other = TestClient(client.app)
        other.headers.update({"Authorization": f"Bearer {tokens.issue(f'{role.value}-c', role=role)}"})
        return other

    return client, as_role


@pytest.fixture
def api(manager: DatabaseManager, tmp_path: Path):
    shop(manager)
    return client_for(manager, tmp_path)


def test_drop_database_needs_the_name_repeated_as_confirm(api, manager: DatabaseManager):
    _, as_role = api
    admin = as_role(Role.ADMIN)
    unconfirmed = admin.delete("/databases/shop")
    assert unconfirmed.status_code == 422
    assert "2 table(s), 4 row(s), 1 object(s), 1 stream(s)" in unconfirmed.json()["detail"] and "?confirm=shop" in unconfirmed.json()["detail"]
    assert admin.delete("/databases/shop?confirm=wrong").status_code == 422
    assert manager.exists("shop")  # still there
    done = admin.delete("/databases/shop?confirm=shop")
    assert done.status_code == 200 and done.json() == {"dropped": "shop", "tables": 2, "rows": 4, "objects": 1, "streams": 1}
    assert not manager.exists("shop")
    assert admin.get("/databases/shop/tables").status_code == 404


def test_drop_database_errors(api):
    _, as_role = api
    admin = as_role(Role.ADMIN)
    assert admin.delete("/databases/default?confirm=default").status_code == 409
    assert admin.delete("/databases/nope?confirm=nope").status_code == 404


def test_drop_table_needs_confirm_and_reports_the_rows_lost(api, manager: DatabaseManager):
    _, as_role = api
    admin = as_role(Role.ADMIN)
    unconfirmed = admin.delete("/databases/shop/tables/orders")
    assert unconfirmed.status_code == 422 and "3 row(s)" in unconfirmed.json()["detail"]
    assert manager.get("shop").engine.count_records("orders") == 3
    done = admin.delete("/databases/shop/tables/orders?confirm=orders")
    assert done.status_code == 200 and done.json() == {"database": "shop", "dropped": "orders", "rows_deleted": 3}
    assert admin.get("/databases/shop/tables/orders").status_code == 404
    assert admin.delete("/databases/shop/tables/orders?confirm=orders").status_code == 404
    assert admin.delete("/databases/default/tables/merchants?confirm=merchants").status_code == 409
    assert admin.delete("/databases/nope/tables/x?confirm=x").status_code == 404


def test_delete_one_row(api):
    _, as_role = api
    admin = as_role(Role.ADMIN)
    rows = admin.get("/databases/shop/tables/orders/rows").json()["rows"]
    target = rows[0]["record_id"]
    assert admin.delete(f"/databases/shop/tables/orders/rows/{target}").status_code == 204
    remaining = [r["record_id"] for r in admin.get("/databases/shop/tables/orders/rows").json()["rows"]]
    assert target not in remaining and len(remaining) == 2
    assert admin.delete(f"/databases/shop/tables/orders/rows/{target}").status_code == 404
    assert admin.delete("/databases/shop/tables/ghost/rows/x").status_code == 404
    assert admin.delete("/databases/nope/tables/orders/rows/x").status_code == 404


def test_delete_stream_now_needs_drop(api):
    _, as_role = api
    url = "/databases/shop/streams/agent-1"
    assert as_role(Role.EDITOR).delete(url).status_code == 403
    assert as_role(Role.VIEWER).delete(url).status_code == 403
    assert as_role(Role.DB_ADMIN).delete(url).status_code == 204
    assert as_role(Role.ADMIN).get(url).status_code == 404


def test_roles_decide_who_may_delete_what(api, manager: DatabaseManager):
    client, as_role = api
    viewer, editor, dba = as_role(Role.VIEWER), as_role(Role.EDITOR), as_role(Role.DB_ADMIN)
    row = viewer.get("/databases/shop/tables/orders/rows").json()["rows"][0]["record_id"]
    assert viewer.delete(f"/databases/shop/tables/orders/rows/{row}").status_code == 403
    assert editor.delete("/databases/shop/tables/orders?confirm=orders").status_code == 403  # editors write rows, not drop tables
    assert editor.delete("/databases/shop?confirm=shop").status_code == 403
    assert manager.exists("shop") and manager.get("shop").engine.count_records("orders") == 3
    assert editor.delete(f"/databases/shop/tables/orders/rows/{row}").status_code == 204  # but may delete a row
    for call in (client.delete("/databases/shop?confirm=shop"), client.delete("/databases/shop/tables/orders?confirm=orders")):
        assert call.status_code == 401
    assert dba.delete("/databases/shop/tables/orders?confirm=orders").status_code == 200


# -- chat ----------------------------------------------------------------------------

@pytest.fixture
def chat(manager: DatabaseManager) -> ChatEngine:
    shop(manager)
    return ChatEngine(manager)


def switch(chat: ChatEngine, name: str = "shop", session: str = "s") -> None:
    chat.handle(session, f"use database {name}")


def test_drop_table_asks_first_and_deletes_only_on_yes(chat: ChatEngine, manager: DatabaseManager):
    switch(chat)
    asked = chat.handle("s", "drop table orders")
    assert "permanently deletes table" in asked.message and "3 row(s)" in asked.message and "yes" in asked.message
    assert manager.get("shop").engine.count_records("orders") == 3  # nothing happened yet
    done = chat.handle("s", "yes")
    assert done.message == 'Deleted table "orders" from "shop" (3 row(s)).'
    assert manager.get("shop").schema_registry.list_collections() == ["notes"]


@pytest.mark.parametrize("answer", ["no", "nope", "cancel", "list databases", "something else entirely", ""])
def test_anything_but_yes_cancels_and_keeps_the_data(chat: ChatEngine, manager: DatabaseManager, answer):
    switch(chat)
    chat.handle("s", "drop table orders")
    chat.handle("s", answer)
    chat.handle("s", "yes")  # nothing is pending any more
    assert manager.get("shop").engine.count_records("orders") == 3


def test_a_stray_yes_after_an_unrelated_turn_deletes_nothing(chat: ChatEngine, manager: DatabaseManager):
    switch(chat)
    chat.handle("s", "drop table orders")
    chat.handle("s", "list databases")  # moves on - the proposal is dropped
    assert "Deleted" not in chat.handle("s", "yes").message
    assert manager.get("shop").engine.count_records("orders") == 3


def test_dropping_a_database_needs_its_exact_name_not_yes(chat: ChatEngine, manager: DatabaseManager):
    asked = chat.handle("s", "drop database shop")
    assert "2 table(s) holding 4 row(s), 1 object(s), 1 stream(s)" in asked.message and "type the database name (shop)" in asked.message
    chat.handle("s", "yes")
    assert manager.exists("shop")
    chat.handle("s", "drop database shop")
    chat.handle("s", "Shop")  # wrong case is not the name
    assert manager.exists("shop")
    chat.handle("s", "drop database shop")
    assert chat.handle("s", "shop").message.startswith('Deleted database "shop"')
    assert not manager.exists("shop")


def test_the_session_in_a_dropped_database_is_moved_back_to_default(chat: ChatEngine):
    switch(chat)
    chat.handle("s", "drop database shop")
    assert 'You are back in "default"' in chat.handle("s", "shop").message
    assert chat.session_database("s") == "default"


def test_another_session_sitting_in_a_dropped_database_recovers_with_a_notice(chat: ChatEngine):
    switch(chat, session="other")
    chat.handle("s", "drop database shop")
    chat.handle("s", "shop")
    reply = chat.handle("other", "list databases")
    assert reply.message.startswith('Database "shop" no longer exists, so I switched back to "default".')
    assert "1 database(s): default" in reply.message


def test_protected_things_are_refused_before_asking(chat: ChatEngine):
    assert "cannot be dropped" in chat.handle("s", "drop database default").message
    assert "built-in table" in chat.handle("s", "drop table merchants").message
    assert "no pending" not in chat.handle("s", "yes").message.lower() and "Deleted" not in chat.handle("s", "yes").message


def test_unknown_targets_are_reported(chat: ChatEngine):
    assert "No database named 'nope'" in chat.handle("s", "drop database nope").message
    assert 'has no table "ghost"' in chat.handle("s", "drop table ghost in database shop").message
    switch(chat)
    assert 'No stream named "ghost"' in chat.handle("s", "drop stream ghost").message
    assert 'has no record "X-1"' in chat.handle("s", "delete row X-1 from table orders").message


def test_a_table_can_be_dropped_in_another_database_without_switching(chat: ChatEngine, manager: DatabaseManager):
    chat.handle("s", "drop table notes in database shop")
    chat.handle("s", "yes")
    assert manager.get("shop").schema_registry.list_collections() == ["orders"]
    assert chat.session_database("s") == "default"


def test_delete_a_row_and_a_stream_from_chat(chat: ChatEngine, manager: DatabaseManager):
    switch(chat)
    record = manager.get("shop").engine.read(ReadOperation(collection="orders", filters=[])).records[0].record_id
    asked = chat.handle("s", f"delete row {record} from table orders")
    assert f"Delete record {record}" in asked.message
    assert chat.handle("s", "yes").message == f'Deleted record {record} from "orders".'
    assert manager.get("shop").engine.count_records("orders") == 2

    assert "1 event(s)" not in chat.handle("s", "update stream agent-1 set mood=busy").message  # sanity: it is a real stream
    asked = chat.handle("s", "delete stream agent-1")
    assert "whole history (1 event(s))" in asked.message
    assert chat.handle("s", "yes").message == 'Deleted stream "agent-1" and its history.'
    assert manager.events("shop").list_streams() == []


def test_roles_limit_chat_deletion_at_the_question_and_again_at_the_answer(chat: ChatEngine, manager: DatabaseManager):
    switch(chat)
    refused = chat.handle("s", "drop table orders", role=Role.EDITOR)
    assert "drop permission" in refused.message and "permanently deletes" not in refused.message
    assert "drop permission" in chat.handle("s", "drop database shop", role=Role.EDITOR).message
    assert "drop permission" in chat.handle("s", "delete stream agent-1", role=Role.EDITOR).message
    # an editor may delete a single row
    record = manager.get("shop").engine.read(ReadOperation(collection="orders", filters=[])).records[0].record_id
    assert "Delete record" in chat.handle("s", f"delete row {record} from table orders", role=Role.EDITOR).message
    assert "Deleted record" in chat.handle("s", "yes", role=Role.EDITOR).message
    # a pending drop proposed by an admin cannot be confirmed by a demoted caller
    chat.handle("s", "drop table orders", role=Role.ADMIN)
    assert "drop permission" in chat.handle("s", "yes", role=Role.EDITOR).message
    assert manager.get("shop").engine.count_records("orders") == 2


@pytest.mark.parametrize("text, intent, database, table", [
    ("drop database sales", "DROP_DATABASE", "sales", None),
    ("delete the database sales", "DROP_DATABASE", "sales", None),
    ("drop table orders", "DROP_TABLE", None, "orders"),
    ("remove the orders table in database shop", "DROP_TABLE", "shop", "orders"),
    ("delete table orders in the shop database", "DROP_TABLE", "shop", "orders"),
])
def test_drop_phrasings(text, intent, database, table):
    from mdc.nlp.db_command import parse_database_command

    command = parse_database_command(text)
    assert command is not None and (command.intent, command.database_name, command.table_name) == (intent, database, table)


@pytest.mark.parametrize("text", ["delete merchant ABC Store", "delete DOC-1", "delete the table", "drop it", "remove the report", "delete everything"])
def test_other_delete_sentences_are_not_taken_for_a_drop(text):
    from mdc.nlp.db_command import parse_database_command

    assert parse_database_command(text) is None


def test_existing_merchant_and_object_deletes_still_work(chat: ChatEngine):
    chat.handle("s", "Create a merchant called Acme in India")
    assert "(yes/no)" in chat.handle("s", "Delete merchant Acme").message
    assert "Deleted 1 record" in chat.handle("s", "yes").message
