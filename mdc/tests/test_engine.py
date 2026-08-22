"""MDCDataEngine: CRUD against the DuckDB-backed StorageBackend (CLAUDE.md
sections 4, 11, 58-59's non-NLP half - the engine must work without NLP)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from mdc.engine.data_engine import MDCDataEngine
from mdc.engine.errors import RecordNotFoundError
from mdc.model.operation import CreateOperation, DeleteOperation, Filter, ReadOperation, Sort, UpdateOperation
from mdc.schema.loader import load_default_registry
from mdc.schema.registry import SchemaValidationError
from mdc.storage.duckdb_store import DuckDBStore


@pytest.fixture
def engine(tmp_path: Path) -> MDCDataEngine:
    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    return MDCDataEngine(store, load_default_registry())


def test_create_then_read_round_trips(engine: MDCDataEngine):
    created = engine.create(CreateOperation(collection="merchants", data={"name": "ABC Store", "country": "India"}))
    assert created.count == 1
    record = created.records[0]
    assert record.fields == {"name": "ABC Store", "country": "India"}
    assert record.version == 1

    read = engine.read(ReadOperation(collection="merchants", filters=[Filter(field="name", operator="=", value="ABC Store")]))
    assert read.count == 1
    assert read.records[0].record_id == record.record_id


def test_create_rejects_missing_required_field(engine: MDCDataEngine):
    with pytest.raises(SchemaValidationError):
        engine.create(CreateOperation(collection="merchants", data={"country": "India"}))


def test_update_merges_fields_and_bumps_version(engine: MDCDataEngine):
    engine.create(CreateOperation(collection="merchants", data={"name": "ABC Store", "country": "India"}))
    result = engine.update(
        UpdateOperation(
            collection="merchants",
            filters=[Filter(field="name", operator="=", value="ABC Store")],
            data={"settlement_balance": 15000},
        )
    )
    updated = result.records[0]
    assert updated.fields["settlement_balance"] == 15000.0
    assert updated.fields["country"] == "India"  # untouched fields survive
    assert updated.version == 2


def test_update_with_no_match_raises_not_found(engine: MDCDataEngine):
    with pytest.raises(RecordNotFoundError):
        engine.update(
            UpdateOperation(
                collection="merchants",
                filters=[Filter(field="name", operator="=", value="Nobody")],
                data={"status": "active"},
            )
        )


def test_update_operation_requires_a_filter():
    with pytest.raises(ValidationError):
        UpdateOperation(collection="merchants", filters=[], data={"status": "active"})


def test_delete_operation_requires_a_filter():
    with pytest.raises(ValidationError):
        DeleteOperation(collection="merchants", filters=[])


def test_delete_removes_the_record(engine: MDCDataEngine):
    engine.create(CreateOperation(collection="merchants", data={"name": "ABC Store", "country": "India"}))
    result = engine.delete(DeleteOperation(collection="merchants", filters=[Filter(field="name", operator="=", value="ABC Store")]))
    assert result.count == 1

    read = engine.read(ReadOperation(collection="merchants", filters=[]))
    assert read.count == 0


def test_delete_with_no_match_raises_not_found(engine: MDCDataEngine):
    with pytest.raises(RecordNotFoundError):
        engine.delete(DeleteOperation(collection="merchants", filters=[Filter(field="name", operator="=", value="Nobody")]))


def test_read_respects_sort_and_limit(engine: MDCDataEngine):
    for name, balance in [("A", 300), ("B", 100), ("C", 200)]:
        engine.create(CreateOperation(collection="merchants", data={"name": name, "country": "India", "settlement_balance": balance}))

    result = engine.read(
        ReadOperation(collection="merchants", filters=[], sort=[Sort(field="settlement_balance", direction="DESC")], limit=2)
    )
    assert [r.fields["name"] for r in result.records] == ["A", "C"]


def test_two_records_are_independent(engine: MDCDataEngine):
    engine.create(CreateOperation(collection="merchants", data={"name": "ABC Store", "country": "India"}))
    engine.create(CreateOperation(collection="merchants", data={"name": "XYZ Retail", "country": "US"}))

    all_records = engine.read(ReadOperation(collection="merchants", filters=[]))
    assert all_records.count == 2

    engine.update(
        UpdateOperation(
            collection="merchants",
            filters=[Filter(field="name", operator="=", value="ABC Store")],
            data={"status": "active"},
        )
    )
    xyz = engine.read(ReadOperation(collection="merchants", filters=[Filter(field="name", operator="=", value="XYZ Retail")]))
    assert "status" not in xyz.records[0].fields
