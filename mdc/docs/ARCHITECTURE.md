# Architecture

```
Applications (CLI today; REST API / SDK later)
        |
   MDC Data Engine (engine/data_engine.py)
        |
   Schema Registry (schema/registry.py)   Storage Backend (storage/interface.py)
                                                  |
                                      DuckDBStore (storage/duckdb_store.py)
```

Natural language is a separate, optional path into the same engine:

```
User input
   |
   v
mce.resolver.interpret_detailed()   <- ontology resolution, intent, ambiguity
   |
   v
cql.interpreter.process_turn()      <- conversational state, clarification,
   |                                   confirmation, CRUD-vs-read routing
   v
cql.crud (CREATE/UPDATE/DELETE)     mce pipeline (FETCH/COUNT/... read)
   |                                   |
   v                                   v
MDCOperation (model/operation.py)   QueryContext (mce/context.py)
   |
   v
DataEngine.execute()
```

No caller - CLI, NLP, or a future REST API/SDK - is allowed to construct
SQL or touch `StorageBackend` directly. Everything that mutates data goes
through an `MDCOperation` (`CreateOperation` / `ReadOperation` /
`UpdateOperation` / `DeleteOperation`) executed by `MDCDataEngine`.

## Two datasets, on purpose

This prototype currently carries two separate data surfaces, and they are
**not** merged:

