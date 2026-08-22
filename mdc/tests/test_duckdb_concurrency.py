"""DuckDBStore must be safe under concurrent access from multiple
threads - found by hand via the Explorer UI, which fires several
`fetch()` calls in parallel against one running server (one DuckDB
connection, many request-handling threads).
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mdc.storage.duckdb_store import DuckDBStore


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    duckdb_store = DuckDBStore(tmp_path / "mdc.duckdb")
    duckdb_store.init_schema()
    return duckdb_store


def test_concurrent_get_and_search_do_not_cross_contaminate(store: DuckDBStore):
    # Populate distinct blocks whose IDs and payloads make cross-talk
    # detectable: if thread A's read is contaminated by thread B's query,
    # the returned payload won't match the requested block_id's own data.
    block_ids = [f"block-{i}" for i in range(50)]
    for block_id in block_ids:
        store.put(block_id, block_id.encode(), metadata={"tag": block_id})

    errors: list[str] = []

    def worker(block_id: str) -> None:
        for _ in range(20):
            payload = store.get(block_id)
            if payload != block_id.encode():
                errors.append(f"{block_id}: got {payload!r}")
            found = store.search(tag=block_id)
            if found != [block_id]:
                errors.append(f"{block_id}: search returned {found!r}")

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(worker, block_ids * 4))

    assert errors == []


def test_concurrent_metadata_lookups_for_the_same_block_are_consistent(store: DuckDBStore):
    # The exact shape of the bug found by hand: two different read
    # operations on the SAME block_id, fired concurrently, must never
    # disagree about whether it exists.
    store.put("shared", b"payload", metadata={"k": "v"})

    results: list[bool] = []

    def worker(_: int) -> None:
        results.append(store.exists("shared"))

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(worker, range(200)))

    assert all(results)
