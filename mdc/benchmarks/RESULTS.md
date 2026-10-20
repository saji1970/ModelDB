# MDC storage and speed comparison

Run on: macOS-26.6-arm64-arm-64bit-Mach-O, arm64, Python 3.14.0, DuckDB 1.5.6, SQLite 3.53.4
Date: 2026-10-20
PostgreSQL: postgres (PostgreSQL) 14.18 (Homebrew), a throwaway local cluster with default settings, reached over a Unix socket

## A. Records: MDC Data Engine and ordinary databases

**1,000 records** (the same rows as JSON text: 162.5 KB)

| Store | Data on disk | Insert, one at a time | Insert, one transaction | Find by id (median) | Find by a field value (median) | Filtered count (median) |
|---|---|---|---|---|---|---|
| PostgreSQL (local) | 176.0 KB | 0.03 s | 0.02 s | 0.02 ms | 0.04 ms | 0.1 ms |
| SQLite | 76.0 KB | 0.18 s | 0.00 s | 0.00 ms | 0.02 ms | 0.0 ms |
| DuckDB (raw table) | 780.0 KB | 0.43 s | 0.33 s | 0.11 ms | 0.15 ms | 0.1 ms |
| MDC Data Engine | 1.0 MB | 0.44 s | 0.05 s | 0.19 ms | 0.39 ms | 0.7 ms |

**10,000 records** (the same rows as JSON text: 1.6 MB)

| Store | Data on disk | Insert, one at a time | Insert, one transaction | Find by id (median) | Find by a field value (median) | Filtered count (median) |
|---|---|---|---|---|---|---|
| PostgreSQL (local) | 1.2 MB | 0.33 s | 0.15 s | 0.02 ms | 0.29 ms | 0.5 ms |
| SQLite | 676.0 KB | 1.60 s | 0.01 s | 0.00 ms | 0.16 ms | 0.2 ms |
| DuckDB (raw table) | 780.0 KB | 4.26 s | 3.35 s | 0.11 ms | 0.20 ms | 0.1 ms |
| MDC Data Engine | 2.0 MB | 4.39 s | 0.47 s | 0.33 ms | 1.34 ms | 3.5 ms |

**100,000 records** (the same rows as JSON text: 15.9 MB)

| Store | Data on disk | Insert, one at a time | Insert, one transaction | Find by id (median) | Find by a field value (median) | Filtered count (median) |
|---|---|---|---|---|---|---|
| PostgreSQL (local) | 11.6 MB | 3.25 s | 1.48 s | 0.02 ms | 2.49 ms | 4.7 ms |
| SQLite | 6.6 MB | 16.98 s | 0.06 s | 0.00 ms | 2.27 ms | 3.0 ms |
| DuckDB (raw table) | 3.0 MB | 43.59 s | 35.13 s | 0.12 ms | 0.93 ms | 0.2 ms |
| MDC Data Engine | 12.5 MB | 44.06 s | 5.04 s | 1.04 ms | 9.76 ms | 30.9 ms |

## B. Small values: mdc-lite and ordinary stores

**10,000 entries, about 116 bytes each (1.1 MB of values)**

| Store | Size (apparent) | Size (allocated on disk) | Put (average) | Get (average) |
|---|---|---|---|---|
| SQLite key-value table (not encrypted) | 1.5 MB | 1.5 MB | 197 µs | 4 µs |
| One plain file per key (not encrypted) | 1.1 MB | 39.1 MB | 57 µs | 13 µs |
| mdc-lite (encrypted, DNA-encoded) | 6.4 MB | 39.1 MB | 110 µs | 17 µs |

## C. Sealed data inside MDC Platform

**Event stream: base plus 1,000 events**

| What | Plain JSON | Sealed in MDC | Ratio |
|---|---|---|---|
| Bytes stored | 142.6 KB | 168.0 KB | 1.18x |

Append: 0.90 ms each. Rebuild latest state: 100.4 ms. Rebuild as of event 500: 50.5 ms (this stream has no snapshots, so a rebuild replays the log).

**ARCHIVE tier: bytes stored in the database file per byte archived**

| Payload | Archived | Stored | Ratio | Put | Get |
|---|---|---|---|---|---|
| random bytes (incompressible) | 256.0 KB | 256.0 KB | 1.00x | 3 ms | 0 ms |
| text (compressible) | 256.0 KB | 256.0 KB | 1.00x | 3 ms | 0 ms |
