# CLAUDE.md

# MDC — Molecular Data Center

Version: 1.0
Status: Research / Executable Prototype Specification

---

# 1. PROJECT DEFINITION

MDC stands for Molecular Data Center.

MDC is a research prototype for a new data-storage and data-access architecture inspired by the information density and encoding characteristics of biological DNA.

The goal is NOT simply to create a natural-language SQL interface.

The goal is to build:

1. A new logical data storage model.
2. A data engine capable of CRUD operations.
3. A matrix/block representation of data.
4. A DNA-compatible encoding layer.
5. A conversational/NLP interface.
6. A programmatic API.
7. SDKs that applications can use.
8. A deterministic semantic layer that converts natural language into validated MDC operations.
9. A storage abstraction that allows conventional storage, matrix storage, and simulated DNA storage to coexist.

The system must be designed so that the storage engine exists independently of the NLP layer.

---

# 2. CORE ARCHITECTURAL PRINCIPLE

The architecture is:

    Applications
        ↓
    MDC API
        ↓
    MDC Data Service
        ↓
    MDC Data Engine
        ↓
    Logical Data Model
        ↓
    Storage Abstraction
        ↓
    Binary / Matrix / DNA Storage

Natural language is an OPTIONAL access layer:

    User
        ↓
    Conversational Interface
        ↓
    NLP / Semantic Layer
        ↓
    MDC Operation
        ↓
    MDC Data Engine

The NLP layer must NEVER directly manipulate storage.

---

# 3. MOST IMPORTANT RULE

The system must distinguish:

    DATA STORAGE
    DATA ENGINE
    QUERY LANGUAGE
    NLP INTERFACE
    API INTERFACE

These are separate components.

Do NOT implement MDC as:

    Natural Language → SQL → Database

Instead implement:

    Natural Language
          ↓
    Semantic Interpretation
          ↓
    MDC Operation
          ↓
    Data Engine
          ↓
    Storage

---

# 4. MDC DATA ENGINE

The MDC Data Engine is the core of the system.

It must exist independently of the conversational interface.

The Data Engine must expose:

    create()
    read()
    update()
    delete()
    search()
    count()
    aggregate()
    batch_create()
    batch_update()
    batch_delete()

The engine must accept structured operations.

Example:

    CreateRecord(
        collection="merchant",
        data={
            "merchant_id": "M1001",
            "name": "ABC Store",
            "country": "IN",
            "balance": 10000
        }
    )

The engine must NOT require natural language.

---

# 5. MDC IS NOT SQL

MDC must have its own logical operation model.

SQL may be used internally for the initial prototype implementation.

However:

    SQL is an implementation backend.

SQL must NOT define the MDC architecture.

The long-term architecture must allow:

    MDC → DuckDB
    MDC → PostgreSQL
    MDC → Matrix Storage
    MDC → DNA Storage
    MDC → Distributed Storage

without changing the application interface.

---

# 6. MDC DATA MODEL

MDC uses a logical model:

    Namespace
       ↓
    Collection
       ↓
    Record
       ↓
    Field
       ↓
    Value

Example:

    Namespace:
        payments

    Collection:
        merchants

    Record:

        merchant_id: M1001
        name: ABC Store
        country: India
        currency: USD
        balance: 10000

---

# 7. COLLECTIONS

Collections are analogous to tables but MUST NOT be tightly coupled to relational database terminology.

Example:

    merchants
    customers
    payments
    transactions
    settlements

---

# 8. RECORDS

Each record must have:

    record_id
    collection_id
    version
    created_at
    updated_at
    payload
    checksum

---

# 9. SCHEMA

MDC must support schema definitions.

Example:

    merchants:

        merchant_id: string
        name: string
        country: string
        currency: string
        balance: decimal
        status: string

Schemas must be machine-readable.

Use JSON/YAML initially.

---

# 10. SCHEMA REGISTRY

