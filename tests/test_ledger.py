from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from metered_billing.ledger import IdempotencyConflictError, LedgerStore
from metered_billing.models import CreditBucket, UsageEvent


def usage(**changes) -> UsageEvent:
    values = {
        "event_id": "usage_001",
        "idempotency_key": "request-001",
        "customer_id": "acme",
        "product_id": "agent_task",
        "quantity": 2,
        "occurred_at": datetime(2026, 8, 1, 12, tzinfo=UTC),
        "agent_id": "triage_agent",
        "workflow_id": "repo_triage",
    }
    values.update(changes)
    return UsageEvent.model_validate(values)


def bucket() -> CreditBucket:
    return CreditBucket(
        id="task_credits",
        customer_id="acme",
        currency="USD",
        initial_balance_minor=2500,
        product_ids=("agent_task",),
        valid_from=date(2026, 1, 1),
    )


def test_initializes_and_reopens_database(tmp_path: Path) -> None:
    path = tmp_path / "billing.sqlite"
    with LedgerStore(path) as store:
        assert store.schema_version() == 1
        assert store.usage_count() == 0

    with LedgerStore(path) as reopened:
        assert reopened.schema_version() == 1


def test_ingests_usage_exactly_once(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        assert store.ingest_usage_event(usage()) is True
        assert store.ingest_usage_event(usage()) is False
        assert store.usage_count() == 1


def test_same_idempotency_key_and_payload_is_a_replay(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(usage())

        assert store.ingest_usage_event(usage(event_id="usage_002")) is False
        assert store.usage_count() == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"quantity": 3},
        {"product_id": "input_tokens"},
    ],
)
def test_rejects_conflicting_idempotency_key(tmp_path: Path, changed: dict) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(usage())

        with pytest.raises(IdempotencyConflictError, match="idempotency_key"):
            store.ingest_usage_event(usage(event_id="usage_002", **changed))


def test_rejects_event_id_reuse(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(usage())

        with pytest.raises(IdempotencyConflictError, match="event_id"):
            store.ingest_usage_event(usage(idempotency_key="request-002", quantity=3))


def test_batch_rolls_back_on_conflict(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(usage())
        first_new = usage(event_id="usage_002", idempotency_key="request-002")
        conflict = usage(event_id="usage_003", idempotency_key="request-001", quantity=99)

        with pytest.raises(IdempotencyConflictError):
            store.ingest_usage_events([first_new, conflict])

        assert store.usage_count() == 1


def test_grants_credit_and_preserves_balance_on_replay(tmp_path: Path) -> None:
    path = tmp_path / "billing.sqlite"
    with LedgerStore(path) as store:
        assert store.grant_credit(grant_id="grant_001", bucket=bucket()) is True
        assert store.grant_credit(grant_id="grant_001", bucket=bucket()) is False
        assert store.credit_balance("acme", "USD") == 2500
        assert len(store.ledger_entries()) == 1

    with LedgerStore(path) as reopened:
        assert reopened.credit_balance("acme", "USD") == 2500


def test_rejects_conflicting_credit_grant(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.grant_credit(grant_id="grant_001", bucket=bucket())

        with pytest.raises(IdempotencyConflictError, match="grant_id"):
            store.grant_credit(grant_id="grant_001", bucket=bucket(), amount_minor=100)


def test_filters_credit_buckets_by_validity(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.grant_credit(grant_id="grant_001", bucket=bucket())

        current = store.credit_buckets(
            customer_id="acme", currency="USD", on_date=date(2026, 8, 1)
        )
        expired = store.credit_buckets(
            customer_id="acme", currency="USD", on_date=date(2025, 8, 1)
        )

        assert [item["bucket_id"] for item in current] == ["task_credits"]
        assert expired == []
