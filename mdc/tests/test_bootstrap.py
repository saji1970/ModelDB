"""Phase 1 acceptance: DuckDB schema initializes and seed data is deterministic."""

from pathlib import Path

import pytest

from mdc.storage.duckdb_store import DOMAIN_TABLES, DuckDBStore
from mdc.storage.seed import SCALE_PRESETS


@pytest.fixture
def small_store(tmp_path: Path) -> DuckDBStore:
    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    store.seed(scale="small")
    yield store
    store.close()


def test_schema_creates_all_domain_tables(tmp_path: Path):
    store = DuckDBStore(tmp_path / "mdc.duckdb")
    store.init_schema()
    tables = {row[0] for row in store.conn.execute("SHOW TABLES").fetchall()}
    for table in DOMAIN_TABLES:
        assert table in tables
    assert "blocks" in tables
    store.close()


def test_seed_row_counts_match_small_preset(small_store: DuckDBStore):
    counts = small_store.table_counts()
    preset = SCALE_PRESETS["small"]
    assert counts["merchant"] == preset.merchants
    assert counts["customer"] == preset.customers
    assert counts["account"] == preset.accounts
    assert counts["transaction"] == preset.transactions
    assert counts["payment"] == preset.payments
    assert counts["settlement"] == preset.settlements


def test_seed_is_deterministic(tmp_path: Path):
    store_a = DuckDBStore(tmp_path / "a.duckdb")
    store_a.init_schema()
    store_a.seed(scale="small", seed=42)

    store_b = DuckDBStore(tmp_path / "b.duckdb")
    store_b.init_schema()
    store_b.seed(scale="small", seed=42)

    merchants_a = store_a.conn.execute("SELECT * FROM merchant ORDER BY merchant_id").fetchall()
    merchants_b = store_b.conn.execute("SELECT * FROM merchant ORDER BY merchant_id").fetchall()
    assert merchants_a == merchants_b

    store_a.close()
    store_b.close()


def test_referential_integrity(small_store: DuckDBStore):
    merchant_ids = {row[0] for row in small_store.conn.execute("SELECT merchant_id FROM merchant").fetchall()}
    txn_merchant_ids = {row[0] for row in small_store.conn.execute("SELECT DISTINCT merchant_id FROM transaction").fetchall()}
    assert txn_merchant_ids.issubset(merchant_ids)

    customer_ids = {row[0] for row in small_store.conn.execute("SELECT customer_id FROM customer").fetchall()}
    txn_customer_ids = {row[0] for row in small_store.conn.execute("SELECT DISTINCT customer_id FROM transaction").fetchall()}
    assert txn_customer_ids.issubset(customer_ids)


def test_generic_block_storage_roundtrip(small_store: DuckDBStore):
    checksum = small_store.put("BLOCK-1", b"hello molecular world", metadata={"kind": "test"})
    assert small_store.exists("BLOCK-1")
    assert small_store.get("BLOCK-1") == b"hello molecular world"
    meta = small_store.metadata("BLOCK-1")
    assert meta["checksum"] == checksum
    assert "BLOCK-1" in small_store.search()
    small_store.delete("BLOCK-1")
    assert not small_store.exists("BLOCK-1")