Create:

    SchemaRegistry

Responsibilities:

    create_collection()
    delete_collection()
    get_collection()
    add_field()
    remove_field()
    validate_record()

The schema registry is independent of DuckDB.

---

# 11. CRUD

The Data Engine MUST implement complete CRUD.

## CREATE

Example structured operation:

    CREATE merchants
    {
        merchant_id: "M1001",
        name: "ABC Store",
        country: "India"
    }

## READ

    READ merchants
    WHERE merchant_id = "M1001"

## UPDATE

    UPDATE merchants
    SET balance = 15000
    WHERE merchant_id = "M1001"

## DELETE

    DELETE merchants
    WHERE merchant_id = "M1001"

---

# 12. NATURAL LANGUAGE CRUD

Natural language must map to the same Data Engine operations.

Examples:

    Create a merchant called ABC Store in India.

    Show me ABC Store.

    Change ABC Store balance to 15000.

    Delete merchant ABC Store.

The NLP layer produces:

    CreateOperation
    ReadOperation
    UpdateOperation
    DeleteOperation

It must NOT generate arbitrary SQL.

---

# 13. FINANCIAL SAFETY

Because the initial domain includes payments:

Read operations:

    permitted

Create:

    permitted in development

Update:

    permitted in development

Delete:

    permitted in development

Financial actions such as:

    transfer money
    initiate payment
    settle payment

are NOT implemented merely because a natural-language command exists.

They require explicit transactional APIs and authorization.

---

# 14. MDC OPERATION MODEL

Create an operation AST.

Base class:

    MDCOperation

Operations:

    CreateOperation
    ReadOperation
    UpdateOperation
    DeleteOperation
    SearchOperation
    AggregateOperation
    BatchOperation

---

# 15. OPERATION EXAMPLE

Natural language:

    Show all merchants in India with balance above 10000.

Semantic layer produces:

    ReadOperation(
        collection="merchants",
        filters=[
            Filter(
                field="country",
                operator="=",
                value="India"
            ),
            Filter(
                field="balance",
                operator=">",
                value=10000
            )
        ]
    )

This operation is passed to:

    MDCDataEngine.read()

---

# 16. CONVERSATIONAL LAYER

The conversational layer is an interface.

It is NOT the database.

Users can say:

    Show me merchants with balance above 10,000.

The semantic layer resolves:

    entity = merchant
    field = balance
    operator = >
    value = 10000

If "balance" is ambiguous:

    ledger_balance
    available_balance
    settlement_balance

the system asks the user.

---

# 17. LLM ROLE

The LLM may propose:

    intent
    entity
    field
    filters
    values
    relationships

The LLM MUST NOT be trusted to execute operations.

Architecture:

    LLM
      ↓
    Candidate Interpretation
      ↓
    Deterministic Validator
      ↓
    Valid MDC Operation
      ↓
    Data Engine

---

# 18. DETERMINISTIC VALIDATION

Before execution verify:

    collection exists
    fields exist
    datatypes match
    operators are valid
    values are valid
    relationships are valid
    operation is permitted
    authorization permits operation

If validation fails:

    DO NOT EXECUTE.

---

# 19. CLARIFICATION ENGINE

If the semantic layer cannot determine the intended operation with sufficient confidence, it MUST ask the user.

Example:

    User:
    Delete the merchant.

System:

    Which merchant?

    1. ABC Store
    2. XYZ Retail
    3. Global Mart

No operation is executed until ambiguity is resolved.

---

# 20. QUERY CONTEXT

Create:

    QueryContext

Containing:

    intent
    operation
    collection
    fields
    filters
    sort
    limit
    aggregation
    conversation_id
    confidence
    ambiguity
    pending_question

---

# 21. CONVERSATIONAL FOLLOW-UP

Conversation:

    User:
    Show merchants with settlement balance.

    System:
    Which currency?

    User:
    USD.

    System:
    What threshold?

    User:
    Above 10000.

