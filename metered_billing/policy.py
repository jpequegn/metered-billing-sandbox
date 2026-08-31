"""Daily agent-spend policy evaluation and human approval persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from metered_billing.ledger import IdempotencyConflictError, LedgerError, LedgerStore
from metered_billing.models import (
    PolicyOutcome,
    PricingSpec,
    SpendLimit,
    SpendLimitScope,
    SpendPolicySpec,
    UsageEvent,
)
from metered_billing.rating import RatingEngine


class PolicyError(LedgerError):
    """Raised for invalid policy configuration or state transitions."""


@dataclass(frozen=True)
class SpendDecision:
    decision_id: str
    event_id: str
    outcome: PolicyOutcome
    reason_codes: tuple[str, ...]
    estimated_minor: int
    currency: str
    applicable_limit_ids: tuple[str, ...]


def load_spend_policy(path: str | Path) -> SpendPolicySpec:
    source = Path(path)
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"unable to read spend policy {source}: {exc}") from exc
    try:
        return SpendPolicySpec.model_validate(data)
    except ValidationError as exc:
        raise PolicyError(f"invalid spend policy: {exc}") from exc


class SpendPolicyEngine:
    """Evaluate accepted usage against reserved daily spend authority."""

    def __init__(
        self,
        store: LedgerStore,
        pricing_spec: PricingSpec,
        policy_spec: SpendPolicySpec,
    ) -> None:
        self.store = store
        self.pricing_spec = pricing_spec
        self.policy_spec = policy_spec
        self.rating = RatingEngine(store, pricing_spec)
        self._validate_policy_references()

    def _validate_policy_references(self) -> None:
        customers = {customer.id for customer in self.pricing_spec.customers}
        currencies = {price.currency for price in self.pricing_spec.prices}
        for limit in self.policy_spec.limits:
            if limit.customer_id not in customers:
                raise PolicyError(f"limit {limit.id} references unknown customer")
            if limit.currency not in currencies:
                raise PolicyError(f"limit {limit.id} references unknown currency")

    def evaluate(self, event_id: str) -> SpendDecision:
        """Persist and return the deterministic decision for an ingested event."""
        existing = self._existing_decision(event_id)
        if existing is not None:
            return existing
        event = self._load_event(event_id)
        estimate = self.rating.estimate_event(event)
        applicable = self._applicable_limits(event, estimate.currency)
        reason_codes: list[str] = []
        exceeded: list[SpendLimit] = []
        approval: list[SpendLimit] = []
        for limit in applicable:
            reserved = self._reserved_today(event, limit)
            projected = reserved + estimate.net_minor
            if projected > limit.daily_limit_minor:
                exceeded.append(limit)
            elif (
                limit.approval_above_minor is not None
                and projected > limit.approval_above_minor
            ):
                approval.append(limit)

        if exceeded:
            outcome = PolicyOutcome.DENY
            reason_codes.extend(f"limit_exceeded:{limit.id}" for limit in exceeded)
        elif event.risk_level in self.policy_spec.hold_risk_levels:
            outcome = PolicyOutcome.HOLD
            reason_codes.append(f"risk_requires_approval:{event.risk_level.value}")
        elif approval:
            outcome = PolicyOutcome.HOLD
            reason_codes.extend(f"approval_threshold:{limit.id}" for limit in approval)
        else:
            outcome = PolicyOutcome.ALLOW
            reason_codes.append("within_authority")

        decision = SpendDecision(
            decision_id=f"decision_{event.event_id}",
            event_id=event.event_id,
            outcome=outcome,
            reason_codes=tuple(reason_codes),
            estimated_minor=estimate.net_minor,
            currency=estimate.currency,
            applicable_limit_ids=tuple(limit.id for limit in applicable),
        )
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO policy_decisions (
                    decision_id, event_id, outcome, reason_codes_json, estimated_minor,
                    currency, applicable_limit_ids_json, customer_id, agent_id,
                    workflow_id, occurred_date, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.event_id,
                    decision.outcome.value,
                    json.dumps(decision.reason_codes, separators=(",", ":")),
                    decision.estimated_minor,
                    decision.currency,
                    json.dumps(decision.applicable_limit_ids, separators=(",", ":")),
                    event.customer_id,
                    event.agent_id,
                    event.workflow_id,
                    event.occurred_at.date().isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            status = {
                PolicyOutcome.ALLOW: "accepted",
                PolicyOutcome.HOLD: "held",
                PolicyOutcome.DENY: "denied",
            }[outcome]
            connection.execute(
                "UPDATE usage_events SET status = ? WHERE event_id = ?", (status, event.event_id)
            )
        return decision

    def approve_held_event(
        self,
        *,
        event_id: str,
        approval_id: str,
        actor: str,
        approve: bool,
        reason: str,
        decided_at: datetime | None = None,
    ) -> bool:
        """Approve or reject a held event exactly once."""
        outcome = PolicyOutcome.ALLOW if approve else PolicyOutcome.DENY
        timestamp = decided_at or datetime.now(UTC)
        with self.store.transaction() as connection:
            event = connection.execute(
                "SELECT status FROM usage_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if event is None:
                raise PolicyError(f"unknown usage event {event_id}")
            existing = connection.execute(
                "SELECT * FROM approvals WHERE event_id = ? OR approval_id = ?",
                (event_id, approval_id),
            ).fetchone()
            if existing is not None:
                expected = (approval_id, event_id, outcome.value, actor, reason)
                actual = (
                    existing["approval_id"],
                    existing["event_id"],
                    existing["outcome"],
                    existing["actor"],
                    existing["reason"],
                )
                if actual == expected:
                    return False
                raise IdempotencyConflictError(
                    f"approval {approval_id} or event {event_id} was reused"
                )
            if event["status"] != "held":
                raise PolicyError(f"event {event_id} is not awaiting approval")
            connection.execute(
                """
                INSERT INTO approvals (
                    approval_id, event_id, outcome, actor, reason, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (approval_id, event_id, outcome.value, actor, reason, timestamp.isoformat()),
            )
            connection.execute(
                "UPDATE usage_events SET status = ? WHERE event_id = ?",
                ("accepted" if approve else "denied", event_id),
            )
        return True

    def _existing_decision(self, event_id: str) -> SpendDecision | None:
        row = self.store.connection.execute(
            "SELECT * FROM policy_decisions WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        return SpendDecision(
            decision_id=row["decision_id"],
            event_id=row["event_id"],
            outcome=PolicyOutcome(row["outcome"]),
            reason_codes=tuple(json.loads(row["reason_codes_json"])),
            estimated_minor=row["estimated_minor"],
            currency=row["currency"],
            applicable_limit_ids=tuple(json.loads(row["applicable_limit_ids_json"])),
        )

    def _load_event(self, event_id: str) -> UsageEvent:
        row = self.store.connection.execute(
            "SELECT * FROM usage_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise PolicyError(f"unknown usage event {event_id}")
        return UsageEvent(
            event_id=row["event_id"],
            idempotency_key=row["idempotency_key"],
            customer_id=row["customer_id"],
            product_id=row["product_id"],
            quantity=row["quantity"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            agent_id=row["agent_id"],
            workflow_id=row["workflow_id"],
            risk_level=row["risk_level"],
            metadata=json.loads(row["metadata_json"]),
        )

    def _applicable_limits(self, event: UsageEvent, currency: str) -> list[SpendLimit]:
        limits = []
        for limit in self.policy_spec.limits:
            if limit.customer_id != event.customer_id or limit.currency != currency:
                continue
            matches = (
                limit.scope == SpendLimitScope.CUSTOMER
                or (limit.scope == SpendLimitScope.AGENT and limit.subject_id == event.agent_id)
                or (
                    limit.scope == SpendLimitScope.WORKFLOW
                    and limit.subject_id == event.workflow_id
                )
            )
            if matches:
                limits.append(limit)
        order = {
            SpendLimitScope.AGENT: 0,
            SpendLimitScope.WORKFLOW: 1,
            SpendLimitScope.CUSTOMER: 2,
        }
        return sorted(limits, key=lambda limit: (order[limit.scope], limit.id))

    def _reserved_today(self, event: UsageEvent, limit: SpendLimit) -> int:
        query = """
            SELECT COALESCE(SUM(d.estimated_minor), 0)
            FROM policy_decisions d
            LEFT JOIN approvals a ON a.event_id = d.event_id
            WHERE d.customer_id = ? AND d.currency = ? AND d.occurred_date = ?
              AND (d.outcome = 'allow' OR a.outcome = 'allow')
        """
        params: list[str] = [
            event.customer_id,
            limit.currency,
            event.occurred_at.date().isoformat(),
        ]
        if limit.scope == SpendLimitScope.AGENT:
            query += " AND d.agent_id = ?"
            params.append(limit.subject_id or "")
        elif limit.scope == SpendLimitScope.WORKFLOW:
            query += " AND d.workflow_id = ?"
            params.append(limit.subject_id or "")
        return int(self.store.connection.execute(query, params).fetchone()[0])
