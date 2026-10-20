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
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from mdc.model.operation import Filter
from mdc.storage.interface import StorageBackend
from mdc.storage.seed import ScaleCounts, seed_database

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "data" / "schema.sql"

DOMAIN_TABLES = (
    "merchant", "customer", "account", "terminal", "card",
    "settlement", "transaction", "payment", "refund", "chargeback",
)


_BULK_FROM = 20  # at or above this many rows, a batch goes in through a file
_RECORD_COLUMNS = "record_id, version, created_at, updated_at, fields, checksum"
_RECORDS_DDL = """
CREATE SEQUENCE IF NOT EXISTS records_seq;
CREATE TABLE IF NOT EXISTS records (
    collection  VARCHAR NOT NULL,
    record_id   VARCHAR NOT NULL,
    version     INTEGER NOT NULL,
    created_at  TIMESTAMP NOT NULL,
    updated_at  TIMESTAMP NOT NULL,
    fields      VARCHAR NOT NULL,
    checksum    BLOB NOT NULL,
    seq         BIGINT NOT NULL DEFAULT nextval('records_seq'),
    PRIMARY KEY (collection, record_id)
);
"""


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _json_path(field: str) -> str:
    return '$."' + field.replace('"', '\\"') + '"'


def _prefilter(f: Filter) -> tuple[str, list[Any]] | None:
    """A SQL condition that every record matching `f` also satisfies (it may
    let a few non-matches through; the engine re-checks them), or None when
    `f` has no safe equivalent. Never excludes a true match."""
    text = f"json_extract_string(fields, ?)"
    num = "TRY_CAST(json_extract_string(fields, ?) AS DOUBLE)"
    path = _json_path(f.field)
    value = f.value

    def is_num(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    if f.operator == "=":
        if isinstance(value, str):
            return f"{text} = ?", [path, value]
        if is_num(value):
            return f"{num} = ?", [path, float(value)]
    elif f.operator in (">", "<", ">=", "<="):
        if isinstance(value, str):
            return f"{text} {f.operator} ?", [path, value]
        if is_num(value):
            return f"{num} {f.operator} ?", [path, float(value)]
    elif f.operator == "BETWEEN" and isinstance(value, (list, tuple)) and len(value) == 2:
        low, high = value
        if is_num(low) and is_num(high):
            return f"{num} BETWEEN ? AND ?", [path, float(low), float(high)]
        if isinstance(low, str) and isinstance(high, str):
            return f"{text} BETWEEN ? AND ?", [path, low, high]
    elif f.operator == "IN" and isinstance(value, (list, tuple)) and value:
        if all(isinstance(v, str) for v in value):
            return f"{text} IN ({','.join('?' * len(value))})", [path, *value]
        if all(is_num(v) for v in value):
            return f"{num} IN ({','.join('?' * len(value))})", [path, *[float(v) for v in value]]
    return None


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
            self.conn.execute(_RECORDS_DDL)
            self._migrate_block_records()

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

    # -- records: the Data Engine's fast path ---------------------------------------
    #
    # A table of its own instead of one JSON block per record: one query
    # reads a whole collection (not one per record), a record is found by
    # (collection, record_id) through the primary key, and the envelope
    # (version, times, checksum) sits in typed columns that compress, rather
    # than being repeated inside every JSON text. Only `fields` is JSON.
    # `mdc.storage.interface.RecordBackend` is the contract.

    def record_insert_many(self, rows: list[tuple], *, replace: bool = False) -> None:
        """Insert (collection, record_id, version, created_at, updated_at,
        fields_json, checksum_hex) rows in one transaction; with `replace`,
        an existing (collection, record_id) is updated in place (keeping its
        position). Without it a duplicate raises."""
        if not rows:
            return
        with self._lock:
            if len(rows) == 1:
                self._insert_rows(rows, replace)  # a single statement is already atomic
                return
            self.conn.execute("BEGIN")
            try:
                self._insert_rows(rows, replace)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def _insert_rows(self, rows: list[tuple], replace: bool) -> None:
        """Caller holds the lock (and a transaction, for more than one row).

        Binding Python values into a statement costs about 0.1 ms each in the
        DuckDB client, which dominated the insert time, so a row travels as
        ONE JSON parameter that SQL takes apart, and a batch is written to a
        temporary file that DuckDB reads in one go (about 100 times faster
        per row than binding)."""
        tail = (
            " ON CONFLICT (collection, record_id) DO UPDATE SET version = excluded.version, updated_at = excluded.updated_at,"
            " fields = excluded.fields, checksum = excluded.checksum"
            if replace
            else ""
        )
        head = "INSERT INTO records (collection, record_id, version, created_at, updated_at, fields, checksum) "
        docs = [
            {"c": c, "i": i, "v": v, "a": _naive_utc(a).isoformat(), "b": _naive_utc(b).isoformat(), "f": f, "k": k}
            for c, i, v, a, b, f, k in rows
        ]
        if len(docs) < _BULK_FROM:
            for doc in docs:
                self.conn.execute(
                    head
                    + "SELECT j->>'c', j->>'i', (j->>'v')::INTEGER, (j->>'a')::TIMESTAMP, (j->>'b')::TIMESTAMP, j->>'f', unhex(j->>'k')"
                    + " FROM (SELECT ?::JSON AS j)"
                    + tail,
                    [json.dumps(doc)],
                )
            return
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(docs, handle)
            path = handle.name
        try:
            quoted = path.replace("'", "''")
            self.conn.execute(
                head
                + "SELECT c, i, v, a::TIMESTAMP, b::TIMESTAMP, f, unhex(k) FROM read_json('"
                + quoted
                + "', format='array', columns={c:'VARCHAR', i:'VARCHAR', v:'INTEGER', a:'VARCHAR', b:'VARCHAR', f:'VARCHAR', k:'VARCHAR'})"
                + tail
            )
        finally:
            os.unlink(path)

    def record_get(self, collection: str, record_id: str) -> tuple | None:
        with self._lock:
            return self.conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM records WHERE collection = ? AND record_id = ?", [collection, record_id]
            ).fetchone()

    def record_exists(self, collection: str, record_id: str) -> bool:
        with self._lock:
            return self.conn.execute("SELECT 1 FROM records WHERE collection = ? AND record_id = ?", [collection, record_id]).fetchone() is not None

    def record_existing_ids(self, collection: str, record_ids: list[str]) -> set[str]:
        if not record_ids:
            return set()
        with self._lock:
            rows = self.conn.execute(
                "SELECT record_id FROM records WHERE collection = ? AND record_id IN (SELECT unnest(?))", [collection, record_ids]
            ).fetchall()
        return {r[0] for r in rows}

    def record_count(self, collection: str) -> int:
        with self._lock:
            return self.conn.execute("SELECT count(*) FROM records WHERE collection = ?", [collection]).fetchone()[0]

    def record_delete(self, collection: str, record_id: str) -> bool:
        with self._lock:
            found = self.conn.execute("SELECT 1 FROM records WHERE collection = ? AND record_id = ?", [collection, record_id]).fetchone()
            self.conn.execute("DELETE FROM records WHERE collection = ? AND record_id = ?", [collection, record_id])
            return found is not None

    def record_delete_collection(self, collection: str) -> int:
        with self._lock:
            n = self.conn.execute("SELECT count(*) FROM records WHERE collection = ?", [collection]).fetchone()[0]
            self.conn.execute("DELETE FROM records WHERE collection = ?", [collection])
            return n

    def record_query(self, collection: str, filters: list[Filter], limit: int | None = None) -> tuple[list[tuple], bool]:
        """Rows of a collection in insertion order, narrowed by whichever of
        `filters` can be checked inside the database. The rows are a
        SUPERSET of the matches: the caller still applies every filter itself
        (that is what keeps the semantics exactly the engine's), so this is a
        speed-up only. Returns (rows, exact); `exact` is True only when no
        filter was given, which is when `limit` may be applied here."""
        clauses, params = ["collection = ?"], [collection]
        for f in filters:
            pushed = _prefilter(f)
            if pushed:
                clauses.append(pushed[0])
                params += pushed[1]
        exact = not filters
        sql = f"SELECT {_RECORD_COLUMNS} FROM records WHERE {' AND '.join(clauses)} ORDER BY seq"
        if exact and limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            return self.conn.execute(sql, params).fetchall(), exact

    def _insert_new(self, rows: list[tuple]) -> None:
        """Rows from a migration that was cut short are already in `records`;
        add only the ones that are not."""
        have = {(c, i) for c, i in self.conn.execute("SELECT collection, record_id FROM records").fetchall()}
        self._insert_rows([r for r in rows if (r[0], r[1]) not in have], replace=False)

    def _migrate_block_records(self) -> None:
        """Move records an earlier version stored as `<collection>:<id>` JSON
        blocks into the records table. Idempotent, one transaction, and the
        blocks are deleted only after the rows are in and counted."""
        found = self.conn.execute(
            "SELECT block_id, payload FROM blocks WHERE json_extract_string(metadata, '$.collection') IS NOT NULL"
            " AND json_extract_string(metadata, '$.record_id') IS NOT NULL ORDER BY created_at, block_id"
        ).fetchall()
        if not found:
            return
        rows = []
        for block_id, payload in found:
            data = json.loads(payload)
            rows.append((data["collection"], data["record_id"], int(data.get("version", 1)), _parse_dt(data["created_at"]), _parse_dt(data["updated_at"]),
                         json.dumps(data.get("fields", {}), separators=(",", ":")), data.get("checksum") or hashlib.sha256(json.dumps(data.get("fields", {}), sort_keys=True, default=str).encode("utf-8")).hexdigest()))
        self.conn.execute("BEGIN")
        try:
            self._insert_new(rows)
            have = {(c, i) for c, i in self.conn.execute("SELECT collection, record_id FROM records").fetchall()}
            missing = [r for r in rows if (r[0], r[1]) not in have]
            if missing:
                raise RuntimeError(f"migration check failed: {len(missing)} of {len(rows)} records are not in the records table after copying")
            self.conn.execute(
                "DELETE FROM blocks WHERE json_extract_string(metadata, '$.collection') IS NOT NULL AND json_extract_string(metadata, '$.record_id') IS NOT NULL"
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

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