The context must progressively become:

    collection = merchants
    field = settlement_balance
    currency = USD
    operator = >
    value = 10000

---

# 22. MDC QUERY LANGUAGE

Create a structured language called:

    MDCQL

MDCQL is an internal logical representation.

Example:

    FETCH merchants
    WHERE country = "India"
    AND balance > 10000
    ORDER BY balance DESC
    LIMIT 20

MDCQL is NOT required to be exposed to normal users.

It exists as a deterministic intermediate representation.

---

# 23. API LAYER

MDC MUST expose programmatic APIs.

Use REST initially.

Framework:

    FastAPI

Base path:

    /api/v1

---

# 24. CRUD API

Create:

    POST   /collections/{collection}/records
    GET    /collections/{collection}/records/{id}
    PUT    /collections/{collection}/records/{id}
    DELETE /collections/{collection}/records/{id}

---

# 25. QUERY API

Create:

    POST /query

Example request:

    {
        "collection": "merchants",
        "filters": [
            {
                "field": "country",
                "operator": "=",
                "value": "India"
            }
        ]
    }

---

# 26. NATURAL LANGUAGE API

Create:

    POST /nlp/query

Example:

    {
        "prompt":
        "Show me merchants in India with balance above 10000"
    }

Response:

    {
        "status": "resolved",
        "operation": {...},
        "results": [...]
    }

If ambiguous:

    {
        "status": "clarification_required",
        "question":
        "Which balance do you mean?",
        "options": [...]
    }

---

# 27. CRUD THROUGH NLP

The API must support:

    POST /nlp/query

for:

    create
    read
    update
    delete

Example:

    "Create a merchant called ABC Store in India."

The system creates a CreateOperation.

Example:

    "Change ABC Store balance to 15000."

The system creates an UpdateOperation.

Example:

    "Delete ABC Store."

The system creates a DeleteOperation.

---

# 28. API MUST SHARE THE SAME DATA ENGINE

This is critical.

The following:

    CLI
    REST API
    SDK
    NLP
    future GraphQL API

must all call:

    MDCDataEngine

They must NOT implement their own database logic.

---

# 29. APPLICATION SDK

Create a Python SDK.

Example:

    from mdc_sdk import MDCClient

    client = MDCClient("http://localhost:8000")

    client.create(
        "merchants",
        {
            "merchant_id": "M1001",
            "name": "ABC Store"
        }
    )

    client.get(
        "merchants",
        "M1001"
    )

    client.update(
        "merchants",
        "M1001",
        {
            "balance": 15000
        }
    )

    client.delete(
        "merchants",
        "M1001"
    )

---

# 30. NLP SDK

The SDK must also support:

    client.ask(
        "Show merchants with balance above 10000"
    )

The server resolves the request into a validated MDC operation.

---

# 31. STORAGE ABSTRACTION

Create:

    StorageBackend

Methods:

    write_block()
    read_block()
    update_block()
    delete_block()
    locate()
    search()
    metadata()

---

# 32. INITIAL STORAGE BACKEND

Implement:

    DuckDBStorage

DuckDB is used only as the first development backend.

The Data Engine must not depend directly on DuckDB.

---

# 33. MATRIX STORAGE

Implement a logical matrix representation.

The purpose is to investigate whether records can be represented using multidimensional structures rather than traditional row-oriented storage.

Example:

    Record
       ↓
    Binary representation
       ↓
    Matrix blocks
       ↓
    Block index

Matrix storage must support:

    encode
    store
    locate
    retrieve
    reconstruct

---

# 34. DNA STORAGE MODEL

DNA is treated as a potential storage medium.

The system must support:

    binary data
        ↓
    encoded symbols
        ↓
    DNA bases
        ↓
    DNA sequence

Basic mapping:

    00 → A
    01 → C
    10 → G
    11 → T

This is a prototype representation only.

---

