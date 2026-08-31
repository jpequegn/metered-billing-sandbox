"""Seeded failure scenarios and synthetic agent outcome accounting."""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from metered_billing.ledger import IdempotencyConflictError, LedgerStore
from metered_billing.models import PricingSpec, UsageEvent
from metered_billing.rating import RatingEngine


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    passed: bool
    metrics: dict[str, int | str | bool]


@dataclass(frozen=True)
class OutcomePolicyResult:
    policy: str
    selected_tasks: int
    token_cost_minor: int
    completed_tasks: int
    completed_outcome_cost_minor: int | None
    simulated_value_minor: int


@dataclass(frozen=True)
class SimulationReport:
    seed: int
    scenarios: tuple[ScenarioResult, ...]
    outcome_policies: tuple[OutcomePolicyResult, ...]
    disclaimer: str


class SimulationRunner:
    DISCLAIMER = (
        "All customers, prices, outcomes, and business values are synthetic. "
        "Results are educational and are not financial performance."
    )

    def __init__(self, spec: PricingSpec, workdir: str | Path) -> None:
        self.spec = spec
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)

    def run(self, *, seed: int = 42) -> SimulationReport:
        scenarios = (
            self._duplicate_event(),
            self._out_of_order(),
            self._retry_rollback(),
            self._partial_recharge(),
            self._negative_balance_prevention(),
            self._price_change(),
        )
        outcomes = self._compare_outcome_policies(seed)
        return SimulationReport(
            seed=seed,
            scenarios=scenarios,
            outcome_policies=outcomes,
            disclaimer=self.DISCLAIMER,
        )

    @staticmethod
    def to_json(report: SimulationReport) -> str:
        return json.dumps(asdict(report), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def to_markdown(report: SimulationReport) -> str:
        lines = [
            "# Metered billing simulation",
            "",
            f"Seed: `{report.seed}`",
            "",
            "## Failure scenarios",
            "",
            "| Scenario | Passed | Metrics |",
            "|---|---:|---|",
        ]
        for scenario in report.scenarios:
            metrics = ", ".join(f"{key}={value}" for key, value in sorted(scenario.metrics.items()))
            lines.append(f"| {scenario.name} | {'yes' if scenario.passed else 'no'} | {metrics} |")
        lines.extend(
            [
                "",
                "## Synthetic outcome accounting",
                "",
                "| Policy | Tasks | Token cost | Completed | "
                "Cost per completion | Simulated value |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for result in report.outcome_policies:
            cost = (
                str(result.completed_outcome_cost_minor)
                if result.completed_outcome_cost_minor is not None
                else "n/a"
            )
            lines.append(
                f"| {result.policy} | {result.selected_tasks} | {result.token_cost_minor} | "
                f"{result.completed_tasks} | {cost} | {result.simulated_value_minor} |"
            )
        lines.extend(["", report.disclaimer, ""])
        return "\n".join(lines)

    def _database(self, name: str) -> LedgerStore:
        path = self.workdir / f"{name}.sqlite"
        for candidate in (path, path.with_suffix(".sqlite-wal"), path.with_suffix(".sqlite-shm")):
            candidate.unlink(missing_ok=True)
        return LedgerStore(path)

    @staticmethod
    def _event(
        event_id: str,
        *,
        product_id: str = "agent_task",
        quantity: int = 1,
        day: int = 1,
    ) -> UsageEvent:
        return UsageEvent(
            event_id=event_id,
            idempotency_key=f"request-{event_id}",
            customer_id="acme",
            product_id=product_id,
            quantity=quantity,
            occurred_at=datetime(2026, 8, day, 12, tzinfo=UTC),
            agent_id="sim_agent",
            workflow_id="sim_workflow",
        )

    def _rate(self, store: LedgerStore, spec: PricingSpec | None = None):
        return RatingEngine(store, spec or self.spec).rate_period(
            customer_id="acme",
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
        )

    def _duplicate_event(self) -> ScenarioResult:
        with self._database("duplicate-event") as store:
            first = store.ingest_usage_event(self._event("usage_001"))
            replay = store.ingest_usage_event(self._event("usage_001"))
            count = store.usage_count()
        return ScenarioResult(
            name="duplicate event",
            passed=first and not replay and count == 1,
            metrics={"stored_events": count, "replay_inserted": replay},
        )

    def _out_of_order(self) -> ScenarioResult:
        events = [
            self._event("usage_001", quantity=3, day=1),
            self._event("usage_002", product_id="input_tokens", quantity=20_000, day=2),
        ]
        totals = []
        for index, ordered in enumerate((events, list(reversed(events)))):
            with self._database(f"out-of-order-{index}") as store:
                store.ingest_usage_events(ordered)
                result = self._rate(store)
                totals.append(
                    (
                        result.gross_minor,
                        result.discount_minor,
                        result.credit_minor,
                        result.overage_minor,
                        store.credit_balance("acme", "USD"),
                    )
                )
        return ScenarioResult(
            name="out-of-order events",
            passed=totals[0] == totals[1],
            metrics={"gross_minor": totals[0][0], "final_balance_minor": totals[0][-1]},
        )

    def _retry_rollback(self) -> ScenarioResult:
        conflict_seen = False
        with self._database("retry-rollback") as store:
            store.ingest_usage_event(self._event("usage_001"))
            try:
                store.ingest_usage_events(
                    [
                        self._event("usage_002"),
                        self._event("usage_003").model_copy(
                            update={"idempotency_key": "request-usage_001", "quantity": 99}
                        ),
                    ]
                )
            except IdempotencyConflictError:
                conflict_seen = True
            count = store.usage_count()
        return ScenarioResult(
            name="retry rollback",
            passed=conflict_seen and count == 1,
            metrics={"conflict_seen": conflict_seen, "stored_events": count},
        )

    def _partial_recharge(self) -> ScenarioResult:
        data = self._base_without_adjustments()
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
                "id": "sim_recharge",
                "customer_id": "acme",
                "currency": "USD",
                "threshold_minor": 50,
                "recharge_amount_minor": 300,
                "max_recharges_per_period": 1,
            }
        ]
        spec = PricingSpec.model_validate(data)
        with self._database("partial-recharge") as store:
            store.ingest_usage_event(self._event("usage_001", quantity=2))
            result = self._rate(store, spec)
        return ScenarioResult(
            name="partial recharge",
            passed=(result.recharge_minor, result.credit_minor, result.overage_minor)
            == (300, 400, 100),
            metrics={
                "recharge_minor": result.recharge_minor,
                "credit_minor": result.credit_minor,
                "overage_minor": result.overage_minor,
            },
        )

    def _negative_balance_prevention(self) -> ScenarioResult:
        data = self._base_without_adjustments()
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
        spec = PricingSpec.model_validate(data)
        with self._database("negative-balance") as store:
            store.ingest_usage_event(self._event("usage_001", quantity=100))
            result = self._rate(store, spec)
            balances = store.credit_buckets(customer_id="acme", currency="USD")
            minimum = min(item["balance_minor"] for item in balances)
        return ScenarioResult(
            name="negative balance prevention",
            passed=minimum >= 0 and result.overage_minor > 0,
            metrics={"minimum_balance_minor": minimum, "overage_minor": result.overage_minor},
        )

    def _price_change(self) -> ScenarioResult:
        data = self._base_without_adjustments()
        first = dict(data["prices"][0])
        first["effective_to"] = date(2026, 8, 14)
        second = dict(first)
        second.update(id="task_v2", unit_amount_minor=400, effective_from=date(2026, 8, 15))
        second["effective_to"] = None
        data["prices"] = [first, second, data["prices"][1]]
        spec = PricingSpec.model_validate(data)
        with self._database("price-change") as store:
            store.ingest_usage_events(
                [
                    self._event("usage_early", day=14),
                    self._event("usage_late", day=15),
                ]
            )
            result = self._rate(store, spec)
        return ScenarioResult(
            name="effective price change",
            passed=result.gross_minor == 650,
            metrics={"gross_minor": result.gross_minor},
        )

    def _base_without_adjustments(self) -> dict[str, Any]:
        data = self.spec.model_dump(mode="python")
        data["credit_buckets"] = []
        data["recharge_policies"] = []
        data["monthly_commitments"] = []
        data["discounts"] = []
        return data

    @staticmethod
    def _compare_outcome_policies(seed: int) -> tuple[OutcomePolicyResult, ...]:
        rng = random.Random(seed)
        workloads = []
        for index in range(30):
            cost = rng.randint(20, 120)
            probability = rng.randint(2500, 9500)
            value = rng.randint(150, 900)
            completed = rng.randrange(10_000) < probability
            workloads.append(
                {
                    "id": index,
                    "cost": cost,
                    "probability": probability,
                    "value": value,
                    "completed": completed,
                }
            )
        budget = 1000
        cheapest = sorted(workloads, key=lambda item: (item["cost"], item["id"]))
        outcome_ranked = sorted(
            workloads,
            key=lambda item: (
                -(item["probability"] * item["value"] // item["cost"]),
                item["id"],
            ),
        )
        return (
            SimulationRunner._select_workloads("token-only cap", cheapest, budget),
            SimulationRunner._select_workloads(
                "completed-outcome accounting", outcome_ranked, budget
            ),
        )

    @staticmethod
    def _select_workloads(
        policy: str, workloads: list[dict[str, int | bool]], budget: int
    ) -> OutcomePolicyResult:
        selected = []
        spent = 0
        for workload in workloads:
            cost = int(workload["cost"])
            if spent + cost <= budget:
                selected.append(workload)
                spent += cost
        completed = [item for item in selected if item["completed"]]
        value = sum(int(item["value"]) for item in completed)
        return OutcomePolicyResult(
            policy=policy,
            selected_tasks=len(selected),
            token_cost_minor=spent,
            completed_tasks=len(completed),
            completed_outcome_cost_minor=(spent // len(completed) if completed else None),
            simulated_value_minor=value,
        )
