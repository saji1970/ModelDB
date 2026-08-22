-- MDC payments domain schema (see CLAUDE.md sections 7-12)

CREATE TABLE IF NOT EXISTS merchant (
    merchant_id     VARCHAR PRIMARY KEY,
    merchant_name   VARCHAR NOT NULL,
    legal_name      VARCHAR NOT NULL,
    country         VARCHAR NOT NULL,
    currency        VARCHAR NOT NULL,
    merchant_type   VARCHAR NOT NULL,
    status          VARCHAR NOT NULL,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS customer (
    customer_id     VARCHAR PRIMARY KEY,
    customer_name   VARCHAR NOT NULL,
    country         VARCHAR NOT NULL,
    email           VARCHAR NOT NULL,
    status          VARCHAR NOT NULL,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS account (
    account_id          VARCHAR PRIMARY KEY,
    merchant_id         VARCHAR NOT NULL REFERENCES merchant(merchant_id),
    ledger_balance      DECIMAL(18, 2) NOT NULL,
    available_balance   DECIMAL(18, 2) NOT NULL,
    currency            VARCHAR NOT NULL,
    status              VARCHAR NOT NULL,
    updated_at          TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS terminal (
    terminal_id     VARCHAR PRIMARY KEY,
    merchant_id     VARCHAR NOT NULL REFERENCES merchant(merchant_id),
    terminal_type   VARCHAR NOT NULL,
    status          VARCHAR NOT NULL,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS card (
    card_id         VARCHAR PRIMARY KEY,
    customer_id     VARCHAR NOT NULL REFERENCES customer(customer_id),
    card_brand      VARCHAR NOT NULL,
    last_four       VARCHAR NOT NULL,
    status          VARCHAR NOT NULL,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS settlement (
    settlement_id       VARCHAR PRIMARY KEY,
    merchant_id         VARCHAR NOT NULL REFERENCES merchant(merchant_id),
    settlement_amount   DECIMAL(18, 2) NOT NULL,
    settlement_balance  DECIMAL(18, 2) NOT NULL,
    currency            VARCHAR NOT NULL,
    settlement_status   VARCHAR NOT NULL,
    settlement_date     DATE NOT NULL,
    created_at          TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS transaction (
    transaction_id      VARCHAR PRIMARY KEY,
    merchant_id         VARCHAR NOT NULL REFERENCES merchant(merchant_id),
    customer_id         VARCHAR NOT NULL REFERENCES customer(customer_id),
    terminal_id         VARCHAR NOT NULL REFERENCES terminal(terminal_id),
    transaction_type    VARCHAR NOT NULL,
    amount               DECIMAL(18, 2) NOT NULL,
    currency             VARCHAR NOT NULL,
    status                VARCHAR NOT NULL,
    transaction_date     TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS payment (
    payment_id      VARCHAR PRIMARY KEY,
    transaction_id  VARCHAR NOT NULL REFERENCES transaction(transaction_id),
    merchant_id     VARCHAR NOT NULL REFERENCES merchant(merchant_id),
    amount          DECIMAL(18, 2) NOT NULL,
    currency        VARCHAR NOT NULL,
    payment_status  VARCHAR NOT NULL,
    payment_date    TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS refund (
    refund_id       VARCHAR PRIMARY KEY,
    payment_id      VARCHAR NOT NULL REFERENCES payment(payment_id),
    merchant_id     VARCHAR NOT NULL REFERENCES merchant(merchant_id),
    amount          DECIMAL(18, 2) NOT NULL,
    currency        VARCHAR NOT NULL,
    refund_status   VARCHAR NOT NULL,
    refund_date     TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS chargeback (
    chargeback_id       VARCHAR PRIMARY KEY,
    transaction_id      VARCHAR NOT NULL REFERENCES transaction(transaction_id),
    merchant_id         VARCHAR NOT NULL REFERENCES merchant(merchant_id),
    amount              DECIMAL(18, 2) NOT NULL,
    currency            VARCHAR NOT NULL,
    chargeback_status   VARCHAR NOT NULL,
    chargeback_date     TIMESTAMP NOT NULL
);

-- Generic block-addressed storage, shared by every StorageBackend
-- implementation (DuckDBStore, MatrixStore, DNAStore) so callers can
-- put()/get() opaque payloads the same way regardless of backend.
CREATE TABLE IF NOT EXISTS blocks (
    block_id    VARCHAR PRIMARY KEY,
    payload     BLOB NOT NULL,
    metadata    VARCHAR,
    checksum    VARCHAR NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT now()
);