# 35. DNA STORAGE ABSTRACTION

Create:

    DNAStorageBackend

It must implement the same StorageBackend interface.

Therefore:

    MDCDataEngine
        ↓
    StorageBackend

can eventually use:

    DuckDBStorage
    MatrixStorage
    DNAStorage

without changing the API.

---

# 36. DNA RECORD STRUCTURE

Each DNA storage block should contain logical metadata:

    block_id
    record_id
    collection_id
    sequence_number
    payload_length
    checksum
    encoding_version
    ECC metadata

The metadata itself may initially be stored outside the simulated DNA sequence.

---

# 37. DNA ERROR MODEL

Implement simulation of:

    substitution
    insertion
    deletion
    sequence dropout

This allows research into reliability.

---

# 38. DNA ERROR CORRECTION

Create:

    ECCProvider

The storage engine must not depend on a particular ECC algorithm.

Future implementations may include:

    Reed-Solomon
    fountain codes
    LDPC
    custom redundancy schemes

---

# 39. DATA INTEGRITY

Every record/block must have:

    SHA-256 checksum

The system must verify integrity after retrieval.

If checksum fails:

    return DATA_INTEGRITY_ERROR

Do not silently return corrupted data.

---

# 40. VERSIONING

The storage model must support versioned records.

Example:

    record M1001

    version 1
    version 2
    version 3

The system should eventually support:

    time travel
    audit history
    rollback

Version 1.0 only needs basic version metadata.

---

# 41. BLOCK ARCHITECTURE

Do not assume:

    one record = one DNA sequence

Instead use:

    Collection
       ↓
    Record
       ↓
    Payload
       ↓
    Blocks
       ↓
    Encoded sequences

This allows large records to be split across multiple storage units.

---

# 42. INDEX

Create:

    MDCIndex

The index maps:

    collection
    record_id
    field
    value
    block_id

The index may initially be implemented using DuckDB.

Future versions can investigate:

    matrix index
    probabilistic index
    DNA metadata index

---

# 43. IMPORTANT STORAGE PRINCIPLE

Do NOT assume that DNA storage should perform random database-style CRUD directly.

DNA is likely better suited for:

    archival storage
    cold storage
    high-density storage

while conventional storage may handle:

    hot data
    indexes
    frequent updates

Therefore MDC should eventually support:

    Hot Storage
        ↓
    Matrix / Block Storage
        ↓
    DNA Archive

---

# 44. HYBRID STORAGE

Create a future-compatible:

    StorageRouter

Possible tiers:

    HOT
    WARM
    COLD
    DNA_ARCHIVE

Example:

    frequently accessed data
        → HOT

    historical data
        → WARM

    archival data
        → DNA_ARCHIVE

---

# 45. APPLICATION ARCHITECTURE

Applications should never need to know whether data is stored in:

    DuckDB
    Matrix
    DNA

The application sees:

    MDC API

Example:

    Mobile App
       ↓
    MDC API
       ↓
    Data Engine
       ↓
    Storage Router
       ↓
    appropriate backend

---

# 46. CLI

The CLI is one application interface.

Example:

    mdc> Show merchants in India

    mdc> Create merchant ABC Store

    mdc> Update ABC Store balance to 15000

    mdc> Delete ABC Store

    mdc> Show merchant ABC Store

---

# 47. CLI COMMANDS

Support:

    /help
    /context
    /operation
    /schema
    /storage
    /history
    /reset
    /debug
    /exit

---

# 48. API DOCUMENTATION

FastAPI must automatically expose:

    OpenAPI
    Swagger UI
    ReDoc

Document:

    CRUD
    Query
    NLP
    Schema
    Storage metadata

---

# 49. SECURITY

Implement:

    authentication interface
    authorization interface
    operation policy

Do not hard-code authentication.

Create:

    AuthProvider

and:

    AuthorizationPolicy

---

# 50. OPERATION AUTHORIZATION

