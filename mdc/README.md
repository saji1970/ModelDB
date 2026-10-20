# Molecular Data Center (MDC)

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

Version 1.0 - research prototype. Full build specification: [CLAUDE.md](CLAUDE.md).

## What it is

MDC is a research prototype for a data-storage and data-access architecture
inspired by DNA's information density. It is **not** primarily a
natural-language-to-SQL tool - that's one access mechanism sitting above the
real product, the **MDC Data Engine**: a storage-agnostic CRUD engine that a
CLI, natural language, and eventually a REST API/SDK all drive identically.

```
Chat / CLI / Storage Explorer / REST API (and your own NLU or UI)
        |
   MDC Data Engine  --  Schema Registry  --  Event streams
        |
   Storage Router --  HOT (memory) / WARM / COLD (DuckDB) / ARCHIVE (encrypted, DNA-encoded, persisted)
```

Natural language is an optional path into the same engine, never a shortcut
around it (CLAUDE.md section 3):

> The LLM proposes meaning. Deterministic software establishes executable meaning.

An LLM classifies intent and extracts candidate entities/fields/values, but a
deterministic pipeline (ontology resolution -> ambiguity check -> a
validated `MDCOperation`) is what actually reaches the Data Engine. Ambiguous
input (e.g. "balance" when the schema has three different balance fields) is
refused and clarified rather than guessed, and a destructive DELETE is never
executed without an explicit confirmation. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full data-flow diagram
and an important scoping note on the two datasets this prototype carries.

## Features at a glance

