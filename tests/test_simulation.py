from pathlib import Path

from metered_billing.pricing import load_pricing_spec
from metered_billing.simulation import SimulationRunner

PRICING = Path(__file__).parents[1] / "examples" / "pricing.yaml"


def test_all_failure_scenarios_preserve_invariants(tmp_path: Path) -> None:
    report = SimulationRunner(load_pricing_spec(PRICING), tmp_path).run(seed=42)

    assert len(report.scenarios) == 6
    assert all(scenario.passed for scenario in report.scenarios)
    assert {scenario.name for scenario in report.scenarios} == {
        "duplicate event",
        "out-of-order events",
        "retry rollback",
        "partial recharge",
        "negative balance prevention",
        "effective price change",
    }


def test_seeded_outcome_comparison_is_reproducible(tmp_path: Path) -> None:
    runner = SimulationRunner(load_pricing_spec(PRICING), tmp_path)

    first = runner.to_json(runner.run(seed=123))
    second = runner.to_json(runner.run(seed=123))
    different = runner.to_json(runner.run(seed=124))

    assert first == second
    assert first != different
    assert "token-only cap" in first
    assert "completed-outcome accounting" in first


def test_reports_distinct_cost_outcome_and_value_metrics(tmp_path: Path) -> None:
    runner = SimulationRunner(load_pricing_spec(PRICING), tmp_path)
    report = runner.run(seed=42)

    for policy in report.outcome_policies:
        assert policy.token_cost_minor <= 1000
        assert policy.completed_tasks <= policy.selected_tasks
        assert policy.simulated_value_minor >= 0
    markdown = runner.to_markdown(report)
    assert "Cost per completion" in markdown
    assert "Simulated value" in markdown
    assert "not financial performance" in markdown
