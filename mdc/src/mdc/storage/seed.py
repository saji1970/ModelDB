"""Deterministic synthetic payments data generator (CLAUDE.md section 13).

Uses the stdlib ``random`` module seeded with a fixed value so that the
same scale always produces byte-identical rows across runs and machines.
Hand-writing 500,000+ INSERT statements into data/seed.sql is not
practical, so generation happens here instead - see data/seed.sql for
the rationale.
"""

from __future__ import annotations

import csv
import os
import random
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import duckdb

SEED = 42
EPOCH = datetime(2024, 1, 1)


def _bulk_insert(conn: duckdb.DuckDBPyConnection, table: str, rows: list[tuple]) -> None:
    """Load `rows` into `table` via a temp CSV + COPY.

    `executemany()` binds and executes one statement per row, which is
    fine for hundreds of rows but not for the hundreds of thousands
    CLAUDE.md section 13 asks for (500k+ transactions took minutes).
    COPY FROM a CSV is DuckDB's vectorized bulk-load path and loads the
    same data in well under a second, so seed generation uses it instead.
    `table` is always one of our own fixed internal table names, never
    user input.
    """
    if not rows:
        return
    fd, path = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            csv.writer(f).writerows(rows)
        conn.execute(f"COPY {table} FROM '{path}' (HEADER false, DELIMITER ',')")
    finally:
        os.unlink(path)

COUNTRY_CURRENCY = [
    ("US", "USD"),
    ("IN", "INR"),
    ("GB", "GBP"),
    ("DE", "EUR"),
    ("SG", "SGD"),
    ("AU", "AUD"),
    ("CA", "CAD"),
    ("FR", "EUR"),
    ("JP", "JPY"),
    ("BR", "BRL"),
]

MERCHANT_TYPES = ["retail", "ecommerce", "marketplace", "subscription", "services"]
MERCHANT_STATUSES = ["active", "suspended", "closed"]
CUSTOMER_STATUSES = ["active", "suspended", "closed"]
ACCOUNT_STATUSES = ["active", "frozen", "closed"]
TERMINAL_TYPES = ["pos", "online", "mobile"]
CARD_BRANDS = ["visa", "mastercard", "amex", "rupay"]
TRANSACTION_TYPES = ["purchase", "refund", "authorization", "capture", "void"]
TRANSACTION_STATUSES = ["completed", "pending", "failed"]
SETTLEMENT_STATUSES = ["pending", "settled", "failed"]
PAYMENT_STATUSES = ["succeeded", "failed", "pending"]
REFUND_STATUSES = ["succeeded", "failed", "pending"]
CHARGEBACK_STATUSES = ["open", "won", "lost"]

NAME_PARTS_1 = ["Nova", "Sunrise", "Blue", "Summit", "Cedar", "Harbor", "Delta", "Orbit", "Pioneer", "Maple"]
NAME_PARTS_2 = ["Retail", "Traders", "Mart", "Goods", "Foods", "Works", "Labs", "Store", "Bazaar", "Supply"]
FIRST_NAMES = ["Aiden", "Maya", "Liam", "Zoe", "Arjun", "Priya", "Noah", "Emma", "Kenji", "Sofia"]
LAST_NAMES = ["Carter", "Singh", "Muller", "Dupont", "Silva", "Tanaka", "Nguyen", "Brown", "Khan", "Rossi"]


@dataclass(frozen=True)
class ScaleCounts:
    merchants: int
    customers: int
    accounts: int
    transactions: int
    payments: int
    settlements: int
    terminals: int
    cards: int
    refunds: int
    chargebacks: int