| Area | What you get | Details |
|---|---|---|
| Conversational data | Create, read, update, delete and analyse in plain sentences; ambiguity is clarified, deletes are confirmed | [CRUD](#crud-through-conversation), [Analytics](#analytics-queries) |
| Object store | Classifies any bytes (AI models, images, documents, logs, tensors, tabular) by content and tiers them HOT / WARM / COLD / ARCHIVE | [Polymorphic storage](#the-polymorphic-storage-engine) |
| AI models | `.safetensors` parsed; each tensor stored and tiered on its own; retrieve one layer without touching the rest | [Polymorphic storage](#the-polymorphic-storage-engine) |
| DNA-inspired archive | ARCHIVE tier: AES-256-GCM first, then binary to A/C/G/T, with error correction and a corruption simulator (a simulation - no physical DNA) | [Storage tiers](https://saji1970.github.io/ModelDB/platform.html#tiers) |
| Databases and tables | Unlimited isolated databases, typed tables, insert and browse, universal `find` across all of them | [Multiple databases](#multiple-databases), [Universal search](#universal-search) |
| Import and export | In: `.md` design doc, `.csv`, `.sql` (parsed, never run), Mermaid ER diagram, MDC export. Out: CSV, SQL, Excel, design doc + ER diagram, MDC export | [Import, export and design](#import-export-and-schema-design) |
| Schema from a prompt | `create database shop with customers(name, email)` or a pasted ER diagram: previewed, created on `yes` | [Import, export and design](#import-export-and-schema-design) |
| Agent state streams | Event sourcing: a base state plus timestamped changes, rebuilt as of any time, sealed per stream | [Agent state streams](#agent-state-streams-event-sourcing) |
| Deleting | Drop a database or table, delete a row or stream; shows what is lost, asks first | [Deleting things](#deleting-things) |
| Access control | Bearer-token REST API, four roles (viewer, editor, db_admin, admin), user accounts | [Security](#security-and-access-control) |
| Storage Explorer | Browser UI: browse by type, tier or database; inspect, upload, move, delete; chat panel with import and download | [Storage Explorer](#storage-explorer-browser-ui) |
| Deployment | Dockerfile + Railway config, health check, persistent volume | [Deployment](#deployment-docker-and-railway) |

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## CLI usage

```bash
python -m mdc db init              # schema + full spec-minimum seed data
python -m mdc db init --scale small  # smaller/faster dataset for dev
python -m mdc                       # start the conversational shell
python -m mdc serve                 # REST API + Storage Explorer (--host, --port, --database)
python -m mdc token issue <name> [--role viewer|editor|db_admin|admin]   # an API token
python -m mdc token list | revoke <name>
python -m mdc key generate | show | rotate --database <path> (--generate | --new-key-env VAR | --new-key-file F)   # the sealing key
python -m mdc user create <name> [--role ...] | list | set-role <name> <role> | delete <name>
```

Running `python -m mdc` prints the banner and drops you into the `mdc>`
prompt. If `data/mdc.duckdb` doesn't exist yet it is auto-created and
seeded at `small` scale so startup stays fast; run `mdc db init`
explicitly to get the full CLAUDE.md section 56 dataset (10,000
merchants / 50,000 customers / 100,000 accounts / 500,000 transactions /
100,000 payments / 100,000 settlements).

## CRUD through conversation

```
mdc> Create a merchant called ABC Store in India
Created MER-D7B45F6194 (name=ABC Store, country=IN)

mdc> Show me ABC Store
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━┓
┃ record_id      ┃ country ┃ name      ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━┩
│ MER-D7B45F6194 │ IN      │ ABC Store │
└────────────────┴─────────┴───────────┘
1 row(s)

mdc> Change ABC Store balance to 15000
I need to clarify what you mean by "balance".
  1. Ledger balance
  2. Available balance
  3. Settlement balance
mdc> settlement balance
Updated MER-D7B45F6194 (name=ABC Store, country=IN, settlement_balance=15000.0)

mdc> Delete merchant ABC Store
Delete merchant 'ABC Store'? (yes/no)
mdc> yes
Deleted 1 record(s).
```

Every one of these lines becomes an `MDCOperation` (`CreateOperation` /
`ReadOperation` / `UpdateOperation` / `DeleteOperation`) executed by
`MDCDataEngine` against the `merchants` collection - the same engine a
future REST API or SDK would call, never a string of SQL generated by the
LLM. Records are schema-validated (`schema/collections.yaml`) before being
written.

## Analytics queries

The original conversational query interface, over the larger synthetic
payments dataset, still works the same way - including multi-turn
follow-ups and typo correction:

```
mdc> Show merchants with balance above 5000
I need to clarify what you mean by "balance".
  1. Ledger balance
  2. Available balance
  3. Settlement balance
mdc> settlement balance
Intent: FETCH    Entity: merchant
Fields: settlement_balance
  WHERE settlement_balance > 5000.0
mdc> Only India
Intent: FETCH    Entity: merchant
Fields: settlement_balance
  WHERE settlement_balance > 5000.0
  WHERE country = IN
mdc> Sort highest first
...
  ORDER BY settlement_balance DESC
```

This path resolves down to a `QueryContext`, not yet to SQL execution
against the payments dataset - see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
for why analytics reads and CRUD currently target two different data
surfaces.

Slash commands available today: `/help`, `/context`, `/history`, `/reset`,
`/exit`. `/explain`, `/mdql`, `/sql`, `/debug` are wired up but report
"not implemented yet".

## The polymorphic storage engine

Above the merchants-CRUD engine, MDC also has a second, broader system: a
storage-agnostic pipeline that classifies *any* object (models, images,
documents, matrices, tabular files - see [CLAUDE.md](CLAUDE.md)), profiles
it, decides a storage strategy, and routes it to a tiered backend:

```
bytes -> classify (DataType) -> profile (entropy, structure)
      -> StorageStrategyEngine (representation/compression/tier)
      -> StorageRouter -> HOT (memory) / WARM,COLD (DuckDB) / ARCHIVE (real DNA encoding, saved in the DuckDB file)
```

- **Classification & profiling** (`classification/`): real content-signature
  detection (magic bytes, safetensors/npy header parsing), not filename
  guessing.
- **Storage Intelligence** (`storage_intelligence/`): `StorageStrategyEngine`
  picks representation/compression/chunking/tier per object from its actual
  profile - two objects of the same type can land on different tiers.
- **Specialized stores**: `models/` (tensor-block model storage - retrieving
  one tensor never loads the whole model), `objects/` (image dimension
  parsing, document text extraction + chunking), `matrix/` (real block
  splitting and a genuine truncated-SVD low-rank approximation), `dna/`
  (binary<->DNA base encoding, a corruption simulator, and a working
  repetition-code ECC).
- **`StorageBackend`** (`put`/`get`/`exists`/`delete`/`metadata`/`search`)
  is implemented by `DuckDBStore`, `MemoryStorageBackend`, and
  `DNAStorageBackend` - the router picks between them per tier without any
  caller needing to know which one is underneath.
- **REST API** (`api/`): `POST/GET/PUT/DELETE /objects`, tier `move`,
  `optimize`, an explainability endpoint, and tensor-level model access -
  see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full picture.

## Storage Explorer (browser UI)

A Windows-Explorer-style app for browsing everything stored in MDC, with a
chat panel wired to the same conversational layer as the CLI:

```bash
python -m mdc serve
```

Then open http://127.0.0.1:8000.

- **Browse** objects by type or storage tier, and databases and their tables (click a table to see its rows).
- **Inspect** an object's metadata and the explanation of why it is in its tier; **preview** its content, **archive** it, or **delete** it (the browser asks first); a table's details offer **Browse data**. Any other tier move is `move <id> to hot` in the chat. **Upload** adds any file.
- **Ask MDC** (the chat panel) takes everything in the reference below: `list images`, `show <object_id>`, `archive <object_id>`, `search for <text>`, `create database ...`, `find ...`, `drop table ...`. Tables come back as tables.
- **Paperclip / drag and drop** a `.md`, `.csv`, `.sql`, `.mmd` or MDC export file onto the chat to import it; an `export ...` command answers with a download button.
- **Say `cancel`** (or `never mind`, `/reset`) to drop whatever the chat is waiting on.

There is no login screen. `mdc serve` hands its own page a per-process admin token, so it
works without setup on your machine; the deployed container asks you to paste the API token
instead (see [Security](#security-and-access-control) and [Deployment](#deployment-docker-and-railway)).

## Conversational storage (Phase J)

The `mdc>` shell also understands natural language for the polymorphic
object system - a real intent-classified layer (`nlp/`, `conversation/`),
separate from the merchants-CRUD conversation above on purpose (the two
domains use the same surface words - "show", "find" - for different
things), with its own conversational state including pronoun resolution:

```
mdc> store ./report.md
Stored "report.md" as DOC-30A3DC793C (DOCUMENT).

mdc> archive it
Archived "DOC-30A3DC793C".

mdc> describe it
ARCHIVE (DNA-encoded): explicit archival request on an immutable,
unaccessed object. Compressed with ZLIB: the content measured as
compressible.

mdc> delete it
Delete "DOC-30A3DC793C"? (yes/no)
mdc> yes
Deleted "DOC-30A3DC793C".
```

`"as compact as possible"`/`"optimize"`/`"compress"` phrasings only ever
*report* a requested preference (`MAX_COMPRESSION`/`FAST_ACCESS`) alongside
whatever the engine actually decided - never override it - e.g. `mdc>
make it as compact as possible` on an object already at its
entropy-appropriate tier answers "No change needed... (requested: max
compression - the storage engine decides the actual representation/
compression from the object's measured content, not from the request
directly)".

Unrecognized text falls through to the merchants-CRUD interpreter (above),
so both conversations coexist in the same shell without cross-talk - a
storage command is only ever recognized when its object reference is
either a generated id (`AIM-XXXXXXXXXX`) or a resolvable pronoun ("it",
"this model"), never free text like "the old model," which stays
unresolvable rather than guessed. The `optimize`/`compress`/`"as ... as
possible"` phrasings only ever *report* a requested preference
(`MAX_COMPRESSION`/`FAST_ACCESS`) - the actual representation/compression
decision always comes from `StorageStrategyEngine`, never from the NLP
layer directly. The same engine (`conversation/interpreter.py`) backs
both the CLI and the Explorer's chat panel - one implementation, two
transports, per `read_file` being the only thing that differs (the CLI
can read local files for `store <path>`; the browser-facing chat cannot,
by design - see the module's docstring).

## Multiple databases

MDC is not limited to one database. `DatabaseManager` (`databases/manager.py`)
creates and caches any number of independently-stored named databases on
demand - each gets its own DuckDB file, schema registry, and object index,
fully isolated from every other one. There is no hardcoded limit on how many
you can have.

You can create a database, add tables (schema-registry collections, never raw
SQL), and browse/insert data entirely from the chat prompt or the `mdc>`
shell - a third NLP layer (`nlp/db_command.py`, `conversation/
db_interpreter.py`) kept deliberately separate from both the merchants-CRUD
and object-storage vocabularies above, so "table"/"database" keywords never
collide with "show all merchants" or "archive it":

```
mdc> create database mytest
Created database "mytest" and switched to it. It starts empty - use "create table ..." to add one.

mdc[mytest]> create table products with sku string, name string, price decimal
Created table "products" in "mytest" with fields: sku (string), name (string), price (decimal).

mdc[mytest]> insert into products sku=ABC123, name=Widget, price=9.99
Inserted <record_id> into "products".

mdc[mytest]> show data in products
1 row(s) in "products".

mdc[mytest]> describe database
1 table(s) in "mytest": products.

mdc[mytest]> describe table products
3 field(s).

mdc[mytest]> list databases
2 database(s): default, mytest. Current: mytest.

mdc[mytest]> use database default
Switched to database "default".
```

The same commands work through the Storage Explorer's chat panel
(`POST /chat`) and the REST API (`GET /databases`, `POST /databases`); each
chat session tracks its own `current_database` independently, so switching
databases in one browser tab never affects another session. Switching
databases resets that session's object pronoun ("it") and any pending delete
confirmation, since they'd otherwise silently resolve against the wrong
database. Merchants CRUD/analytics natural language ("Show all merchants",
"Create a merchant called...") intentionally always stays bound to the
original `default` database regardless of which database the session has
switched to - see `conversation/interpreter.py`'s module docstring for why.

## Universal search

`find <text> [under|over <amount>] [in table <t>] [in database <d>]` searches every database's
tables and document text at once, with no need to know where something lives:

```
mdc> find widget under 20000
1 row(s) matched "widget".
  database   table     record_id       sku     name    price
  inventory  products  PRO-A497AE9E55  ABC123  Widget  9.99
```

REST: `GET /find?q=&min=&max=&database=&table=`.

## Import, export and schema design

In the chat window (and over REST), nothing is written unless the whole request is valid.

**Import** - the paperclip, or `POST /databases/import?filename=...` (raw body; the extension picks the format):

| File | Becomes |
|---|---|
| `.md` design document | a database with the tables the document defines (a `## Table: name` heading followed by a Field / Type / Required table) |
| `.mmd`, or `.md` containing a `mermaid` `erDiagram` block | a database built from the ER diagram; a relationship becomes a `<parent>_id` field on the "many" side, many-to-many is flagged |
| `.csv` / `.tsv` | one table, named after the file, in the current database; types are inferred (`01234` stays text); a `record_id` column is kept as the row identity |
| `.sql` script | its `CREATE TABLE` and `INSERT ... VALUES` statements, **parsed and never executed**; `DROP`, `DELETE`, `UPDATE`, `ALTER` and the rest are skipped and listed |
| `.json` / `.dnadb.json` | an MDC export file from another instance, `record_id`s preserved |

An import never overwrites: an existing table or record id stops the whole import. The caller's role decides what it may do (`create_database`, `create_table`, `write`). Files over 25 MB are refused.

**Export** - `export database as csv|sql|excel|markdown`, `export schema as er diagram`, `export database with data`, `export table <t> as csv`, or `GET /databases/{name}/export?format=csv|sql|xlsx|md|er|dnadb&scope=schema|data|all&table=`:

| Format | Contains |
|---|---|
| CSV | data only: a zip of one file per table, or one table's file |
| SQL | `CREATE TABLE` + `INSERT` in standard SQL (or only one of them) |
| Excel (`.xlsx`) | a sheet per table plus a `_schema` sheet |
| Markdown design document | structure only: a Mermaid ER diagram and a table per entity - it imports straight back |
| ER diagram (`.mmd`) | the Mermaid source alone |
| MDC export file | structure, data, or both, for importing into another instance |

Cells that look like spreadsheet formulas (`=`, `+`, `-`, `@`) are neutralised in CSV and Excel, and restored on CSV import. **An export is a plain, readable file**: the sealing under [Security](#security-and-access-control) protects data stored inside MDC, not what you download.

**Schema from a prompt or an ER diagram** - say it, or paste an `erDiagram`, and MDC shows what it understood and creates nothing until you reply `yes`:

```
> create database shop with customers(name, email, joined), orders(customer_id, total decimal, paid)
Here is the design - new database "shop": ...
Create it? Reply yes to create it - anything else drops it.
```

This is a deterministic grammar, not a language model: an untyped field is a string unless its name suggests otherwise (`total` / `price` decimal, `age` / `count` integer, `is_...` / `active` boolean, `..._at` / `joined` datetime), and each guess is listed. Free-form English ("I need a database for a school") is not interpreted; `portability.design.SchemaDesigner` is the extension point for a model, with the same validate-and-confirm path.

## Agent state streams (event sourcing)

A normal table keeps only the latest value. For an AI agent that loses the
history: what it believed yesterday, or its state when it made a decision. A
**stream** keeps the state as a **base** (event 0) plus an append-only log of
timestamped **events**; the state is never stored directly but **replayed** from
those, deterministically, up to any time or event number (**as of**). A **codec**
defines how a change is written and applied, so the model decides what a state
and a change are; **sealing** (below) makes the stored bytes unreadable without
the key.

Model *weights* (the `.safetensors` checkpoint) stay in the object store as
tensor blocks and are not event-sourced; streams hold the data a model or agent
produces and changes (working state, memory, decisions).

```
base (event 0)   mood=calm goal=idle level=1        10:00
event 1          mood=busy goal="write report"      11:00
state as of 10:30 -> mood=calm goal=idle level=1
```

In chat:

```
> create stream agent-1 with mood=calm, goal=idle
> update stream agent-1 set mood=busy
> show stream agent-1 as of 15 minutes ago
> show history of stream agent-1
```

REST: `POST /databases/{name}/streams/{stream}` (create), `.../events` (append),
`GET .../streams/{stream}?at=<iso>|seq=<n>` (rebuild), `.../history`, list, delete.
From Python: `DatabaseManager.events(name)` returns an `EventStore`.

Every record, including the event times inside it, is sealed with AES-256-GCM
under a per-stream key derived from the server's master key (`MDC_DNA_KEY` /
`~/.mdc/dna.key`), and bound to its stream and position - so a copied, swapped,
altered or deleted event is detected on read. What stays visible to someone with
the database file: the stream id, record counts and sizes, and when each record
was written. The API is token-gated by role, not per stream. How a change is
encoded is a pluggable `StateCodec` (the built-in writes `set`/`unset` path
operations); replay is always deterministic code. See `src/mdc/events/store.py`.

## Deleting things

`drop database <name>` (type the name to confirm), `drop table <name>`,
`delete row <record_id> from table <t>`, `delete stream <id>`: each shows what
would be lost and waits; anything but the confirmation cancels. REST:
`DELETE /databases/{name}?confirm={name}`, `DELETE .../tables/{table}?confirm={table}`,
`DELETE .../rows/{record_id}`. Dropping needs the `drop` permission (`db_admin`,
`admin`); deleting a row or object is ordinary `write`. The `default` database and
its `merchants` table are protected. Deletions are permanent.

## Security and access control

- **Every REST route needs a bearer token** (`Authorization: Bearer <token>`), except `GET /`, which serves the Explorer page. Issue one with `mdc token issue <name> [--role ...]` (shown once; only a hash is stored in `~/.mdc/api_tokens.json`, mode 0600), or set `MDC_API_TOKENS` (comma-separated) for fixed tokens. CORS is open on purpose so a third-party NLU or UI can call the API; the token is the access control.
- **Four roles**, one vocabulary for tokens and accounts:

| Role | Can do |
|---|---|
| `viewer` | read and query: every `GET`, `/find`, exports |
| `editor` | + create tables, insert/update/delete rows, upload/replace/delete/move objects, chat |
| `db_admin` | + create databases, and `drop`: permanently delete a table, a database or a stream |
| `admin` | + manage user accounts |

  A valid token without the right role gets `403`; a missing or bad one gets `401`.
- **User accounts** (`mdc user create|list|set-role|delete`; passwords are scrypt-hashed, in `~/.mdc/users.json`). The first time `mdc` or `mdc serve` runs in a terminal with no account, it asks you to create an admin. **Accounts are not a login yet:** the shell and the Explorer do not ask you to sign in, so who can do what is decided by the token's role.
- **Nothing is executed as SQL.** Natural language, imported `.sql` scripts and table creation all go through the schema registry and the Data Engine.
- **Sealed at rest:** the ARCHIVE tier and event streams are encrypted with AES-256-GCM (the streams under a per-stream derived key). The key is, first match wins: `MDC_DNA_KEY` (64 hex characters), the file named by `MDC_DNA_KEY_FILE`, or `~/.mdc/dna.key` (mode 0600, made on first use). It lives on the server: anyone with the key and the files can read them, and losing it makes them unreadable. See [Encryption keys](#encryption-keys).

## REST API

All routes need a token (see above); the permission column is what the token's role must include.

| Method and path | Does | Needs |
|---|---|---|
| `POST /objects` | upload any file; classified and tiered automatically | write |
| `GET /objects`, `GET /objects/{id}`, `POST /objects/{id}/read`, `GET /objects/{id}/strategy` | list (`?type=&tier=`), metadata, content, why it is in its tier | read |
| `PUT /objects/{id}`, `POST /objects/{id}/move`, `POST /objects/{id}/optimize`, `DELETE /objects/{id}` | replace, force a tier, re-evaluate, delete | write |
| `GET /models/{id}`, `GET /models/{id}/tensors/{name}` | a model's manifest; one tensor's bytes | read |
| `GET /databases`, `POST /databases` | list; create | read; create_database |
| `DELETE /databases/{name}?confirm={name}` | drop a database and everything in it | drop |
| `GET /databases/{name}/tables`, `.../tables/{table}`, `.../tables/{table}/rows` | tables, a table's fields, its rows | read |
| `POST /databases/{name}/tables/{table}` | create a table from structured fields | create_table |
| `POST .../tables/{table}/rows`, `DELETE .../rows/{record_id}` | insert a row; delete one | write |
| `DELETE .../tables/{table}?confirm={table}` | drop a table and its rows | drop |
| `GET /databases/{name}/export` | download (csv, sql, xlsx, md, er, dnadb) | read |
| `POST /databases/import` | create a database / tables from an uploaded file | role-dependent: create_database, create_table, write |
| `GET /databases/{name}/streams`, `.../streams/{s}`, `.../streams/{s}/history` | list; rebuild (`?at=` or `?seq=`); history | read |
| `POST .../streams/{s}`, `POST .../streams/{s}/events` | create with a base state; append a change | write |
| `DELETE .../streams/{s}` | delete a stream's history | drop |
| `GET /find` | universal search | read |
| `POST /chat`, `POST /chat/attach` | the conversational layer; the paperclip's import | write; role-dependent |

## Encryption keys

One 32-byte master key seals event streams (a key derived per stream, HKDF-SHA256) and the ARCHIVE tier. Set it up:

```bash
python -m mdc key generate                      # 1. a new key (64 hex characters; not saved)
export MDC_DNA_KEY=<the key>                    # 2. give it to the server (or MDC_DNA_KEY_FILE=/path/to/file)
python -m mdc key show                          # 3. which key is in use: source + fingerprint, never the key
python -m mdc serve                             #    the same line is printed at startup
```

Back the key up apart from the data: lose it and sealed data is gone. The default key file sits on the data volume, so use `MDC_DNA_KEY` or `MDC_DNA_KEY_FILE` (a mounted secret, another disk) to keep them apart.

**Rotate** (server stopped, data backed up; the current key still configured):

```bash
python -m mdc key rotate --database data/mdc.duckdb --generate        # or --new-key-env VAR / --new-key-file F
```

Every stream and every archived object in every database is re-sealed in memory then written back; a record that does not authenticate stops it before anything changes, and an interrupted run is finished by running the same command again. A generated key is saved (file) or printed (environment) *before* any record changes. Then switch `MDC_DNA_KEY` / `MDC_DNA_KEY_FILE` to the new key. The old key then fails with "The sealing key in use is not the one these streams were sealed with" - a wrong key is reported as such, not as tampering.

Not covered: rotating while the server runs; hardware key storage.

## Monitoring and Kafka

An optional **separate port** publishes live statistics, and the same snapshots can stream to **Kafka**. Both are off until asked for; the port is read-only and carries counts and sizes only, never data, keys or tokens.

```bash
python -m mdc serve --metrics-port 9464                       # /metrics, /stats, /stream, /healthz on 127.0.0.1
MDC_METRICS_TOKEN=... python -m mdc serve --metrics-port 9464 --metrics-host 0.0.0.0
python -m mdc serve --kafka-bootstrap broker:9092 --kafka-topic mdc.stats --kafka-interval 10
```

- `GET /metrics` Prometheus text, `GET /stats` JSON, `GET /stream` Server-Sent Events (a snapshot every `MDC_METRICS_INTERVAL` seconds), `GET /healthz`. With `MDC_METRICS_TOKEN` set, everything but `/healthz` needs a bearer token.
- A snapshot has process CPU and memory, system CPU/memory/disk, API request counts, and per database: file size, rows per table, objects by tier, ARCHIVE blocks, streams, events. Memory and CPU are current values with `psutil` (a dependency); without it they fall back to peak memory and CPU time.
- Kafka: one JSON message per snapshot, keyed by host name. You run the broker and install `confluent-kafka` or `kafka-python` (MDC bundles neither; `pip install .[kafka]` adds confluent-kafka). A failed publish is counted and retried; it never stops the server. No broker authentication or TLS settings yet, and the Kafka path is tested with a stand-in producer and against the real client libraries with no broker present, never against a live broker.
- The container reads `MDC_METRICS_PORT`, `MDC_METRICS_HOST`, `MDC_METRICS_TOKEN`, `MDC_METRICS_INTERVAL`, `MDC_KAFKA_BOOTSTRAP`, `MDC_KAFKA_TOPIC`, `MDC_KAFKA_INTERVAL`. Source: `src/mdc/monitoring/`.

## Deployment (Docker and Railway)

```bash
docker build -t modeldb .
docker run -p 8000:8000 -v modeldb:/data -e MDC_API_TOKENS=choose-a-long-random-token modeldb
```

The image runs `deploy/serve.py`: the same app as `mdc serve`, minus its interactive first-run prompt, listening on every address (IPv6 where available) on `PORT` and on 8000, with `GET /healthz` for health checks. `railway.json` builds the Dockerfile, checks `/healthz`, and requires a volume at `/data`.

- **Token:** `MDC_API_TOKENS` (or `MODELDB_TOKEN`). With neither set it makes one, keeps it in `/data/modeldb-api-token`, and prints it in the deploy log. The deployed Explorer asks you to paste it (kept for the browser tab); a wrong one is refused and asked for again. `/admin` redirects to `/`.
- **Key:** set `MDC_DNA_KEY` (64 hex characters) for the sealed ARCHIVE tier and streams, or `MDC_DNA_KEY_FILE` to keep the key file off the data volume; otherwise one is generated under `HOME` (`/data/.mdc/dna.key` in the container). See [Encryption keys](#encryption-keys).
- **Volume:** mount `/data`. Without one, the database, a generated token and the key are lost on every redeploy (the container warns).
- Other variables: `MODELDB_DATABASE` (default `/data/mdc.duckdb`), `MODELDB_TOKEN_FILE`, `HOST`.

## Where things are stored

`data/mdc.duckdb` is the `default` database; every other database is `data/databases/<name>.duckdb` with its table structure in `<name>.schema.json` beside it (this includes tables you add to `default`). Inside each file, table rows live in a `records` table (typed columns for id, version, times and checksum, the fields as JSON), everything else (objects, event streams, the archive) in a block table. Databases from earlier versions are converted the first time they are opened. Credentials live in `~/.mdc/` (`api_tokens.json`, `users.json`, `dna.key`).

## Compared with a traditional database

Measured on the same merchant-style records (one run, Apple M5 laptop, local default-configured PostgreSQL 14 and SQLite, no network). At **10,000 records** MDC took **2.0 MB** (PostgreSQL 1.2 MB, SQLite 0.68 MB; the data as JSON text is 1.6 MB), at 100,000 records 12.5 MB (11.6 and 6.6). One-at-a-time inserts take 0.44 ms (PostgreSQL 0.03 ms, SQLite 0.16 ms); a 10,000-record bulk load (`create_many`) 0.47 s (0.15 s and 0.01 s). A lookup by id takes 0.33 ms at 10,000 records and 1.0 ms at 100,000 (PostgreSQL 0.02 ms); finding by a field value 1.3 ms and 9.8 ms (0.29 and 2.5); a filtered count 3.5 ms and 31 ms (0.5 and 4.7), still growing with the table because field values are not indexed. Event streams cost about 1.18x the plain JSON; the ARCHIVE tier stores **1.00x** the bytes it archives. So MDC is still larger and slower than the ordinary databases (a few times, not the hundreds to thousands of times it was when every read scanned the whole table), and a plain DuckDB table with typed columns held 100,000 rows in a quarter of MDC's space: storing each table's fields as typed columns is the known next step and has not been done. In return there is no SQL to write, no driver and no managed instance: a small always-on deployment can run on a usage-billed host for about $5 a month, against about $14 for the smallest Amazon RDS PostgreSQL instance (third-party price summaries, 2026-10-20; RDS's backups and failover are not included in that gap, and PostgreSQL in the same kind of container would cost about the same as MDC).

Reproduce it with `python benchmarks/compare_stores.py`; see [benchmarks/README.md](benchmarks/README.md) and [benchmarks/RESULTS.md](benchmarks/RESULTS.md) for the full tables and their limits. The same comparison, with the cost assumptions, is on [the documentation site](https://saji1970.github.io/ModelDB/platform.html#vs-database).

## Testing

```bash
pytest -q
```

## Status

Research prototype. Built: classification, storage strategy, the object index and router, model and tensor storage, image and document storage, real compression, matrix storage, the encrypted DNA-encoding archive tier, the REST API and Storage Explorer, conversational storage and database administration (CLI and chat), import and export, schema design from prompts and ER diagrams, event-sourced agent-state streams, deletion with confirmation, bearer-token roles, and container deployment. Not yet built: a login screen / authenticated sessions for the shell and Explorer, benchmarks (CLAUDE.md Phase K), master-key rotation, a language model behind free-form schema design, and event-sourced tables.

The merchants CRUD engine and the analytics conversation path (above) are a separate, earlier system - see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for why the two aren't merged. See [CLAUDE.md](CLAUDE.md) section 51 for the implementation order.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE). Third-party
dependency licenses are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
