"""DatabaseManager: unlimited, independently-created named databases,
each a fully isolated DuckDB file (own blocks table, own schema
registry, own object index) - including the path-traversal safety
boundary around user-supplied database names.
"""

from pathlib import Path

import pytest

from mdc.databases.errors import DatabaseAlreadyExistsError, DatabaseNotFoundError, InvalidDatabaseNameError
from mdc.databases.manager import DEFAULT_DATABASE, DatabaseManager
from mdc.schema.loader import load_default_registry
from mdc.storage.duckdb_store import DuckDBStore


@pytest.fixture
def manager(tmp_path: Path) -> DatabaseManager:
    store = DuckDBStore(tmp_path / "default.duckdb")
    store.init_schema()
    return DatabaseManager(tmp_path / "databases", store, load_default_registry())


def test_default_database_is_always_available(manager: DatabaseManager):
    assert manager.exists(DEFAULT_DATABASE)
    assert DEFAULT_DATABASE in manager.list_names()


def test_default_database_has_the_merchants_collection(manager: DatabaseManager):
    handle = manager.get(DEFAULT_DATABASE)
    assert handle.schema_registry.has_collection("merchants")


def test_create_new_database_is_completely_empty(manager: DatabaseManager):
    handle = manager.create("mytest")
    assert handle.schema_registry.list_collections() == []


def test_create_persists_a_real_file_on_disk(manager: DatabaseManager, tmp_path: Path):
    manager.create("mytest")
    assert (tmp_path / "databases" / "mytest.duckdb").exists()


def test_no_hardcoded_limit_on_number_of_databases(manager: DatabaseManager):
    for i in range(25):
        manager.create(f"db{i}")
    assert len(manager.list_names()) == 26  # default + 25


def test_create_duplicate_name_raises(manager: DatabaseManager):
    manager.create("mytest")
    with pytest.raises(DatabaseAlreadyExistsError):
        manager.create("mytest")


def test_get_unknown_database_raises(manager: DatabaseManager):
    with pytest.raises(DatabaseNotFoundError):
        manager.get("nope")


def test_get_returns_the_same_cached_handle(manager: DatabaseManager):
    created = manager.create("mytest")
    fetched = manager.get("mytest")
    assert fetched is created


def test_table_structure_survives_a_manager_restart(tmp_path: Path):
    from mdc.model.operation import CreateOperation, ReadOperation
    from mdc.schema.registry import FieldSchema

    store1 = DuckDBStore(tmp_path / "default.duckdb")
    store1.init_schema()
    manager1 = DatabaseManager(tmp_path / "databases", store1, load_default_registry())
    handle1 = manager1.create("mytest")
    handle1.schema_registry.create_collection("products", {"sku": FieldSchema(name="sku", datatype="string")})
    manager1.persist_schema("mytest")
    handle1.engine.create(CreateOperation(collection="products", data={"sku": "ABC123"}))

    # A fresh manager (simulating a server restart) must still know about
    # the "products" table - and must still be able to read the row that
    # was written before the restart (schema/registry.py's `_matching`
    # refuses to read a collection it doesn't know about, so losing the
    # schema would silently strand real, on-disk data).
    store2 = DuckDBStore(tmp_path / "default2.duckdb")
    store2.init_schema()
    manager2 = DatabaseManager(tmp_path / "databases", store2, load_default_registry())
    handle2 = manager2.get("mytest")
    assert handle2.schema_registry.list_collections() == ["products"]
    rows = handle2.engine.read(ReadOperation(collection="products", filters=[]))
    assert rows.count == 1
    assert rows.records[0].fields["sku"] == "ABC123"


def test_get_reopens_a_database_created_in_a_previous_manager_instance(tmp_path: Path):
    store1 = DuckDBStore(tmp_path / "default.duckdb")
    store1.init_schema()
    manager1 = DatabaseManager(tmp_path / "databases", store1, load_default_registry())
    manager1.create("mytest")

    # A fresh manager (simulating a server restart) must still find it on disk.
    store2 = DuckDBStore(tmp_path / "default2.duckdb")
    store2.init_schema()
    manager2 = DatabaseManager(tmp_path / "databases", store2, load_default_registry())
    assert manager2.exists("mytest")
    handle = manager2.get("mytest")
    assert handle.name == "mytest"


def test_databases_are_fully_isolated_from_each_other(manager: DatabaseManager):
    from mdc.model.operation import CreateOperation
    from mdc.schema.registry import FieldSchema

    a = manager.create("db_a")
    b = manager.create("db_b")
    a.schema_registry.create_collection("widgets", {"name": FieldSchema(name="name", datatype="string")})
    b.schema_registry.create_collection("widgets", {"name": FieldSchema(name="name", datatype="string")})

    a.engine.create(CreateOperation(collection="widgets", data={"name": "from-a"}))

    from mdc.model.operation import ReadOperation
    assert a.engine.read(ReadOperation(collection="widgets", filters=[])).count == 1
    assert b.engine.read(ReadOperation(collection="widgets", filters=[])).count == 0


# -- name validation / path-traversal safety --------------------------------------

@pytest.mark.parametrize("bad_name", ["../evil", "/etc/passwd", "has space", "123digit", "", "has$dollar", "a" * 100])
def test_rejects_unsafe_or_malformed_names(manager: DatabaseManager, bad_name: str):
    with pytest.raises(InvalidDatabaseNameError):
        manager.create(bad_name)


def test_valid_names_with_underscores_and_hyphens_are_accepted(manager: DatabaseManager):
    manager.create("my_test-db")
    assert manager.exists("my_test-db")


def test_create_with_traversal_name_does_not_escape_the_databases_directory(manager: DatabaseManager, tmp_path: Path):
    with pytest.raises(InvalidDatabaseNameError):
        manager.create("../../escaped")
    # Nothing was written outside the databases directory.
    assert not (tmp_path.parent / "escaped.duckdb").exists()
