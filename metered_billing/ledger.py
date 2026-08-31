"""Transactional SQLite ledger for usage, credits, and billing evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from metered_billing.models import CreditBucket, LedgerEntryKind, UsageEvent


class LedgerError(RuntimeError):
    """Base error for ledger operations."""


class IdempotencyConflictError(LedgerError):
    """Raised when a stable identifier is reused for different data."""


class LedgerStore:
    """Own the SQLite schema and atomic mutations for one billing ledger."""

    SCHEMA_VERSION = 2

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def __enter__(self) -> LedgerStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a mutation batch atomically."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _migrate(self) -> None:
        current = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if current > self.SCHEMA_VERSION:
            raise LedgerError(
                f"database schema {current} is newer than supported {self.SCHEMA_VERSION}"
            )
        if current == 0:
            with self.transaction() as connection:
                connection.executescript(
                    """
                    CREATE TABLE usage_events (
                        event_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        payload_hash TEXT NOT NULL,
                        customer_id TEXT NOT NULL,
                        product_id TEXT NOT NULL,
                        quantity INTEGER NOT NULL CHECK (quantity > 0),
                        occurred_at TEXT NOT NULL,
                        agent_id TEXT,
                        workflow_id TEXT,
                        risk_level TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'accepted',
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE credit_buckets (
                        bucket_id TEXT PRIMARY KEY,
                        customer_id TEXT NOT NULL,
                        currency TEXT NOT NULL,
                        product_ids_json TEXT NOT NULL,
                        valid_from TEXT NOT NULL,
                        valid_to TEXT,
                        balance_minor INTEGER NOT NULL CHECK (balance_minor >= 0),
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE ledger_entries (
                        entry_id TEXT PRIMARY KEY,
                        customer_id TEXT NOT NULL,
                        currency TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        amount_minor INTEGER NOT NULL,
                        occurred_at TEXT NOT NULL,
                        source_event_id TEXT,
                        credit_bucket_id TEXT,
                        description TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        FOREIGN KEY (source_event_id) REFERENCES usage_events(event_id),
                        FOREIGN KEY (credit_bucket_id) REFERENCES credit_buckets(bucket_id)
                    );

                    CREATE INDEX usage_events_customer_time
                    ON usage_events(customer_id, occurred_at);
                    CREATE INDEX ledger_entries_customer_time
                    ON ledger_entries(customer_id, occurred_at);
                    """
                )
                connection.execute("PRAGMA user_version = 1")
            current = 1
        if current < 2:
            with self.transaction() as connection:
                connection.executescript(
                    """
                    CREATE TABLE rated_events (
                        event_id TEXT PRIMARY KEY,
                        price_id TEXT NOT NULL,
                        currency TEXT NOT NULL,
                        gross_minor INTEGER NOT NULL CHECK (gross_minor >= 0),
                        discount_minor INTEGER NOT NULL CHECK (discount_minor >= 0),
                        credit_minor INTEGER NOT NULL CHECK (credit_minor >= 0),
                        overage_minor INTEGER NOT NULL CHECK (overage_minor >= 0),
                        period_start TEXT NOT NULL,
                        period_end TEXT NOT NULL,
                        rated_at TEXT NOT NULL,
                        FOREIGN KEY (event_id) REFERENCES usage_events(event_id)
                    );

                    CREATE INDEX rated_events_period
                    ON rated_events(period_start, period_end, currency);
                    """
                )
                connection.execute("PRAGMA user_version = 2")

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _usage_fingerprint(event: UsageEvent) -> str:
        payload = event.model_dump(mode="json", exclude={"event_id", "idempotency_key"})
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def ingest_usage_event(self, event: UsageEvent) -> bool:
        """Insert one event, returning false for an exact replay."""
        return self.ingest_usage_events([event]) == 1

    def ingest_usage_events(self, events: Iterable[UsageEvent]) -> int:
        """Insert a batch exactly once and roll the whole batch back on conflict."""
        inserted = 0
        with self.transaction() as connection:
            for event in events:
                fingerprint = self._usage_fingerprint(event)
                by_id = connection.execute(
                    "SELECT idempotency_key, payload_hash FROM usage_events WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
                if by_id is not None:
                    if (
                        by_id["idempotency_key"] == event.idempotency_key
                        and by_id["payload_hash"] == fingerprint
                    ):
                        continue
                    raise IdempotencyConflictError(
                        f"event_id {event.event_id} was reused for different usage"
                    )

                by_key = connection.execute(
                    "SELECT payload_hash FROM usage_events WHERE idempotency_key = ?",
                    (event.idempotency_key,),
                ).fetchone()
                if by_key is not None:
                    if by_key["payload_hash"] == fingerprint:
                        continue
                    raise IdempotencyConflictError(
                        f"idempotency_key {event.idempotency_key!r} was reused for different usage"
                    )

                connection.execute(
                    """
                    INSERT INTO usage_events (
                        event_id, idempotency_key, payload_hash, customer_id, product_id,
                        quantity, occurred_at, agent_id, workflow_id, risk_level,
                        metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.idempotency_key,
                        fingerprint,
                        event.customer_id,
                        event.product_id,
                        event.quantity,
                        event.occurred_at.isoformat(),
                        event.agent_id,
                        event.workflow_id,
                        event.risk_level.value,
                        json.dumps(event.metadata, sort_keys=True, separators=(",", ":")),
                        self._utc_now(),
                    ),
                )
                inserted += 1
        return inserted

    def grant_credit(
        self,
        *,
        grant_id: str,
        bucket: CreditBucket,
        amount_minor: int | None = None,
        occurred_at: datetime | None = None,
    ) -> bool:
        """Grant credit exactly once and create the bucket on its first grant."""
        amount = bucket.initial_balance_minor if amount_minor is None else amount_minor
        if amount <= 0:
            raise LedgerError("credit grant must be positive")
        timestamp = occurred_at or datetime.now(UTC)
        with self.transaction() as connection:
            existing_entry = connection.execute(
                """
                SELECT amount_minor, credit_bucket_id, customer_id, currency
                FROM ledger_entries WHERE entry_id = ?
                """,
                (grant_id,),
            ).fetchone()
            if existing_entry is not None:
                expected = (amount, bucket.id, bucket.customer_id, bucket.currency)
                actual = tuple(existing_entry)
                if actual == expected:
                    return False
                raise IdempotencyConflictError(
                    f"grant_id {grant_id} was reused for a different credit grant"
                )

            existing_bucket = connection.execute(
                "SELECT * FROM credit_buckets WHERE bucket_id = ?", (bucket.id,)
            ).fetchone()
            scope_json = json.dumps(sorted(bucket.product_ids), separators=(",", ":"))
            if existing_bucket is None:
                connection.execute(
                    """
                    INSERT INTO credit_buckets (
                        bucket_id, customer_id, currency, product_ids_json, valid_from,
                        valid_to, balance_minor, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        bucket.id,
                        bucket.customer_id,
                        bucket.currency,
                        scope_json,
                        bucket.valid_from.isoformat(),
                        bucket.valid_to.isoformat() if bucket.valid_to else None,
                        self._utc_now(),
                    ),
                )
            else:
                expected_bucket = (
                    bucket.customer_id,
                    bucket.currency,
                    scope_json,
                    bucket.valid_from.isoformat(),
                    bucket.valid_to.isoformat() if bucket.valid_to else None,
                )
                actual_bucket = (
                    existing_bucket["customer_id"],
                    existing_bucket["currency"],
                    existing_bucket["product_ids_json"],
                    existing_bucket["valid_from"],
                    existing_bucket["valid_to"],
                )
                if actual_bucket != expected_bucket:
                    raise IdempotencyConflictError(
                        f"bucket_id {bucket.id} was reused with different attributes"
                    )

            connection.execute(
                "UPDATE credit_buckets SET balance_minor = balance_minor + ? WHERE bucket_id = ?",
                (amount, bucket.id),
            )
            connection.execute(
                """
                INSERT INTO ledger_entries (
                    entry_id, customer_id, currency, kind, amount_minor, occurred_at,
                    credit_bucket_id, description, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grant_id,
                    bucket.customer_id,
                    bucket.currency,
                    LedgerEntryKind.CREDIT_GRANT.value,
                    amount,
                    timestamp.isoformat(),
                    bucket.id,
                    f"Credit grant to {bucket.id}",
                    "{}",
                ),
            )
        return True

    def credit_balance(self, customer_id: str, currency: str) -> int:
        row = self.connection.execute(
            """
            SELECT COALESCE(SUM(balance_minor), 0) AS balance
            FROM credit_buckets WHERE customer_id = ? AND currency = ?
            """,
            (customer_id, currency),
        ).fetchone()
        return int(row["balance"])

    def usage_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0])

    def ledger_entries(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM ledger_entries ORDER BY occurred_at, entry_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def schema_version(self) -> int:
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def credit_buckets(
        self,
        *,
        customer_id: str,
        currency: str,
        on_date: date | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT * FROM credit_buckets
            WHERE customer_id = ? AND currency = ?
        """
        params: list[Any] = [customer_id, currency]
        if on_date is not None:
            query += " AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)"
            params.extend((on_date.isoformat(), on_date.isoformat()))
        query += " ORDER BY bucket_id"
        return [dict(row) for row in self.connection.execute(query, params).fetchall()]