Example:

    READ
    CREATE
    UPDATE
    DELETE
    ADMIN

A user's authorization must be evaluated before the operation reaches the Data Engine.

---

# 51. AUDIT LOG

Every mutation must generate an audit record.

Record:

    timestamp
    user
    operation
    collection
    record_id
    before_hash
    after_hash
    request_id

---

# 52. TRANSACTION MODEL

The Data Engine must expose:

    begin()
    commit()
    rollback()

The initial DuckDB implementation can use native transactions.

Future DNA storage may use an append/version model rather than conventional transactions.

---

# 53. PERFORMANCE BENCHMARK

Measure:

    create latency
    read latency
    update latency
    delete latency
    search latency
    batch throughput

Compare:

    DuckDB
    Matrix representation
    simulated DNA

Do NOT claim that simulated DNA is faster than conventional databases.

The benchmark must distinguish:

    encoding speed
    storage density
    retrieval speed
    update cost
    durability

---

# 54. STORAGE DENSITY RESEARCH

The research layer should calculate:

    logical_bytes
    encoded_bytes
    storage_symbols
    overhead_bytes
    redundancy_bytes

For DNA:

    effective_density =
        payload_bits /
        physical_or_logical_storage_units

Do not present theoretical DNA density as achieved hardware density.

---

# 55. RESEARCH HYPOTHESIS

MDC should investigate whether:

    structured data
        ↓
    binary blocks
        ↓
    matrix representation
        ↓
    molecular encoding

can provide advantages for:

    storage density
    archival durability
    data portability
    energy efficiency
    long-term preservation

This is a research hypothesis, not an established claim.

---

# 56. TEST DATA

Initial domain:

    payments

Create:

    merchants
    customers
    accounts
    transactions
    payments
    settlements

Generate deterministic synthetic data.

Seed:

    42

---

# 57. TESTING

All components require tests.

Minimum:

    Data Engine tests
    CRUD tests
    Schema tests
    API tests
    NLP tests
    Conversation tests
    Matrix tests
    DNA encoding tests
    DNA decoding tests
    Integrity tests
    Authorization tests

---

# 58. REQUIRED ACCEPTANCE TEST

The following must work.

CLI:

    mdc> Create a merchant called ABC Store in India

System:

    creates merchant

Then:

    mdc> Show me ABC Store

System:

    returns merchant

Then:

    mdc> Change ABC Store balance to 15000

System:

    asks for clarification if multiple balances exist

Then:

    mdc> settlement balance

System:

    updates settlement balance

Then:

    mdc> Show ABC Store

System:

    returns updated record

Then:

    mdc> Delete ABC Store

System:

    asks for confirmation

Then:

    yes

System:

    deletes record

---

# 59. API ACCEPTANCE TEST

Application sends:

    POST /api/v1/collections/merchants/records

The record must be stored.

Then:

    GET

must retrieve it.

Then:

    PUT

must update it.

Then:

    DELETE

must delete it.

The exact same Data Engine used by the CLI must execute these operations.

---

# 60. NLP API ACCEPTANCE TEST

Request:

    POST /api/v1/nlp/query

Body:

    {
      "prompt":
      "Show me merchants in India with settlement balance above 10000"
    }

Expected:

    NLP
      ↓
    QueryContext
      ↓
    MDCOperation
      ↓
    Validation
      ↓
    DataEngine
      ↓
    Results

---

# 61. NO DIRECT LLM EXECUTION

The following architecture is forbidden:

    LLM
      ↓
    SQL
      ↓
    Database

The following architecture is required:

    LLM
      ↓
    Candidate MDC Operation
      ↓
    Deterministic Validator
      ↓
    Authorized MDC Operation
      ↓
    Data Engine
      ↓
    Storage

---

# 62. PHASE PLAN

## PHASE 1

Build:

    project scaffold
    schema registry
    Data Engine interface
    DuckDB storage backend
    CRUD
    CLI

