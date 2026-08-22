"""DuckDB-backed StorageBackend (CLAUDE.md section 32).

Owns the payments-domain database: schema bootstrap, deterministic
seeding, and parameterized query execution used by the read-only NLP
query pipeline (mce/cql). Also implements the generic block-addressed
StorageBackend contract against the `blocks` table - metadata is
stored as JSON so it can be filtered on (`search(collection=...)`),
which is how `MDCDataEngine` (engine/data_engine.py) lists the records
in a collection - so this backend is interchangeable with a future
MatrixStore / DNAStore.

A single `duckdb.connect()` connection is shared for this store's
whole lifetime, and DuckDB's Python connection object isn't safe for
concurrent use from multiple threads: an `execute()` followed by a
`fetchone()`/`fetchall()` on one thread can be interleaved with
another thread's `execute()` on the same connection, silently
returning the wrong result rather than raising. This is exactly the
situation `api/app.py`'s sync route handlers create under Starlette's
threadpool (two requests firing concurrently against the same store) -
found by hand, not by a test, when the Explorer UI's `Promise.all` of
two concurrent lookups for the same object had one return 404 and the
other 200. `_lock` serializes every execute+fetch sequence so that
can't happen.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import duckdb

from mdc.storage.interface import StorageBackend
from mdc.storage.seed import ScaleCounts, seed_database

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "data" / "schema.sql"

DOMAIN_TABLES = (
    "merchant", "customer", "account", "terminal", "card",
    "settlement", "transaction", "payment", "refund", "chargeback",
)


class DuckDBStore(StorageBackend):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.path))
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # -- schema / seed lifecycle -------------------------------------------------

    def init_schema(self, schema_path: Path = SCHEMA_PATH) -> None:
        with self._lock:
            self.conn.execute(schema_path.read_text())

    def is_seeded(self) -> bool:
        with self._lock:
            tables = {row[0] for row in self.conn.execute("SHOW TABLES").fetchall()}
            if "merchant" not in tables:
                return False
            return self.conn.execute("SELECT count(*) FROM merchant").fetchone()[0] > 0

    def seed(self, scale: str = "full", seed: int = 42) -> ScaleCounts:
        with self._lock:
            return seed_database(self.conn, scale=scale, seed=seed)

    def table_counts(self) -> dict[str, int]:
        with self._lock:
            return {
                table: self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in DOMAIN_TABLES
            }

    # -- parameterized query passthrough (used by the MDQL compiler, section 31) -

    def query(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        with self._lock:
            return self.conn.execute(sql, params or []).fetchall()

    def query_df_columns(self, sql: str, params: list[Any] | None = None) -> tuple[list[str], list[tuple]]:
        with self._lock:
            cursor = self.conn.execute(sql, params or [])
            columns = [d[0] for d in cursor.description]
            return columns, cursor.fetchall()

    # -- StorageBackend: generic block-addressed interface -----------------------

    def put(self, block_id: str, payload: bytes, metadata: dict[str, Any] | None = None) -> str:
        checksum = hashlib.sha256(payload).hexdigest()
        meta_str = json.dumps(metadata) if metadata else None
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO blocks (block_id, payload, metadata, checksum)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (block_id) DO UPDATE SET
                    payload = excluded.payload,
                    metadata = excluded.metadata,
                    checksum = excluded.checksum
                """,
                [block_id, payload, meta_str, checksum],
            )
        return checksum

    def get(self, block_id: str) -> bytes:
        with self._lock:
            row = self.conn.execute(
                "SELECT payload FROM blocks WHERE block_id = ?", [block_id]
            ).fetchone()
        if row is None:
            raise KeyError(f"No block found for block_id={block_id!r}")
        return row[0]

    def exists(self, block_id: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM blocks WHERE block_id = ?", [block_id]
            ).fetchone()
        return row is not None

    def delete(self, block_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM blocks WHERE block_id = ?", [block_id])

    def metadata(self, block_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT metadata, checksum, created_at FROM blocks WHERE block_id = ?",
                [block_id],
            ).fetchone()
        if row is None:
            raise KeyError(f"No block found for block_id={block_id!r}")
        return {"metadata": json.loads(row[0]) if row[0] else None, "checksum": row[1], "created_at": row[2]}

    def search(self, **filters: Any) -> list[str]:
        with self._lock:
            if not filters:
                return [row[0] for row in self.conn.execute("SELECT block_id FROM blocks").fetchall()]
            # Filter keys come only from our own code (never from user/NLP
            # input), so it's safe to interpolate them into the JSON path -
            # asserted here as a defense-in-depth check, not a trust boundary.
            for key in filters:
                assert key.isidentifier(), f"unsafe metadata filter key: {key!r}"
            clauses = " AND ".join(f"json_extract_string(metadata, '$.{key}') = ?" for key in filters)
            rows = self.conn.execute(
                f"SELECT block_id FROM blocks WHERE {clauses}", [str(v) for v in filters.values()]
            ).fetchall()
            return [row[0] for row in rows]
