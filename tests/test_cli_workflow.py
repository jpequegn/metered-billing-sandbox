from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
PRICING = ROOT / "examples" / "pricing.yaml"
POLICIES = ROOT / "examples" / "policies.yaml"
EVENT = ROOT / "examples" / "usage-event.json"


def run_cli(*args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "metered_billing.cli", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected, result.stderr or result.stdout
    return result


def test_documented_end_to_end_workflow(tmp_path: Path) -> None:
    db = tmp_path / "billing.sqlite"
    invoice = tmp_path / "invoice.json"

    initialized = run_cli("init", "--db", str(db))
    assert json.loads(initialized.stdout)["schema_version"] == 3

    validated = run_cli("validate-pricing", "--pricing", str(PRICING))
    assert json.loads(validated.stdout)["products"] == 2

    granted = run_cli(
        "grant-credits",
        "--db",
        str(db),
        "--pricing",
        str(PRICING),
        "--customer",
        "acme",
    )
    assert json.loads(granted.stdout) == {"granted": 2, "replayed": 0}

    ingested = run_cli(
        "ingest",
        "--db",
        str(db),
        "--event",
        str(EVENT),
        "--pricing",
        str(PRICING),
        "--policy",
        str(POLICIES),
    )
    assert json.loads(ingested.stdout)["policy"]["outcome"] == "allow"

    rated = run_cli(
        "rate",
        "--db",
        str(db),
        "--pricing",
        str(PRICING),
        "--customer",
        "acme",
        "--start",
        "2026-08-01",
        "--end",
        "2026-08-31",
    )
    assert json.loads(rated.stdout)["rated_events"] == 1

    balance = run_cli(
        "balances", "--db", str(db), "--customer", "acme", "--currency", "USD"
    )
    assert json.loads(balance.stdout)["total_minor"] == 3050

    run_cli(
        "invoice",
        "--db",
        str(db),
        "--customer",
        "acme",
        "--currency",
        "USD",
        "--start",
        "2026-08-01",
        "--end",
        "2026-08-31",
        "--format",
        "json",
        "--output",
        str(invoice),
    )
    document = json.loads(invoice.read_text(encoding="utf-8"))
    assert document["total_minor"] == 5000

    reconciled = run_cli("reconcile", "--db", str(db), "--invoice", str(invoice))
    assert json.loads(reconciled.stdout)["matched"] is True


def test_simulation_command_reports_all_scenarios(tmp_path: Path) -> None:
    result = run_cli(
        "simulate",
        "--pricing",
        str(PRICING),
        "--workdir",
        str(tmp_path / "simulations"),
        "--seed",
        "42",
        "--format",
        "json",
    )

    report = json.loads(result.stdout)
    assert len(report["scenarios"]) == 6
    assert all(scenario["passed"] for scenario in report["scenarios"])


def test_reconcile_returns_nonzero_for_drift(tmp_path: Path) -> None:
    db = tmp_path / "billing.sqlite"
    bad_invoice = tmp_path / "invoice.json"
    run_cli("init", "--db", str(db))
    bad_invoice.write_text(
        json.dumps(
            {
                "id": "inv_acme_20260801_20260831_usd",
                "customer_id": "acme",
                "currency": "USD",
                "period_start": "2026-08-01",
                "period_end": "2026-08-31",
                "lines": [
                    {
                        "id": "line_manual",
                        "description": "Unsupported manual charge",
                        "amount_minor": 1,
                    }
                ],
                "total_minor": 1,
            }
        ),
        encoding="utf-8",
    )

    result = run_cli(
        "reconcile", "--db", str(db), "--invoice", str(bad_invoice), expected=1
    )

    assert json.loads(result.stdout)["difference_minor"] == -1
