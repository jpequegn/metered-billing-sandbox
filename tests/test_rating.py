from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from metered_billing.ledger import LedgerStore
from metered_billing.models import PricingSpec, UsageEvent
from metered_billing.pricing import load_pricing_spec
from metered_billing.rating import RatingEngine

FIXTURE = Path(__file__).parents[1] / "examples" / "pricing.yaml"
PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 8, 31)


def usage(
    event_id: str = "usage_001",
    *,
    product_id: str = "agent_task",
    quantity: int = 2,
    day: int = 1,
) -> UsageEvent:
    return UsageEvent(
        event_id=event_id,
        idempotency_key=f"request-{event_id}",
        customer_id="acme",
        product_id=product_id,
        quantity=quantity,
        occurred_at=datetime(2026, 8, day, 12, tzinfo=UTC),
    )


def spec_with(**changes) -> PricingSpec:
    data = load_pricing_spec(FIXTURE).model_dump(mode="python")
    data.update(changes)
    return PricingSpec.model_validate(data)


def rate(store: LedgerStore, spec: PricingSpec):
    return RatingEngine(store, spec).rate_period(
        customer_id="acme", period_start=PERIOD_START, period_end=PERIOD_END
    )


def test_rates_discount_and_scoped_credit(tmp_path: Path) -> None:
    spec = load_pricing_spec(FIXTURE)
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(usage())

        result = rate(store, spec)

        assert result.rated_events == 1
        assert result.gross_minor == 500
        assert result.discount_minor == 50
        assert result.credit_minor == 450
        assert result.overage_minor == 0
        buckets = {item["bucket_id"]: item for item in store.credit_buckets(
            customer_id="acme", currency="USD"
        )}
        assert buckets["task_credits"]["balance_minor"] == 2050
        assert buckets["general_credits"]["balance_minor"] == 1000


def test_rounds_fractional_units_up_in_minor_currency(tmp_path: Path) -> None:
    spec = spec_with(credit_buckets=[], recharge_policies=[], monthly_commitments=[])
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(usage(product_id="input_tokens", quantity=1500))

        result = rate(store, spec)

        assert result.gross_minor == 8
        assert result.overage_minor == 8


def test_uses_price_effective_on_event_date(tmp_path: Path) -> None:
    data = load_pricing_spec(FIXTURE).model_dump(mode="python")
    data["credit_buckets"] = []
    data["recharge_policies"] = []
    data["monthly_commitments"] = []
    data["discounts"] = []
    first = dict(data["prices"][0])
    first["effective_to"] = date(2026, 8, 14)
    second = dict(first)
    second.update(id="task_v2", unit_amount_minor=400, effective_from=date(2026, 8, 15))
    second["effective_to"] = None
    data["prices"] = [first, second, data["prices"][1]]
    spec = PricingSpec.model_validate(data)
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_events(
            [usage("usage_early", quantity=1, day=14), usage("usage_late", quantity=1, day=15)]
        )

        result = rate(store, spec)

        assert result.gross_minor == 650


def test_auto_recharges_then_applies_new_credit(tmp_path: Path) -> None:
    data = load_pricing_spec(FIXTURE).model_dump(mode="python")
    data["credit_buckets"] = [
        {
            "id": "general_credits",
            "customer_id": "acme",
            "currency": "USD",
            "initial_balance_minor": 100,
            "product_ids": [],
            "valid_from": date(2026, 1, 1),
        }
    ]
    data["recharge_policies"] = [
        {
            "id": "acme_recharge",
            "customer_id": "acme",
            "currency": "USD",
            "threshold_minor": 50,
            "recharge_amount_minor": 300,
            "max_recharges_per_period": 1,
        }
    ]
    data["monthly_commitments"] = []
    data["discounts"] = []
    spec = PricingSpec.model_validate(data)
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(usage(quantity=2))

        result = rate(store, spec)

        assert result.gross_minor == 500
        assert result.recharge_minor == 300
        assert result.credit_minor == 400
        assert result.overage_minor == 100


def test_applies_monthly_commitment_shortfall_once(tmp_path: Path) -> None:
    spec = spec_with(credit_buckets=[], recharge_policies=[], discounts=[])
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(usage(quantity=2))

        first = rate(store, spec)
        entries_after_first = len(store.ledger_entries())
        second = rate(store, spec)

        assert first.overage_minor == 500
        assert first.commitment_minor == 4500
        assert second.rated_events == 0
        assert second.commitment_minor == 0
        assert len(store.ledger_entries()) == entries_after_first


def test_insertion_order_does_not_change_period_result(tmp_path: Path) -> None:
    spec = load_pricing_spec(FIXTURE)
    events = [
        usage("usage_002", product_id="input_tokens", quantity=200_000, day=2),
        usage("usage_001", product_id="agent_task", quantity=8, day=1),
    ]
    results = []
    balances = []
    for index, ordered in enumerate((events, list(reversed(events)))):
        with LedgerStore(tmp_path / f"billing-{index}.sqlite") as store:
            store.ingest_usage_events(ordered)
            results.append(rate(store, spec))
            balances.append(store.credit_balance("acme", "USD"))

    assert results[0] == results[1]
    assert balances[0] == balances[1]