def _preset(merchants: int, customers: int, accounts: int, transactions: int, payments: int, settlements: int) -> ScaleCounts:
    return ScaleCounts(
        merchants=merchants,
        customers=customers,
        accounts=accounts,
        transactions=transactions,
        payments=payments,
        settlements=settlements,
        terminals=max(1, merchants * 2),
        cards=max(1, customers),
        refunds=max(1, payments // 20),
        chargebacks=max(1, transactions // 100),
    )


# CLAUDE.md section 13: minimum development dataset.
SCALE_PRESETS: dict[str, ScaleCounts] = {
    "full": _preset(merchants=10_000, customers=50_000, accounts=100_000, transactions=500_000, payments=100_000, settlements=100_000),
    "small": _preset(merchants=50, customers=200, accounts=300, transactions=1_000, payments=300, settlements=300),
}


def _random_datetime(rng: random.Random, days_span: int = 700) -> datetime:
    return EPOCH + timedelta(
        days=rng.randint(0, days_span),
        seconds=rng.randint(0, 86_399),
    )


def _random_date(rng: random.Random, days_span: int = 700) -> date:
    return (EPOCH + timedelta(days=rng.randint(0, days_span))).date()


def seed_database(conn: duckdb.DuckDBPyConnection, scale: str = "full", seed: int = SEED) -> ScaleCounts:
    """Populate every payments table deterministically. Returns row counts used."""
    if scale not in SCALE_PRESETS:
        raise ValueError(f"Unknown scale '{scale}'. Expected one of {sorted(SCALE_PRESETS)}.")

    counts = SCALE_PRESETS[scale]
    rng = random.Random(seed)

    merchant_ids = _seed_merchants(conn, rng, counts.merchants)
    customer_ids = _seed_customers(conn, rng, counts.customers)
    _seed_accounts(conn, rng, counts.accounts, merchant_ids)
    terminal_ids = _seed_terminals(conn, rng, counts.terminals, merchant_ids)
    _seed_cards(conn, rng, counts.cards, customer_ids)
    _seed_settlements(conn, rng, counts.settlements, merchant_ids)
    transaction_rows = _seed_transactions(conn, rng, counts.transactions, merchant_ids, customer_ids, terminal_ids)
    payment_rows = _seed_payments(conn, rng, counts.payments, transaction_rows)
    _seed_refunds(conn, rng, counts.refunds, payment_rows)
    _seed_chargebacks(conn, rng, counts.chargebacks, transaction_rows)

    return counts


def _seed_merchants(conn, rng: random.Random, n: int) -> list[str]:
    ids = [f"MER-{i:07d}" for i in range(1, n + 1)]
    rows = []
    for mid in ids:
        country, currency = rng.choice(COUNTRY_CURRENCY)
        name = f"{rng.choice(NAME_PARTS_1)} {rng.choice(NAME_PARTS_2)}"
        rows.append((
            mid,
            name,
            f"{name} Pvt Ltd",
            country,
            currency,
            rng.choice(MERCHANT_TYPES),
            rng.choice(MERCHANT_STATUSES),
            _random_datetime(rng),
        ))
    _bulk_insert(conn, "merchant", rows)
    return ids


def _seed_customers(conn, rng: random.Random, n: int) -> list[str]:
    ids = [f"CUS-{i:07d}" for i in range(1, n + 1)]
    rows = []
    for cid in ids:
        country, _ = rng.choice(COUNTRY_CURRENCY)
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        rows.append((
            cid,
            f"{first} {last}",
            country,
            f"{first.lower()}.{last.lower()}{rng.randint(1, 999)}@example.com",
            rng.choice(CUSTOMER_STATUSES),
            _random_datetime(rng),
        ))
    _bulk_insert(conn, "customer", rows)
    return ids


def _seed_accounts(conn, rng: random.Random, n: int, merchant_ids: list[str]) -> None:
    rows = []
    for i in range(1, n + 1):
        merchant_id = rng.choice(merchant_ids)
        currency = rng.choice(COUNTRY_CURRENCY)[1]
        ledger = round(rng.uniform(0, 250_000), 2)
        available = round(ledger * rng.uniform(0.5, 1.0), 2)
        rows.append((
            f"ACC-{i:08d}",
            merchant_id,
            ledger,
            available,
            currency,
            rng.choice(ACCOUNT_STATUSES),
            _random_datetime(rng),
        ))
    _bulk_insert(conn, "account", rows)


def _seed_terminals(conn, rng: random.Random, n: int, merchant_ids: list[str]) -> list[str]:
    ids = [f"TRM-{i:07d}" for i in range(1, n + 1)]
    rows = [
        (tid, rng.choice(merchant_ids), rng.choice(TERMINAL_TYPES), "active", _random_datetime(rng))
        for tid in ids
    ]
    _bulk_insert(conn, "terminal", rows)
    return ids


def _seed_cards(conn, rng: random.Random, n: int, customer_ids: list[str]) -> None:
    rows = []
    for i in range(1, n + 1):
        rows.append((
            f"CRD-{i:07d}",
            rng.choice(customer_ids),
            rng.choice(CARD_BRANDS),
            f"{rng.randint(0, 9999):04d}",
            "active",
            _random_datetime(rng),
        ))
    _bulk_insert(conn, "card", rows)


def _seed_settlements(conn, rng: random.Random, n: int, merchant_ids: list[str]) -> None:
    rows = []
    for i in range(1, n + 1):
        merchant_id = rng.choice(merchant_ids)
        currency = rng.choice(COUNTRY_CURRENCY)[1]
        amount = round(rng.uniform(100, 500_000), 2)
        balance = round(amount * rng.uniform(0, 1), 2)
        rows.append((
            f"STL-{i:07d}",
            merchant_id,
            amount,
            balance,
            currency,
            rng.choice(SETTLEMENT_STATUSES),
            _random_date(rng),
            _random_datetime(rng),
        ))
    _bulk_insert(conn, "settlement", rows)


def _seed_transactions(conn, rng: random.Random, n: int, merchant_ids, customer_ids, terminal_ids) -> list[tuple[str, str]]:
    ids: list[tuple[str, str]] = []  # (transaction_id, merchant_id)
    rows = []
    for i in range(1, n + 1):
        tid = f"TXN-{i:08d}"
        merchant_id = rng.choice(merchant_ids)
        currency = rng.choice(COUNTRY_CURRENCY)[1]
        rows.append((
            tid,
            merchant_id,
            rng.choice(customer_ids),
            rng.choice(terminal_ids),
            rng.choice(TRANSACTION_TYPES),
            round(rng.uniform(1, 20_000), 2),
            currency,
            rng.choice(TRANSACTION_STATUSES),
            _random_datetime(rng),
        ))
        ids.append((tid, merchant_id))
    _bulk_insert(conn, "transaction", rows)
    return ids


def _seed_payments(conn, rng: random.Random, n: int, transactions: list[tuple[str, str]]) -> list[tuple[str, str]]:
    ids: list[tuple[str, str]] = []  # (payment_id, merchant_id)
    rows = []
    for i in range(1, n + 1):
        pid = f"PAY-{i:08d}"
        txn_id, merchant_id = rng.choice(transactions)
        currency = rng.choice(COUNTRY_CURRENCY)[1]
        rows.append((
            pid,
            txn_id,
            merchant_id,
            round(rng.uniform(1, 20_000), 2),
            currency,
            rng.choice(PAYMENT_STATUSES),
            _random_datetime(rng),
        ))
        ids.append((pid, merchant_id))
    _bulk_insert(conn, "payment", rows)
    return ids


def _seed_refunds(conn, rng: random.Random, n: int, payments: list[tuple[str, str]]) -> None:
    rows = []
    for i in range(1, n + 1):
        payment_id, merchant_id = rng.choice(payments)
        currency = rng.choice(COUNTRY_CURRENCY)[1]
        rows.append((
            f"REF-{i:07d}",
            payment_id,
            merchant_id,
            round(rng.uniform(1, 5_000), 2),
            currency,
            rng.choice(REFUND_STATUSES),
            _random_datetime(rng),
        ))
    _bulk_insert(conn, "refund", rows)


def _seed_chargebacks(conn, rng: random.Random, n: int, transactions: list[tuple[str, str]]) -> None:
    rows = []
    for i in range(1, n + 1):
        txn_id, merchant_id = rng.choice(transactions)
        currency = rng.choice(COUNTRY_CURRENCY)[1]
        rows.append((
            f"CHB-{i:07d}",
            txn_id,
            merchant_id,
            round(rng.uniform(1, 10_000), 2),
            currency,
            rng.choice(CHARGEBACK_STATUSES),
            _random_datetime(rng),
        ))
    _bulk_insert(conn, "chargeback", rows)
