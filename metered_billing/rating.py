"""Deterministic period rating with credits, discounts, recharge, and commitments."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from metered_billing.ledger import LedgerError, LedgerStore
from metered_billing.models import (
    CreditBucket,
    Discount,
    LedgerEntryKind,
    Price,
    PricingSpec,
    RechargePolicy,
)


class RatingError(LedgerError):
    """Raised when accepted usage cannot be rated deterministically."""


@dataclass(frozen=True)
class RatingResult:
    rated_events: int
    gross_minor: int
    discount_minor: int
    credit_minor: int
    overage_minor: int
    recharge_minor: int
    commitment_minor: int


@dataclass(frozen=True)
class ChargeEstimate:
    price_id: str
    currency: str
    gross_minor: int
    discount_minor: int
    net_minor: int


class RatingEngine:
    def __init__(self, store: LedgerStore, spec: PricingSpec) -> None:
        self.store = store
        self.spec = spec

    def rate_period(
        self,
        *,
        customer_id: str,
        period_start: date,
        period_end: date,
    ) -> RatingResult:
        """Rate all accepted events in a closed date range exactly once."""
        if period_end < period_start:
            raise RatingError("period_end must not precede period_start")
        if customer_id not in {customer.id for customer in self.spec.customers}:
            raise RatingError(f"unknown customer {customer_id}")

        self._seed_credit_buckets(customer_id)
        start = datetime.combine(period_start, time.min, tzinfo=UTC)
        end_exclusive = datetime.combine(period_end + timedelta(days=1), time.min, tzinfo=UTC)
        totals = {
            "rated_events": 0,
            "gross_minor": 0,
            "discount_minor": 0,
            "credit_minor": 0,
            "overage_minor": 0,
            "recharge_minor": 0,
            "commitment_minor": 0,
        }

        with self.store.transaction() as connection:
            events = connection.execute(
                """
                SELECT * FROM usage_events
                WHERE customer_id = ? AND occurred_at >= ? AND occurred_at < ?
                  AND status = 'accepted'
                ORDER BY occurred_at, event_id
                """,
                (customer_id, start.isoformat(), end_exclusive.isoformat()),
            ).fetchall()
            for event in events:
                rated = self._rate_event(
                    connection,
                    event,
                    period_start=period_start,
                    period_end=period_end,
                )
                for key in (
                    "gross_minor",
                    "discount_minor",
                    "credit_minor",
                    "overage_minor",
                    "recharge_minor",
                ):
                    totals[key] += rated[key]
                totals["rated_events"] += 1

            totals["commitment_minor"] = self._apply_commitment(
                connection,
                customer_id=customer_id,
                period_start=period_start,
                period_end=period_end,
            )
        return RatingResult(**totals)

    def estimate_event(self, event) -> ChargeEstimate:
        """Estimate one event without mutating the ledger."""
        price = self._price_for(event.product_id, event.occurred_at.date())
        gross = self._round_charge(event.quantity, price)
        discount = self._discount_for(
            customer_id=event.customer_id,
            product_id=event.product_id,
            gross_minor=gross,
        )
        return ChargeEstimate(
            price_id=price.id,
            currency=price.currency,
            gross_minor=gross,
            discount_minor=discount,
            net_minor=gross - discount,
        )

    def _seed_credit_buckets(self, customer_id: str) -> None:
        for bucket in self.spec.credit_buckets:
            if bucket.customer_id != customer_id:
                continue
            if bucket.initial_balance_minor > 0:
                self.store.grant_credit(
                    grant_id=f"spec_grant_{bucket.id}",
                    bucket=bucket,
                    occurred_at=datetime.combine(bucket.valid_from, time.min, tzinfo=UTC),
                )
            else:
                self._ensure_empty_bucket(bucket)

    def _ensure_empty_bucket(self, bucket: CreditBucket) -> None:
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT bucket_id FROM credit_buckets WHERE bucket_id = ?", (bucket.id,)
            ).fetchone()
            if existing is None:
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
                        json.dumps(sorted(bucket.product_ids), separators=(",", ":")),
                        bucket.valid_from.isoformat(),
                        bucket.valid_to.isoformat() if bucket.valid_to else None,
                        datetime.now(UTC).isoformat(),
                    ),
                )

    def _rate_event(
        self,
        connection: sqlite3.Connection,
        event: sqlite3.Row,
        *,
        period_start: date,
        period_end: date,
    ) -> dict[str, int]:
        occurred_at = datetime.fromisoformat(event["occurred_at"])
        price = self._price_for(event["product_id"], occurred_at.date())
        gross = self._round_charge(event["quantity"], price)
        discount = self._discount_for(
            customer_id=event["customer_id"],
            product_id=event["product_id"],
            gross_minor=gross,
        )
        net = gross - discount

        self._insert_entry(
            connection,
            entry_id=f"charge_{event['event_id']}",
            customer_id=event["customer_id"],
            currency=price.currency,
            kind=LedgerEntryKind.CHARGE,
            amount_minor=gross,
            occurred_at=occurred_at,
            source_event_id=event["event_id"],
            description=f"Usage charge at price {price.id}",
            metadata={"price_id": price.id},
        )
        if discount:
            self._insert_entry(
                connection,
                entry_id=f"discount_{event['event_id']}",
                customer_id=event["customer_id"],
                currency=price.currency,
                kind=LedgerEntryKind.DISCOUNT,
                amount_minor=-discount,
                occurred_at=occurred_at,
                source_event_id=event["event_id"],
                description="Configured usage discount",
                metadata={"price_id": price.id},
            )

        credit = self._consume_credits(
            connection,
            event_id=event["event_id"],
            customer_id=event["customer_id"],
            product_id=event["product_id"],
            currency=price.currency,
            amount_minor=net,
            occurred_at=occurred_at,
        )
        remaining = net - credit
        recharge = self._maybe_recharge(
            connection,
            customer_id=event["customer_id"],
            currency=price.currency,
            occurred_at=occurred_at,
            period_start=period_start,
            period_end=period_end,
        )
        if remaining and recharge:
            extra_credit = self._consume_credits(
                connection,
                event_id=event["event_id"],
                customer_id=event["customer_id"],
                product_id=event["product_id"],
                currency=price.currency,
                amount_minor=remaining,
                occurred_at=occurred_at,
                suffix="recharge",
            )
            credit += extra_credit
            remaining -= extra_credit

        connection.execute(
            """
            INSERT INTO rated_events (
                event_id, price_id, currency, gross_minor, discount_minor,
                credit_minor, overage_minor, period_start, period_end, rated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_id"],
                price.id,
                price.currency,
                gross,
                discount,
                credit,
                remaining,
                period_start.isoformat(),
                period_end.isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.execute(
            "UPDATE usage_events SET status = 'rated' WHERE event_id = ?",
            (event["event_id"],),
        )
        return {
            "gross_minor": gross,
            "discount_minor": discount,
            "credit_minor": credit,
            "overage_minor": remaining,
            "recharge_minor": recharge,
        }

    def _price_for(self, product_id: str, on_date: date) -> Price:
        candidates = [
            price
            for price in self.spec.prices
            if price.product_id == product_id
            and price.effective_from <= on_date
            and (price.effective_to is None or price.effective_to >= on_date)
        ]
        if not candidates:
            raise RatingError(f"no effective price for {product_id} on {on_date}")
        return max(candidates, key=lambda item: (item.effective_from, item.id))

    @staticmethod
    def _round_charge(quantity: int, price: Price) -> int:
        numerator = quantity * price.unit_amount_minor
        return (numerator + price.unit_size - 1) // price.unit_size

    def _discount_for(self, *, customer_id: str, product_id: str, gross_minor: int) -> int:
        applicable: list[Discount] = [
            discount
            for discount in self.spec.discounts
            if discount.customer_id == customer_id
            and (not discount.product_ids or product_id in discount.product_ids)
        ]
        basis_points = max((item.basis_points for item in applicable), default=0)
        return gross_minor * basis_points // 10_000

    def _consume_credits(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: str,
        customer_id: str,
        product_id: str,
        currency: str,
        amount_minor: int,
        occurred_at: datetime,
        suffix: str = "initial",
    ) -> int:
        rows = connection.execute(
            """
            SELECT * FROM credit_buckets
            WHERE customer_id = ? AND currency = ? AND balance_minor > 0
              AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)
            """,
            (customer_id, currency, occurred_at.date().isoformat(), occurred_at.date().isoformat()),
        ).fetchall()
        eligible = []
        for row in rows:
            scope = tuple(json.loads(row["product_ids_json"]))
            if not scope or product_id in scope:
                eligible.append((0 if scope else 1, row["valid_to"] or "9999-12-31", row))
        eligible.sort(key=lambda item: (item[0], item[1], item[2]["bucket_id"]))

        consumed = 0
        for _, _, bucket in eligible:
            if consumed == amount_minor:
                break
            take = min(bucket["balance_minor"], amount_minor - consumed)
            if take == 0:
                continue
            connection.execute(
                "UPDATE credit_buckets SET balance_minor = balance_minor - ? WHERE bucket_id = ?",
                (take, bucket["bucket_id"]),
            )
            self._insert_entry(
                connection,
                entry_id=f"credit_{event_id}_{suffix}_{bucket['bucket_id']}",
                customer_id=customer_id,
                currency=currency,
                kind=LedgerEntryKind.CREDIT_CONSUMPTION,
                amount_minor=-take,
                occurred_at=occurred_at,
                source_event_id=event_id,
                credit_bucket_id=bucket["bucket_id"],
                description=f"Credit consumed from {bucket['bucket_id']}",
                metadata={},
            )
            consumed += take
        return consumed

    def _maybe_recharge(
        self,
        connection: sqlite3.Connection,
        *,
        customer_id: str,
        currency: str,
        occurred_at: datetime,
        period_start: date,
        period_end: date,
    ) -> int:
        policies: list[RechargePolicy] = [
            policy
            for policy in self.spec.recharge_policies
            if policy.customer_id == customer_id and policy.currency == currency
        ]
        if not policies:
            return 0
        policy = sorted(policies, key=lambda item: item.id)[0]
        balance = connection.execute(
            """
            SELECT COALESCE(SUM(balance_minor), 0) FROM credit_buckets
            WHERE customer_id = ? AND currency = ?
            """,
            (customer_id, currency),
        ).fetchone()[0]
        if balance > policy.threshold_minor:
            return 0

        period_key = f"{period_start.isoformat()}_{period_end.isoformat()}"
        prefix = f"recharge_{policy.id}_{period_key}_"
        count = connection.execute(
            "SELECT COUNT(*) FROM ledger_entries WHERE entry_id GLOB ?",
            (f"{prefix}*",),
        ).fetchone()[0]
        if count >= policy.max_recharges_per_period:
            return 0

        target = connection.execute(
            """
            SELECT * FROM credit_buckets
            WHERE customer_id = ? AND currency = ? AND product_ids_json = '[]'
              AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)
            ORDER BY bucket_id LIMIT 1
            """,
            (customer_id, currency, occurred_at.date().isoformat(), occurred_at.date().isoformat()),
        ).fetchone()
        if target is None:
            raise RatingError("auto-recharge requires an active unscoped credit bucket")

        entry_id = f"{prefix}{count + 1}"
        connection.execute(
            "UPDATE credit_buckets SET balance_minor = balance_minor + ? WHERE bucket_id = ?",
            (policy.recharge_amount_minor, target["bucket_id"]),
        )
        self._insert_entry(
            connection,
            entry_id=entry_id,
            customer_id=customer_id,
            currency=currency,
            kind=LedgerEntryKind.RECHARGE,
            amount_minor=policy.recharge_amount_minor,
            occurred_at=occurred_at,
            credit_bucket_id=target["bucket_id"],
            description=f"Auto-recharge from policy {policy.id}",
            metadata={"policy_id": policy.id, "period": period_key},
        )
        return policy.recharge_amount_minor

    def _apply_commitment(
        self,
        connection: sqlite3.Connection,
        *,
        customer_id: str,
        period_start: date,
        period_end: date,
    ) -> int:
        commitments = [
            commitment
            for commitment in self.spec.monthly_commitments
            if commitment.customer_id == customer_id
        ]
        adjustment_total = 0
        for commitment in commitments:
            overage = connection.execute(
                """
                SELECT COALESCE(SUM(overage_minor), 0) FROM rated_events
                WHERE event_id IN (
                    SELECT event_id FROM usage_events WHERE customer_id = ?
                ) AND currency = ? AND period_start = ? AND period_end = ?
                """,
                (
                    customer_id,
                    commitment.currency,
                    period_start.isoformat(),
                    period_end.isoformat(),
                ),
            ).fetchone()[0]
            shortfall = max(0, commitment.amount_minor - overage)
            entry_id = (
                f"commitment_{commitment.id}_{period_start.isoformat()}_{period_end.isoformat()}"
            )
            existing = connection.execute(
                "SELECT amount_minor FROM ledger_entries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            if existing is not None:
                continue
            if shortfall:
                self._insert_entry(
                    connection,
                    entry_id=entry_id,
                    customer_id=customer_id,
                    currency=commitment.currency,
                    kind=LedgerEntryKind.COMMITMENT,
                    amount_minor=shortfall,
                    occurred_at=datetime.combine(period_end, time.max, tzinfo=UTC),
                    description=f"Monthly commitment {commitment.id} shortfall",
                    metadata={
                        "commitment_id": commitment.id,
                        "period_start": period_start.isoformat(),
                        "period_end": period_end.isoformat(),
                    },
                )
            adjustment_total += shortfall
        return adjustment_total

    @staticmethod
    def _insert_entry(
        connection: sqlite3.Connection,
        *,
        entry_id: str,
        customer_id: str,
        currency: str,
        kind: LedgerEntryKind,
        amount_minor: int,
        occurred_at: datetime,
        description: str,
        metadata: dict,
        source_event_id: str | None = None,
        credit_bucket_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO ledger_entries (
                entry_id, customer_id, currency, kind, amount_minor, occurred_at,
                source_event_id, credit_bucket_id, description, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                customer_id,
                currency,
                kind.value,
                amount_minor,
                occurred_at.isoformat(),
                source_event_id,
                credit_bucket_id,
                description,
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            ),
        )
