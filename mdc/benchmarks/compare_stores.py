#!/usr/bin/env python
"""How MDC compares with ordinary databases: size on disk, and speed.

Run it yourself - the numbers depend on your machine:

    pip install psycopg2-binary          # optional: adds PostgreSQL to the comparison
    python benchmarks/compare_stores.py [--sizes 1000 10000] [--out RESULTS.md]

What it measures
  A. Records (the MDC Data Engine vs PostgreSQL, SQLite, DuckDB): the same
     `merchants`-style rows; storage size, insert time (one at a time, and in
     one transaction), a lookup by id, a lookup by a field value (no index on
     it in any store) and a filtered count. PostgreSQL is a throwaway local cluster (needs the
     PostgreSQL binaries on PATH or Homebrew's postgresql@14/15/16); it is the
     engine behind RDS, but with no network between client and server.
  B. Small values (mdc-lite vs a SQLite key-value table vs one plain file per
     key): size apparent and allocated, put and get time. mdc-lite is run
     through its real C library (`mdc-lite/dist/...`, macOS arm64 build).
  C. Sealed data inside MDC Platform: how much bigger event streams and the
     ARCHIVE tier are than the plain data.

It does not measure network latency, concurrency, or anything on a cloud
provider, and every comparison is against a default-configured local
database, not a tuned one.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb  # noqa: E402

from mdc.engine.data_engine import MDCDataEngine  # noqa: E402
from mdc.model.operation import CreateOperation, Filter, ReadOperation  # noqa: E402
from mdc.schema.loader import load_default_registry  # noqa: E402
from mdc.storage.duckdb_store import DuckDBStore  # noqa: E402

COUNTRIES = ["IN", "US", "GB", "DE", "SG", "BR", "AU", "JP"]
SEED = 42


def rows(n: int) -> list[dict]:
    rnd = random.Random(SEED)
    return [
        {
            "name": f"Merchant {i:06d} Trading Co",
            "country": rnd.choice(COUNTRIES),
            "currency": rnd.choice(["USD", "INR", "GBP", "EUR"]),
            "ledger_balance": round(rnd.uniform(0, 100000), 2),
            "available_balance": round(rnd.uniform(0, 100000), 2),
            "settlement_balance": round(rnd.uniform(0, 100000), 2),
        }
        for i in range(n)
    ]


def median_ms(fn, times: int) -> float:
    samples = []
    for _ in range(times):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000)
    return statistics.median(samples)


def size_of(path: Path) -> int:
    return path.stat().st_size


def dir_allocated(path: Path) -> tuple[int, int]:
    """(apparent bytes, allocated bytes) of every file under path."""
    apparent = allocated = 0
    for f in path.rglob("*"):
        if f.is_file():
            st = f.stat()
            apparent += st.st_size
            allocated += st.st_blocks * 512
    return apparent, allocated


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# ---------------------------------------------------------------- A. records


class Postgres:
    """A throwaway local PostgreSQL cluster, default settings."""

    def __init__(self, tmp: Path):
        import psycopg2  # noqa: F401  (raise early if missing)

        bindir = self._find_bin()
        self.bin = bindir
        self.dir = tmp / "pg"
        self.sock = tmp / "pgsock"
        self.sock.mkdir()
        subprocess.run([bindir / "initdb", "-D", self.dir, "-A", "trust", "-U", "bench"], check=True, capture_output=True)
        subprocess.run(
            [bindir / "pg_ctl", "-D", self.dir, "-o", f"-k {self.sock} -c listen_addresses=''", "-w", "-l", tmp / "pg.log", "start"],
            check=True, capture_output=True,
        )
        import psycopg2

        self.psycopg2 = psycopg2
        admin = psycopg2.connect(host=str(self.sock), user="bench", dbname="postgres")
        admin.autocommit = True
        admin.cursor().execute("CREATE DATABASE bench")
        admin.close()
        self.version = subprocess.run([bindir / "postgres", "--version"], capture_output=True, text=True).stdout.strip()

    @staticmethod
    def _find_bin() -> Path:
        found = shutil.which("initdb")
        if found:
            return Path(found).parent
        for v in ("16", "15", "14", "17"):
            p = Path(f"/opt/homebrew/opt/postgresql@{v}/bin")
            if (p / "initdb").exists():
                return p
        raise RuntimeError("PostgreSQL binaries not found")

    def connect(self):
        return self.psycopg2.connect(host=str(self.sock), user="bench", dbname="bench")

    def stop(self):
        subprocess.run([self.bin / "pg_ctl", "-D", self.dir, "-m", "fast", "stop"], capture_output=True)


SQL_TABLE = (
    "CREATE TABLE merchants (id INTEGER PRIMARY KEY, name TEXT NOT NULL, country TEXT NOT NULL, currency TEXT,"
    " ledger_balance NUMERIC(14,2), available_balance NUMERIC(14,2), settlement_balance NUMERIC(14,2))"
)
INSERT_PG = "INSERT INTO merchants VALUES (%s,%s,%s,%s,%s,%s,%s)"
INSERT_Q = "INSERT INTO merchants VALUES (?,?,?,?,?,?,?)"


def tup(i: int, r: dict) -> tuple:
    return (i, r["name"], r["country"], r["currency"], r["ledger_balance"], r["available_balance"], r["settlement_balance"])


def bench_postgres(pg: Postgres, data: list[dict], reads: int, scans: int) -> dict:
    conn = pg.connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS merchants")
    cur.execute(SQL_TABLE)
    t = time.perf_counter()
    for i, r in enumerate(data):
        cur.execute(INSERT_PG, tup(i, r))
    individual = time.perf_counter() - t
    cur.execute("TRUNCATE merchants")
    conn.autocommit = False
    t = time.perf_counter()
    cur.executemany(INSERT_PG, [tup(i, r) for i, r in enumerate(data)])
    conn.commit()
    bulk = time.perf_counter() - t
    conn.autocommit = True
    cur.execute("CHECKPOINT")
    cur.execute("SELECT pg_total_relation_size('merchants')")
    table_bytes = cur.fetchone()[0]
    cur.execute("SELECT pg_database_size('bench')")
    db_bytes = cur.fetchone()[0]
    ids = [random.Random(1).randrange(len(data)) for _ in range(reads)]
    point = median_ms(lambda: cur.execute("SELECT * FROM merchants WHERE id=%s", (random.choice(ids),)) or cur.fetchall(), reads)
    byfield = median_ms(lambda: cur.execute("SELECT * FROM merchants WHERE name=%s", (data[random.choice(ids)]["name"],)) or cur.fetchall(), reads)
    scan = median_ms(lambda: cur.execute("SELECT count(*) FROM merchants WHERE country='IN' AND ledger_balance>50000") or cur.fetchall(), scans)
    conn.close()
    return dict(data_bytes=table_bytes, file_bytes=db_bytes, insert_individual_s=individual, insert_bulk_s=bulk, point_ms=point, field_ms=byfield, scan_ms=scan)


def bench_sqlite(tmp: Path, data: list[dict], reads: int, scans: int) -> dict:
    path = tmp / "bench.sqlite"
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    conn.execute(SQL_TABLE.replace("NUMERIC(14,2)", "REAL"))
    t = time.perf_counter()
    for i, r in enumerate(data):
        conn.execute(INSERT_Q, tup(i, r))
    individual = time.perf_counter() - t
    conn.execute("DELETE FROM merchants")
    t = time.perf_counter()
    conn.execute("BEGIN")
    conn.executemany(INSERT_Q, [tup(i, r) for i, r in enumerate(data)])
    conn.execute("COMMIT")
    bulk = time.perf_counter() - t
    conn.execute("VACUUM")
    ids = [random.Random(1).randrange(len(data)) for _ in range(reads)]
    point = median_ms(lambda: conn.execute("SELECT * FROM merchants WHERE id=?", (random.choice(ids),)).fetchall(), reads)
    byfield = median_ms(lambda: conn.execute("SELECT * FROM merchants WHERE name=?", (data[random.choice(ids)]["name"],)).fetchall(), reads)
    scan = median_ms(lambda: conn.execute("SELECT count(*) FROM merchants WHERE country='IN' AND ledger_balance>50000").fetchall(), scans)
    conn.close()
    return dict(data_bytes=size_of(path), file_bytes=size_of(path), insert_individual_s=individual, insert_bulk_s=bulk, point_ms=point, field_ms=byfield, scan_ms=scan)


def bench_duckdb(tmp: Path, data: list[dict], reads: int, scans: int, individual_cap: int) -> dict:
    path = tmp / "bench.duckdb"
    conn = duckdb.connect(str(path))
    ddl = SQL_TABLE.replace("NUMERIC(14,2)", "DECIMAL(14,2)")
    conn.execute(ddl)
    sample = data[:individual_cap]  # one-row inserts are very slow in DuckDB; time a sample and scale
    t = time.perf_counter()
    for i, r in enumerate(sample):
        conn.execute(INSERT_Q, tup(i, r))
    individual = (time.perf_counter() - t) * (len(data) / len(sample))
    conn.execute("DELETE FROM merchants")
    t = time.perf_counter()
    conn.begin()
    conn.executemany(INSERT_Q, [tup(i, r) for i, r in enumerate(data)])
    conn.commit()
    bulk = time.perf_counter() - t
    conn.execute("CHECKPOINT")
    conn.close()
    conn = duckdb.connect(str(path))
    ids = [random.Random(1).randrange(len(data)) for _ in range(reads)]
    point = median_ms(lambda: conn.execute("SELECT * FROM merchants WHERE id=?", [random.choice(ids)]).fetchall(), reads)
    byfield = median_ms(lambda: conn.execute("SELECT * FROM merchants WHERE name=?", [data[random.choice(ids)]["name"]]).fetchall(), reads)
    scan = median_ms(lambda: conn.execute("SELECT count(*) FROM merchants WHERE country='IN' AND ledger_balance>50000").fetchall(), scans)
    conn.close()
    return dict(data_bytes=size_of(path), file_bytes=size_of(path), insert_individual_s=individual, insert_bulk_s=bulk, point_ms=point, field_ms=byfield, scan_ms=scan, individual_scaled_from=len(sample))


def bench_mdc(tmp: Path, data: list[dict], reads: int, scans: int) -> dict:
    n = len(data)
    ops = [CreateOperation(collection="merchants", data=r, record_id=f"M{i:07d}") for i, r in enumerate(data)]
    # one at a time: its own file, so the size below is of a clean bulk load, not of churn
    one = DuckDBStore(tmp / "one.duckdb")
    one.init_schema()
    slow = MDCDataEngine(one, load_default_registry())
    t = time.perf_counter()
    for op in ops:
        slow.create(op)
    insert = time.perf_counter() - t
    one.close()
    path = tmp / "mdc.duckdb"
    store = DuckDBStore(path)
    store.init_schema()
    engine = MDCDataEngine(store, load_default_registry())
    t = time.perf_counter()
    engine.create_many(ops)
    bulk = time.perf_counter() - t
    store.conn.execute("CHECKPOINT")
    size = size_of(path)
    ids = [f"M{random.Random(1).randrange(n):07d}" for _ in range(reads)]
    point = median_ms(lambda: engine.get_record("merchants", random.choice(ids)), reads)
    byfield = median_ms(lambda: engine.read(ReadOperation(collection="merchants", filters=[Filter(field="name", operator="=", value=data[random.randrange(n)]["name"])])), reads)
    scan = median_ms(
        lambda: engine.read(
            ReadOperation(collection="merchants", filters=[Filter(field="country", operator="=", value="IN"), Filter(field="ledger_balance", operator=">", value=50000)])
        ),
        scans,
    )
    store.close()
    return dict(data_bytes=size, file_bytes=size, insert_individual_s=insert, insert_bulk_s=bulk, point_ms=point, field_ms=byfield, scan_ms=scan)


def section_records(sizes: list[int], pg: Postgres | None, tmp: Path) -> tuple[list[str], dict]:
    out, raw = [], {}
    for n in sizes:
        data = rows(n)
        reads = 200 if n <= 1000 else 100
        mdc_reads, mdc_scans = reads, 20
        results = {}
        if pg:
            results["PostgreSQL (local)"] = bench_postgres(pg, data, reads, 20)
        d = tmp / f"sqlite{n}"
        d.mkdir()
        results["SQLite"] = bench_sqlite(d, data, reads, 20)
        d = tmp / f"duck{n}"
        d.mkdir()
        results["DuckDB (raw table)"] = bench_duckdb(d, data, reads, 20, individual_cap=min(n, 300))
        d = tmp / f"mdc{n}"
        d.mkdir()
        results["MDC Data Engine"] = bench_mdc(d, data, mdc_reads, mdc_scans)
        raw[n] = results
        base = len(json.dumps(data).encode())
        out.append(f"\n**{n:,} records** (the same rows as JSON text: {fmt_bytes(base)})\n")
        out.append("| Store | Data on disk | Insert, one at a time | Insert, one transaction | Find by id (median) | Find by a field value (median) | Filtered count (median) |")
        out.append("|---|---|---|---|---|---|---|")
        for name, r in results.items():
            bulk = f"{r['insert_bulk_s']:.2f} s" if r["insert_bulk_s"] is not None else "n/a"
            out.append(
                f"| {name} | {fmt_bytes(r['data_bytes'])} | {r['insert_individual_s']:.2f} s | {bulk} | {r['point_ms']:.2f} ms | {r['field_ms']:.2f} ms | {r['scan_ms']:.1f} ms |"
            )
    return out, raw


# ---------------------------------------------------------------- B. small values


def find_lite_lib() -> Path | None:
    for rel in ("mdc-lite/dist/macos-arm64/libmdc_lite.dylib", "mdc-lite/target/release/libmdc_lite.dylib", "mdc-lite/target/release/libmdc_lite.so"):
        p = ROOT.parent / rel
        if p.exists():
            return p
    return None


def section_values(n: int, tmp: Path) -> tuple[list[str], dict]:
    rnd = random.Random(SEED)
    values = [json.dumps({"id": i, "theme": rnd.choice(["dark", "light"]), "token": "".join(rnd.choices("abcdef0123456789", k=48)), "n": rnd.random()}).encode() for i in range(n)]
    avg = sum(map(len, values)) / n
    results = {}

    path = tmp / "kv.sqlite"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v BLOB)")
    t = time.perf_counter()
    for i, v in enumerate(values):
        conn.execute("INSERT INTO kv VALUES (?,?)", (f"item/{i}", v))
    put = time.perf_counter() - t
    keys = [f"item/{rnd.randrange(n)}" for _ in range(2000)]
    t = time.perf_counter()
    for k in keys:
        conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    get = time.perf_counter() - t
    conn.close()
    results["SQLite key-value table (not encrypted)"] = (size_of(path), size_of(path), put / n * 1e6, get / len(keys) * 1e6)

    d = tmp / "files"
    d.mkdir()
    t = time.perf_counter()
    for i, v in enumerate(values):
        (d / f"{i}.json").write_bytes(v)
    put = time.perf_counter() - t
    t = time.perf_counter()
    for k in keys:
        (d / f"{k.split('/')[1]}.json").read_bytes()
    get = time.perf_counter() - t
    app, alloc = dir_allocated(d)
    results["One plain file per key (not encrypted)"] = (app, alloc, put / n * 1e6, get / len(keys) * 1e6)

    lib_path = find_lite_lib()
    if lib_path is None:
        results["mdc-lite"] = None
    else:
        lib = ctypes.CDLL(str(lib_path))
        lib.mdc_lite_open.restype = ctypes.c_void_p
        lib.mdc_lite_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.mdc_lite_put.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
        lib.mdc_lite_get.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)), ctypes.POINTER(ctypes.c_size_t)]
        lib.mdc_lite_free_buffer.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
        lib.mdc_lite_close.argtypes = [ctypes.c_void_p]
        d = tmp / "lite"
        d.mkdir()
        store = lib.mdc_lite_open(str(d).encode(), bytes(range(32)))
        t = time.perf_counter()
        for i, v in enumerate(values):
            assert lib.mdc_lite_put(store, f"item/{i}".encode(), v, len(v)) == 0
        put = time.perf_counter() - t
        t = time.perf_counter()
        for k in keys:
            out, ln = ctypes.POINTER(ctypes.c_uint8)(), ctypes.c_size_t()
            assert lib.mdc_lite_get(store, k.encode(), ctypes.byref(out), ctypes.byref(ln)) == 0
            lib.mdc_lite_free_buffer(out, ln)
        get = time.perf_counter() - t
        lib.mdc_lite_close(store)
        app, alloc = dir_allocated(d)
        results["mdc-lite (encrypted, DNA-encoded)"] = (app, alloc, put / n * 1e6, get / len(keys) * 1e6)

    out = [f"\n**{n:,} entries, about {avg:.0f} bytes each ({fmt_bytes(sum(map(len, values)))} of values)**\n"]
    out.append("| Store | Size (apparent) | Size (allocated on disk) | Put (average) | Get (average) |")
    out.append("|---|---|---|---|---|")
    for name, r in results.items():
        if r is None:
            out.append(f"| {name} | not run: no library build found for this machine | | | |")
        else:
            out.append(f"| {name} | {fmt_bytes(r[0])} | {fmt_bytes(r[1])} | {r[2]:.0f} µs | {r[3]:.0f} µs |")
    return out, {k: v for k, v in results.items()}


# ---------------------------------------------------------------- C. sealed data


def section_sealed(tmp: Path) -> tuple[list[str], dict]:
    from mdc.dna.storage import DNAStorageBackend
    from mdc.events import EventStore

    out = []
    # event streams
    path = tmp / "ev.duckdb"
    store = DuckDBStore(path)
    store.init_schema()
    ev = EventStore(store, b"\x07" * 32, snapshot_every=1_000_000)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state = {"mood": "calm", "goal": "idle", "level": 1, "memory": {"last_tool": "none"}}
    ev.create_stream("agent-1", state, ts=t0)
    plain = len(json.dumps(state).encode())
    n_events = 1000
    t = time.perf_counter()
    for i in range(1, n_events + 1):
        state = {**state, "level": i + 1, "mood": ("busy" if i % 2 else "calm")}
        e = ev.append("agent-1", state=state, ts=t0 + timedelta(minutes=i))
        plain += len(json.dumps({"ts": e.ts.isoformat(), "payload": e.payload}).encode())
    append_ms = (time.perf_counter() - t) / n_events * 1000
    stored = sum(len(store.get(b)) for b in store.search(stream="agent-1"))
    read = median_ms(lambda: ev.state_at("agent-1"), 20)
    asof = median_ms(lambda: ev.state_at("agent-1", seq=500), 20)
    store.close()
    out.append(f"\n**Event stream: base plus {n_events:,} events**\n")
    out.append("| What | Plain JSON | Sealed in MDC | Ratio |")
    out.append("|---|---|---|---|")
    out.append(f"| Bytes stored | {fmt_bytes(plain)} | {fmt_bytes(stored)} | {stored / plain:.2f}x |")
    out.append(f"\nAppend: {append_ms:.2f} ms each. Rebuild latest state: {read:.1f} ms. Rebuild as of event 500: {asof:.1f} ms (this stream has no snapshots, so a rebuild replays the log).")

    # archive tier
    store = DuckDBStore(tmp / "arc.duckdb")
    store.init_schema()
    rnd = random.Random(SEED)
    arc = DNAStorageBackend(encryption_key=b"\x09" * 32, persist=store)
    out.append("\n**ARCHIVE tier: bytes stored in the database file per byte archived**\n")
    out.append("| Payload | Archived | Stored | Ratio | Put | Get |")
    out.append("|---|---|---|---|---|---|")
    arc_res = {}
    for label, payload in (("random bytes (incompressible)", rnd.randbytes(256 * 1024)), ("text (compressible)", (b"The quick brown fox jumps over the lazy dog. " * 6000)[: 256 * 1024])):
        bid = f"b-{label[:4]}"
        t = time.perf_counter()
        arc.put(bid, payload)
        put = (time.perf_counter() - t) * 1000
        stored = len(store.get("dna:" + bid))
        t = time.perf_counter()
        assert arc.get(bid) == payload
        get = (time.perf_counter() - t) * 1000
        out.append(f"| {label} | {fmt_bytes(len(payload))} | {fmt_bytes(stored)} | {stored / len(payload):.2f}x | {put:.0f} ms | {get:.0f} ms |")
        arc_res[label] = stored / len(payload)
    store.close()
    return out, dict(append_ms=append_ms, archive_ratio=arc_res)


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000])
    ap.add_argument("--entries", type=int, default=10000, help="entries for the small-value comparison")
    ap.add_argument("--out", type=Path, default=None, help="also write the report to this file")
    ap.add_argument("--no-postgres", action="store_true")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="mdc-bench-"))
    pg = None
    lines = [
        "# MDC storage and speed comparison",
        "",
        f"Run on: {platform.platform()}, {platform.machine()}, Python {platform.python_version()}, DuckDB {duckdb.__version__}, SQLite {sqlite3.sqlite_version}",
        f"Date: {datetime.now(timezone.utc).date()}",
    ]
    try:
        if not args.no_postgres:
            try:
                pg = Postgres(tmp)
                lines.append(f"PostgreSQL: {pg.version}, a throwaway local cluster with default settings, reached over a Unix socket")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"PostgreSQL: not run ({exc})")
        lines += ["", "## A. Records: MDC Data Engine and ordinary databases"]
        rec, _ = section_records(args.sizes, pg, tmp)
        lines += rec
        lines += ["", "## B. Small values: mdc-lite and ordinary stores"]
        val, _ = section_values(args.entries, tmp)
        lines += val
        lines += ["", "## C. Sealed data inside MDC Platform"]
        sealed, _ = section_sealed(tmp)
        lines += sealed
    finally:
        if pg:
            pg.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    report = "\n".join(lines) + "\n"
    print(report)
    if args.out:
        args.out.write_text(report)


if __name__ == "__main__":
    main()
