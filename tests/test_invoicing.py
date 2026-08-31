from datetime import UTC, date, datetime
from pathlib import Path

from metered_billing.invoicing import InvoiceService
from metered_billing.ledger import LedgerStore
from metered_billing.models import PricingSpec, UsageEvent
from metered_billing.pricing import load_pricing_spec
from metered_billing.rating import RatingEngine

ROOT = Path(__file__).parents[1]
PRICING = ROOT / "examples" / "pricing.yaml"
START = date(2026, 8, 1)
END = date(2026, 8, 31)


def event(quantity: int = 2) -> UsageEvent:
    return UsageEvent(
        event_id="usage_001",
        idempotency_key="request-001",
        customer_id="acme",
        product_id="agent_task",
        quantity=quantity,
        occurred_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )


def no_credit_spec(*, commitment: bool = False) -> PricingSpec:
    data = load_pricing_spec(PRICING).model_dump(mode="python")
    data["credit_buckets"] = []
    data["recharge_policies"] = []
    data["discounts"] = []
    if not commitment:
        data["monthly_commitments"] = []
    return PricingSpec.model_validate(data)


def build_invoice(store: LedgerStore, spec: PricingSpec):
    RatingEngine(store, spec).rate_period(
        customer_id="acme", period_start=START, period_end=END
    )
    return InvoiceService(store).generate(
        customer_id="acme", currency="USD", period_start=START, period_end=END
    )


def test_generates_traceable_overage_invoice(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(event())

        invoice = build_invoice(store, no_credit_spec())

        assert invoice.total_minor == 500
        assert len(invoice.lines) == 1
        line = invoice.lines[0]
        assert line.usage_event_ids == ("usage_001",)
        assert line.price_id == "task_v1"
        assert "charge_usage_001" in line.ledger_entry_ids


def test_json_and_markdown_are_stable_and_consistent(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(event())
        service = InvoiceService(store)
        first = build_invoice(store, no_credit_spec())
        second = service.generate(
            customer_id="acme", currency="USD", period_start=START, period_end=END
        )

        assert service.audit_report_json(first) == service.audit_report_json(second)
        markdown = service.render_markdown(first)
        assert "**Total: USD 5.00**" in markdown
        assert "Evidence total: USD 5.00" in markdown
        assert "Matched: yes" in markdown


def test_reconciliation_detects_changed_evidence(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(event())
        invoice = build_invoice(store, no_credit_spec())
        store.connection.execute(
            "UPDATE rated_events SET overage_minor = overage_minor + 1 WHERE event_id = ?",
            ("usage_001",),
        )

        result = InvoiceService(store).reconcile(invoice)

        assert result.matched is False
        assert result.difference_minor == 1


def test_includes_commitment_shortfall_evidence(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(event())

        invoice = build_invoice(store, no_credit_spec(commitment=True))

        assert invoice.total_minor == 5000
        assert {line.amount_minor for line in invoice.lines} == {500, 4500}
        commitment = next(line for line in invoice.lines if "Commitment" in line.description)
        assert commitment.ledger_entry_ids[0].startswith("commitment_acme_commit")


def test_includes_auto_recharge_as_a_traceable_line(tmp_path: Path) -> None:
    data = load_pricing_spec(PRICING).model_dump(mode="python")
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
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(event())

        invoice = build_invoice(store, PricingSpec.model_validate(data))

        assert invoice.total_minor == 400
        recharge = next(line for line in invoice.lines if "Auto-recharge" in line.description)
        assert recharge.amount_minor == 300
        assert recharge.ledger_entry_ids[0].startswith("recharge_acme_recharge")


def test_zero_payable_usage_produces_zero_invoice(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(event())

        invoice = build_invoice(store, load_pricing_spec(PRICING))

        assert invoice.total_minor == 5000
        assert all(line.usage_event_ids == () for line in invoice.lines)
