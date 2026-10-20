# Benchmarks

`compare_stores.py` measures how MDC compares with ordinary databases: size on
disk and speed. Run it on your own machine; the numbers depend on it.

```bash
pip install psycopg2-binary        # optional: adds PostgreSQL to the comparison
python benchmarks/compare_stores.py                      # prints the report
python benchmarks/compare_stores.py --sizes 1000 10000 100000 --out RESULTS.md
```

[RESULTS.md](RESULTS.md) is the output of one run (an Apple M5 laptop, macOS,
local SSD, 2026-10-20). Read it with these limits in mind:

- PostgreSQL is a throwaway local cluster with default settings, reached over a
  Unix socket. It is the engine behind Amazon RDS, but there is **no network**
  between client and server here, and no tuning. A real RDS instance adds a
  network round trip to every call.
- SQLite and DuckDB are default-configured too. DuckDB is the engine MDC
  stores its data in; its raw-table file size is a fixed minimum at these small
  sizes (it allocates in large blocks), so compare it only at larger sizes.
- DuckDB "one at a time" inserts are timed on a 300-row sample and scaled.
- "MDC Data Engine" is the Python engine called directly, not through the REST
  API, which adds an HTTP call per operation.
- mdc-lite is run through its shipped macOS arm64 library (`mdc-lite/dist`).
- One run, one machine. No cloud provider, no concurrency, no network, and no
  workload larger than 10,000 records or entries.

What the results say, plainly: MDC stores the same records in more space than
the ordinary databases, inserts them more slowly, and its reads scan the whole
collection (no indexes), so lookups slow down in proportion to the number of
records. See the documentation page for what that means in practice.
