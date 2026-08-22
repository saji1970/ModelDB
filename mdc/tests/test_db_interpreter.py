"""Turn handling for database/table administration
(conversation/db_interpreter.py) - create/use/list/describe databases,
create/describe tables, show data, insert - plus the reset-on-switch
behavior that keeps stale object pointers from leaking across
databases.
"""

from pathlib import Path

import pytest

from mdc.conversation.db_interpreter import handle_database_command
from mdc.conversation.state import StorageConversationState
from mdc.databases.manager import DatabaseManager
from mdc.nlp.db_command import parse_database_command
from mdc.schema.loader import load_default_registry
from mdc.storage.duckdb_store import DuckDBStore


@pytest.fixture
def manager(tmp_path: Path) -> DatabaseManager:
    store = DuckDBStore(tmp_path / "default.duckdb")
    store.init_schema()
    return DatabaseManager(tmp_path / "databases", store, load_default_registry())


@pytest.fixture
def state() -> StorageConversationState:
    return StorageConversationState()


def _turn(state: StorageConversationState, manager: DatabaseManager, text: str):
    command = parse_database_command(text)
    assert command is not None, f"expected {text!r} to parse as a database command"
    return handle_database_command(state, manager, command)


# -- CREATE_DATABASE ------------------------------------------------------------

def test_create_database_switches_to_it(state, manager):
    result = _turn(state, manager, "create database mytest")
    assert "Created database" in result.message
    assert state.current_database == "mytest"


def test_create_database_duplicate_is_reported_without_switching(state, manager):
    _turn(state, manager, "create database mytest")
    state.current_database = "default"
    result = _turn(state, manager, "create database mytest")
    assert "already exists" in result.message.lower() or "exists" in result.message.lower()
    assert state.current_database == "default"


# -- USE_DATABASE ----------------------------------------------------------------

def test_use_database_switches(state, manager):
    manager.create("mytest")
    result = _turn(state, manager, "use database mytest")
    assert state.current_database == "mytest"
    assert "mytest" in result.message


def test_use_unknown_database_does_not_switch(state, manager):
    result = _turn(state, manager, "use database nope")
    assert state.current_database == "default"
    assert "nope" in result.message.lower() or "not found" in result.message.lower()


def test_switching_database_clears_stale_object_pointer(state, manager):
    manager.create("mytest")
    state.last_object_id = "AIM-1"
    state.pending_delete = "AIM-1"
    _turn(state, manager, "use database mytest")
    assert state.last_object_id is None
    assert state.pending_delete is None


# -- LIST_DATABASES ---------------------------------------------------------------

def test_list_databases_flags_current(state, manager):
    manager.create("mytest")
    result = _turn(state, manager, "list databases")
    rows = {row["database"]: row["current"] for row in result.data}
    assert rows["default"] is True
    assert rows["mytest"] is False


# -- CREATE_TABLE / DESCRIBE_DATABASE / DESCRIBE_TABLE ----------------------------

def test_create_table_then_describe_database_lists_it(state, manager):
    _turn(state, manager, "create database mytest")
    _turn(state, manager, "create table products with sku string, price decimal")
    result = _turn(state, manager, "describe database")
    tables = {row["table"] for row in result.data}
    assert tables == {"products"}


def test_create_duplicate_table_is_rejected(state, manager):
    _turn(state, manager, "create database mytest")
    _turn(state, manager, "create table products with sku string")
    result = _turn(state, manager, "create table products with sku string")
    assert "already exists" in result.message.lower()


def test_describe_database_with_no_tables(state, manager):
    _turn(state, manager, "create database mytest")
    result = _turn(state, manager, "describe database")
    assert "no tables" in result.message.lower()


def test_describe_table_lists_fields(state, manager):
    _turn(state, manager, "create database mytest")
    _turn(state, manager, "create table products with sku string, price decimal")
    result = _turn(state, manager, "describe table products")
    fields = {row["field"]: row["type"] for row in result.data}
    assert fields == {"sku": "string", "price": "decimal"}


def test_describe_unknown_table_reports_error(state, manager):
    _turn(state, manager, "create database mytest")
    result = _turn(state, manager, "describe table nope")
    assert "nope" in result.message.lower() or "not found" in result.message.lower()


# -- INSERT / SHOW_DATA -----------------------------------------------------------

def test_insert_then_show_data(state, manager):
    _turn(state, manager, "create database mytest")
    _turn(state, manager, "create table products with sku string, name string")
    insert_result = _turn(state, manager, "insert into products sku=ABC123, name=Widget")
    assert "Inserted" in insert_result.message

    show_result = _turn(state, manager, "show data in products")
    assert show_result.data is not None
    assert len(show_result.data) == 1
    assert show_result.data[0]["sku"] == "ABC123"


def test_show_data_on_empty_table(state, manager):
    _turn(state, manager, "create database mytest")
    _turn(state, manager, "create table products with sku string")
    result = _turn(state, manager, "show data in products")
    assert "no rows" in result.message.lower()


def test_show_data_on_unknown_table_reports_error(state, manager):
    _turn(state, manager, "create database mytest")
    result = _turn(state, manager, "show data in nope")
    assert "nope" in result.message.lower() or "not found" in result.message.lower()


def test_insert_into_unknown_table_reports_error(state, manager):
    _turn(state, manager, "create database mytest")
    result = _turn(state, manager, "insert into nope sku=ABC123")
    assert "nope" in result.message.lower() or "not found" in result.message.lower()


# -- data isolation between databases via chat turns ------------------------------

def test_data_inserted_in_one_database_is_invisible_in_another(state, manager):
    _turn(state, manager, "create database db_a")
    _turn(state, manager, "create table widgets with name string")
    _turn(state, manager, "insert into widgets name=from-a")

    _turn(state, manager, "create database db_b")
    _turn(state, manager, "create table widgets with name string")
    result = _turn(state, manager, "show data in widgets")
    assert "no rows" in result.message.lower()