---

## PHASE 2

Build:

    MDC Operation AST
    deterministic validation
    query engine
    indexing

---

## PHASE 3

Build:

    NLP semantic layer
    ontology
    conversation
    ambiguity resolution
    clarification

---

## PHASE 4

Build:

    REST API
    OpenAPI
    CRUD endpoints
    query endpoint
    NLP endpoint

---

## PHASE 5

Build:

    Python SDK
    application examples
    API integration tests

---

## PHASE 6

Build:

    Matrix Storage
    block encoding
    matrix addressing
    benchmarks

---

## PHASE 7

Build:

    DNA Encoder
    DNA Decoder
    checksum
    ECC
    corruption simulator

---

## PHASE 8

Build:

    DNA Storage Backend
    block archive
    retrieval
    integrity verification

---

## PHASE 9

Build:

    Storage Router
    hot/warm/cold/DNA tiers

---

## PHASE 10

Build:

    performance benchmark
    density benchmark
    reliability benchmark
    research reports

---

# 63. REQUIRED PROJECT STRUCTURE

Create:

    mdc/

    src/mdc/

        api/
        cli/
        engine/
        model/
        schema/
        nlp/
        ontology/
        conversation/
        storage/
            base.py
            duckdb.py
            matrix.py
            dna.py
        matrix/
        dna/
        indexing/
        security/
        audit/
        transaction/

    sdk/

        python/

    tests/

    docs/

    examples/

    benchmarks/

    data/

---

# 64. DEFINITION OF DONE

MDC is considered functional when:

[ ] Data Engine works without NLP

[ ] CRUD works without NLP

[ ] CLI works

[ ] REST API works

[ ] SDK works

[ ] NLP can produce MDC operations

[ ] Ambiguous prompts trigger clarification

[ ] LLM cannot directly execute SQL

[ ] Schema validation works

[ ] Authorization works

[ ] Audit logging works

[ ] DuckDB backend works

[ ] Matrix backend works

[ ] DNA encoding works

[ ] DNA decoding works

[ ] DNA integrity validation works

[ ] Storage backend abstraction works

[ ] API and CLI use the same Data Engine

[ ] Tests pass

---

# 65. IMPLEMENTATION RULE

Do not build the system around SQL.

Build the system around:

    MDCOperation
    MDCDataEngine
    StorageBackend

SQL is only the initial implementation mechanism for:

    DuckDBStorage

---

# 66. FIRST CLAUDE TASK

Start by examining the existing project.

The current project contains Phase 1–3 work focused heavily on conversational querying.

DO NOT throw away the existing work.

Refactor it so that:

    Conversation
          ↓
    MDCOperation
          ↓
    DataEngine

rather than:

    Conversation
          ↓
    QueryContext only

Preserve existing tests where possible.

Then implement:

    1. MDCOperation models
    2. DataEngine interface
    3. DuckDBStorage interface
    4. CRUD operations
    5. Schema registry
    6. CLI CRUD commands
    7. Tests

Do not implement DNA physical storage yet.

First establish a correct storage abstraction.

---

# 67. FINAL ARCHITECTURAL OBJECTIVE

The final system should allow:

    User
       │
       │ natural language
       ▼
    MDC Conversation Engine
       │
       ▼
    MDC Operation
       │
       ├───────────────┐
       │               │
       ▼               ▼
    REST API          SDK
       │               │
       └───────┬───────┘
               ▼
          MDC Data Engine
               │
               ▼
          Storage Router
               │
       ┌───────┼────────┐
       ▼       ▼        ▼
      HOT     MATRIX    DNA
     STORE    STORE    ARCHIVE

The storage technology must be replaceable without changing the application or conversational interface.

The ultimate research objective is to investigate whether a DNA-inspired logical storage architecture can provide a viable high-density archival data layer while conventional storage handles hot and transactional data.
