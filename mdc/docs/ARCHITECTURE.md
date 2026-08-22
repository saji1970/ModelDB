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

## Storage abstraction

`StorageBackend` (`storage/interface.py`) is block-addressed
(`put`/`get`/`exists`/`delete`/`metadata`/`search`) so `MDCDataEngine`
never depends on DuckDB specifically. `DuckDBStore` is the only
implementation today; `MatrixStore` and `DNAStore` (CLAUDE.md sections
33-38) would implement the same interface without any change above the
storage layer.
