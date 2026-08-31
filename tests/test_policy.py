from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from metered_billing.ledger import IdempotencyConflictError, LedgerStore
from metered_billing.models import (
    PolicyOutcome,
    RiskLevel,
    SpendPolicySpec,
    UsageEvent,
)
from metered_billing.policy import PolicyError, SpendPolicyEngine, load_spend_policy
from metered_billing.pricing import load_pricing_spec
from metered_billing.rating import RatingEngine

ROOT = Path(__file__).parents[1]
PRICING = ROOT / "examples" / "pricing.yaml"
POLICIES = ROOT / "examples" / "policies.yaml"


def event(
    event_id: str,
    *,
    quantity: int = 1,
    day: int = 1,
    risk: RiskLevel = RiskLevel.LOW,
) -> UsageEvent:
    return UsageEvent(
        event_id=event_id,
        idempotency_key=f"request-{event_id}",
        customer_id="acme",
        product_id="agent_task",
        quantity=quantity,
        occurred_at=datetime(2026, 8, day, 12, tzinfo=UTC),
        agent_id="triage_agent",
        workflow_id="repo_triage",
        risk_level=risk,
    )


def policy_with_limit(limit: int, approval: int | None = None) -> SpendPolicySpec:
    return SpendPolicySpec.model_validate(
        {
            "schema_version": 1,
            "limits": [
                {
                    "id": "agent_daily",
                    "customer_id": "acme",
                    "currency": "USD",
                    "scope": "agent",
                    "subject_id": "triage_agent",
                    "daily_limit_minor": limit,
                    "approval_above_minor": approval,
                }
            ],
            "hold_risk_levels": ["high"],
        }
    )


def engine(store: LedgerStore, policy: SpendPolicySpec) -> SpendPolicyEngine:
    return SpendPolicyEngine(store, load_pricing_spec(PRICING), policy)


def test_loads_policy_fixture() -> None:
    policy = load_spend_policy(POLICIES)

    assert [limit.id for limit in policy.limits] == [
        "acme_daily",
        "triage_agent_daily",
        "repo_triage_daily",
    ]


def test_allows_usage_within_authority_idempotently(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(event("usage_001"))
        policy_engine = engine(store, policy_with_limit(1000))

        first = policy_engine.evaluate("usage_001")
        second = policy_engine.evaluate("usage_001")

        assert first == second
        assert first.outcome == PolicyOutcome.ALLOW
        assert first.estimated_minor == 225


def test_holds_high_risk_until_human_approval(tmp_path: Path) -> None:
    pricing = load_pricing_spec(PRICING)
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(event("usage_001", risk=RiskLevel.HIGH))
        policy_engine = SpendPolicyEngine(store, pricing, policy_with_limit(1000))

        decision = policy_engine.evaluate("usage_001")
        before = RatingEngine(store, pricing).rate_period(
            customer_id="acme", period_start=date(2026, 8, 1), period_end=date(2026, 8, 31)
        )
        approved = policy_engine.approve_held_event(
            event_id="usage_001",
            approval_id="approval_001",
            actor="reviewer@example.test",
            approve=True,
            reason="Reviewed synthetic high-risk task",
        )
        after = RatingEngine(store, pricing).rate_period(
            customer_id="acme", period_start=date(2026, 8, 1), period_end=date(2026, 8, 31)
        )

        assert decision.outcome == PolicyOutcome.HOLD
        assert before.rated_events == 0
        assert approved is True
        assert after.rated_events == 1


def test_denies_runaway_usage_without_mutating_credits(tmp_path: Path) -> None:
    pricing = load_pricing_spec(PRICING)
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(event("usage_001", quantity=10))
        decision = SpendPolicyEngine(store, pricing, policy_with_limit(1000)).evaluate(
            "usage_001"
        )
        result = RatingEngine(store, pricing).rate_period(
            customer_id="acme", period_start=date(2026, 8, 1), period_end=date(2026, 8, 31)
        )

        assert decision.outcome == PolicyOutcome.DENY
        assert result.rated_events == 0
        assert store.credit_balance("acme", "USD") == 3500


def test_strictest_overlapping_limit_denies(tmp_path: Path) -> None:
    policy = SpendPolicySpec.model_validate(
        {
            "schema_version": 1,
            "limits": [
                {
                    "id": "customer_daily",
                    "customer_id": "acme",
                    "currency": "USD",
                    "scope": "customer",
                    "daily_limit_minor": 10000,
                },
                {
                    "id": "agent_daily",
                    "customer_id": "acme",
                    "currency": "USD",
                    "scope": "agent",
                    "subject_id": "triage_agent",
                    "daily_limit_minor": 200,
                },
            ],
        }
    )
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(event("usage_001"))

        decision = engine(store, policy).evaluate("usage_001")

        assert decision.outcome == PolicyOutcome.DENY
        assert decision.reason_codes == ("limit_exceeded:agent_daily",)


def test_daily_limit_resets_on_next_day(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_events(
            [event("usage_001", quantity=2, day=1), event("usage_002", quantity=2, day=2)]
        )
        policy_engine = engine(store, policy_with_limit(500))

        first = policy_engine.evaluate("usage_001")
        second = policy_engine.evaluate("usage_002")

        assert first.outcome == PolicyOutcome.ALLOW
        assert second.outcome == PolicyOutcome.ALLOW


def test_cumulative_daily_reservation_stops_runaway_agent(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_events(
            [event("usage_001", quantity=2), event("usage_002", quantity=2)]
        )
        policy_engine = engine(store, policy_with_limit(500))

        first = policy_engine.evaluate("usage_001")
        second = policy_engine.evaluate("usage_002")

        assert first.outcome == PolicyOutcome.ALLOW
        assert second.outcome == PolicyOutcome.DENY
        assert second.reason_codes == ("limit_exceeded:agent_daily",)


def test_approval_is_idempotent_and_conflicts_are_rejected(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(event("usage_001", risk=RiskLevel.HIGH))
        policy_engine = engine(store, policy_with_limit(1000))
        policy_engine.evaluate("usage_001")
        arguments = {
            "event_id": "usage_001",
            "approval_id": "approval_001",
            "actor": "reviewer@example.test",
            "approve": True,
            "reason": "Reviewed",
        }

        assert policy_engine.approve_held_event(**arguments) is True
        assert policy_engine.approve_held_event(**arguments) is False
        with pytest.raises(IdempotencyConflictError):
            policy_engine.approve_held_event(**{**arguments, "reason": "Changed"})


def test_cannot_approve_a_denied_event(tmp_path: Path) -> None:
    with LedgerStore(tmp_path / "billing.sqlite") as store:
        store.ingest_usage_event(event("usage_001", quantity=10))
        policy_engine = engine(store, policy_with_limit(1000))
        policy_engine.evaluate("usage_001")

        with pytest.raises(PolicyError, match="not awaiting approval"):
            policy_engine.approve_held_event(
                event_id="usage_001",
                approval_id="approval_001",
                actor="reviewer@example.test",
                approve=True,
                reason="Should fail",
            )