1. **The synthetic payments dataset** (`data/schema.sql`: `merchant`,
   `account`, `settlement`, `transaction`, ...) - a normalized, six-table
   relational schema seeded with up to 500k+ synthetic rows. The read-only
   analytics conversation (`mce.resolver`, ontology-driven: "Show merchants
   with settlement balance above 10000 USD") targets this domain. Reads are
   not yet compiled to SQL against it - see `docs/TESTING.md` / README for
   current status - so `/context` shows the resolved query, not results.

2. **The `merchants` collection** (`schema/collections.yaml`, a single
   flat record store, `MDCDataEngine`-backed) - what CRUD-through-
   conversation ("Create a merchant called ABC Store in India") actually
   reads and writes, via the generic block-addressed `StorageBackend`
   (JSON-serialized `Record`s, one block per record).

Welding CRUD onto the normalized six-table schema would mean every
`UpdateOperation` on a single logical "merchant" potentially spanning
writes across `merchant`/`account`/`settlement` in one transaction - real
work, not yet justified by anything in the current acceptance tests, which
only exercise a handful of fields (name, country, the three balance
fields, status). `merchants` is deliberately the simpler, generic
collection/record model CLAUDE.md sections 6-8 describe, so it also
doubles as the reference implementation for how a *new* collection is
added later without touching the payments schema at all.

Unifying the two - or migrating the payments dataset itself onto the
generic collection model - is future work, not a defect in the current
scope.

## Multiple databases and the three-way NLP dispatch

`DatabaseManager` (`databases/manager.py`) replaces the single implicit
database with any number of independently-stored, named ones. Each is a
fully isolated `DatabaseHandle`: its own `DuckDBStore` file, `SchemaRegistry`,
`MDCDataEngine`, `StorageRouter`, and `ObjectService`. Databases are created
lazily and cached by name; there is no hardcoded limit. User-supplied names
are validated against a strict pattern (`^[A-Za-z][A-Za-z0-9_-]{0,62}$`)
*before* any path is constructed from them, since a name becomes part of a
filesystem path (`databases/<name>.duckdb`) - the usual path-traversal
boundary for user-controlled filenames.

Free-text input entering `conversation.interpreter.process_turn()` is tried
against three separate parsers, in order, each anchored-regex rather than
keyword-scored so their vocabularies never collide even though they share
surface words like "show":

```
text
  |
  v
nlp.db_command.parse_database_command()   <- "create database", "create table",
  |  (matched -> conversation.db_interpreter)  "show data in", "insert into" ...
  v (no match)
nlp.command.parse_storage_command()       <- "archive it", "store <path>",
  |  (matched -> conversation.interpreter's own handlers)  "describe it" ...
  v (no match)
falls through to cql.interpreter.process_turn()  <- merchants CRUD/analytics
```

`db_interpreter` handlers only ever call `SchemaRegistry.create_collection()`
with a validated typed field list to create a table - never raw SQL DDL, kept
to the same "no LLM-authored SQL" rule as the rest of the system (CLAUDE.md
section 3). Each conversational session tracks its own
`StorageConversationState.current_database`; switching it resets that
session's "it" pronoun and any pending delete confirmation, since both would
otherwise resolve against the wrong database's object index. The merchants
CRUD/analytics natural language is deliberately *not* database-aware - it
always targets the original `default` database's `merchants` collection
regardless of what the session has switched to, since generalizing that
already-stable, older system was judged out of scope for this change; the
database/table administration commands above give equivalent power for any
collection in any database via explicit phrasing instead.

## Storage abstraction

`StorageBackend` (`storage/interface.py`) is block-addressed
(`put`/`get`/`exists`/`delete`/`metadata`/`search`) so `MDCDataEngine`
never depends on DuckDB specifically. Implementations: `DuckDBStore`
(durable, one file per database), `MemoryStorageBackend` (the HOT tier),
and `DNAStorageBackend` (the ARCHIVE tier: AES-256-GCM first, then 2 bits
per base). The archive is persistent: with `persist=` (the router passes the
database's own store) each block's encrypted A/C/G/T copies are written
through under `dna:<block_id>` ids, with only a small index held in memory,
so archived objects survive a restart; without it the backend is an in-memory
simulation used by the research tooling and tests. `StorageRouter` (`storage_intelligence/router.py`) sends each
object to a tier's backend; matrix storage lives in `matrix/`.

## Later additions

Each of these sits on the same Data Engine / `StorageBackend` layer and
reaches storage only through it.

- **`portability/`** - import and export. Readers (CSV, SQL script, Markdown
  design document, Mermaid ER diagram, MDC export file) all produce one
  `ImportPlan`; `apply.prepare_import` validates it (names, conflicts, value
  types, required fields, record ids) and reports the permissions applying it
  needs *before* anything is written. A SQL script is parsed into tables and
  rows and never executed. Writers produce CSV, SQL, Excel, a Markdown design
  document with an ER diagram, and the MDC export file. `design.py` is the
  deterministic grammar behind "create database shop with customers(...)", with
  `SchemaDesigner` as the seam for a language model; its output is previewed
  and confirmed like everything else.
- **`events/`** - event-sourced agent-state streams. `EventStore` keeps a base
  state plus an append-only log of timestamped changes on any `StorageBackend`
  and rebuilds the state as of a time or event. How a change is encoded is a
  `StateCodec`; replay is deterministic. Every record is sealed (AES-256-GCM,
  per-stream key via HKDF from the master key, bound to stream, kind and
  position). `DatabaseManager.events(name)` is the shared entry point.
- **State and events (concept)** - data that changes over time is kept as
  *base + ordered events*, never as an overwritten value. State is derived by
  deterministic replay (`state_at(time | seq)`), so any past state is
  recoverable; the codec defines the change format; sealing hides the stored
  bytes. Model weights are *not* event-sourced: they stay in the object store as
  tensor blocks, so a checkpoint is never rewritten for a one-field change.
- **Deleting** - `MDCDataEngine.count_records / delete_record /
  drop_collection` and `DatabaseManager.summarize / drop / drop_table`. Chat
  asks for confirmation (`conversation/state.PendingDrop`); REST asks for
  `?confirm=`. The `default` database and its `merchants` table are protected.
- **`security/`** - `roles.py` (viewer, editor, db_admin, admin and their
  permissions, including `drop`), `tokens.py` (hashed bearer tokens),
  `users.py` (scrypt-hashed accounts), `keys.py` (the master key for sealed
  data: `MDC_DNA_KEY`, else the file `MDC_DNA_KEY_FILE` names, else
  `~/.mdc/dna.key`; plus fingerprints and the startup notice). `EventStore`
  stores a sealed key check so a wrong key is told apart from tampering, and
  `rotate_key` re-seals every stream under a new key (`mdc key rotate`). Accounts share the role vocabulary but are not a login yet: access is
  decided by the role of the token presented.
- **`monitoring/`** - read-only statistics. `collector.py` builds a snapshot (process and system CPU/memory, API request counters fed by a middleware in `api/app.py`, and per database: file size, rows, objects by tier, ARCHIVE blocks, streams), cached for 2 seconds; `server.py` serves it on its own port (`/metrics` Prometheus, `/stats`, `/stream` SSE, `/healthz`); `kafka.py` publishes JSON snapshots from a background thread; `runtime.py` starts them from flags or `MDC_METRICS_*` / `MDC_KAFKA_*`. Off unless asked for.
- **`api/`** - the FastAPI app (`app.py`), the chat engine (`chat.py`, one per
  session state, shared by the Explorer and `POST /chat`), and the Storage
  Explorer page (`static/index.html`). `deploy/serve.py` runs the same app in a
  container with a health check and a token prompt in place of the embedded
  admin token.
- **Chat dispatch** - database administration (`nlp/db_command.py`), then
  object storage (`nlp/command.py`), then merchants (`cql`); each requires its
  own literal keywords so the three vocabularies cannot collide. Pending
  confirmations (deletes, schema proposals) are cleared by any reply that is
  not the confirmation, so a question never traps the conversation.
