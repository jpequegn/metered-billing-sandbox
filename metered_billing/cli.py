"""Command-line workflows for the local billing sandbox."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

import typer
import yaml

from metered_billing import __version__
from metered_billing.invoicing import InvoiceService
from metered_billing.ledger import LedgerStore
from metered_billing.models import Invoice, UsageEvent
from metered_billing.policy import SpendPolicyEngine, load_spend_policy
from metered_billing.pricing import load_pricing_spec, pricing_spec_json
from metered_billing.rating import RatingEngine
from metered_billing.simulation import SimulationRunner

app = typer.Typer(
    name="metered-billing",
    help="Simulate metered billing and agent spend controls locally.",
    no_args_is_help=True,
)


class DocumentFormat(StrEnum):
    JSON = "json"
    AUDIT_JSON = "audit-json"
    MARKDOWN = "markdown"


@app.callback()
def main() -> None:
    """Run local billing simulations and inspect their evidence."""


def _abort(exc: Exception | str) -> NoReturn:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(1)


def _date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        _abort(f"{label} must use YYYY-MM-DD: {exc}")


def _load_mapping(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        _abort(f"unable to read {path}: {exc}")
    if not isinstance(data, dict):
        _abort(f"{path} must contain a JSON or YAML object")
    return data


def _emit(text: str, output: Path | None) -> None:
    if output is None:
        typer.echo(text)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text if text.endswith("\n") else f"{text}\n", encoding="utf-8")
    typer.echo(str(output))


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command("init")
def initialize(db: Path = typer.Option(..., "--db", help="SQLite database path.")) -> None:
    """Initialize or migrate a local billing database."""
    try:
        with LedgerStore(db) as store:
            payload = {"database": str(db), "schema_version": store.schema_version()}
        typer.echo(json.dumps(payload, sort_keys=True))
    except Exception as exc:
        _abort(exc)


@app.command("validate-pricing")
def validate_pricing(
    pricing: Path = typer.Option(..., "--pricing", help="Pricing YAML path."),
) -> None:
    """Validate a pricing specification and print its stable digest."""
    try:
        spec = load_pricing_spec(pricing)
        payload = {
            "customers": len(spec.customers),
            "products": len(spec.products),
            "prices": len(spec.prices),
            "schema_version": spec.schema_version,
            "stable_spec": pricing_spec_json(spec),
        }
        typer.echo(json.dumps(payload, sort_keys=True))
    except Exception as exc:
        _abort(exc)


@app.command("grant-credits")
def grant_credits(
    db: Path = typer.Option(..., "--db"),
    pricing: Path = typer.Option(..., "--pricing"),
    customer: str | None = typer.Option(None, "--customer"),
) -> None:
    """Grant configured initial credit buckets exactly once."""
    try:
        spec = load_pricing_spec(pricing)
        granted = 0
        replayed = 0
        with LedgerStore(db) as store:
            for bucket in spec.credit_buckets:
                if customer is not None and bucket.customer_id != customer:
                    continue
                if bucket.initial_balance_minor == 0:
                    continue
                inserted = store.grant_credit(
                    grant_id=f"spec_grant_{bucket.id}", bucket=bucket
                )
                granted += int(inserted)
                replayed += int(not inserted)
        typer.echo(json.dumps({"granted": granted, "replayed": replayed}, sort_keys=True))
    except Exception as exc:
        _abort(exc)


@app.command("ingest")
def ingest_usage(
    db: Path = typer.Option(..., "--db"),
    event: Path = typer.Option(..., "--event", help="Usage event JSON or YAML."),
    pricing: Path | None = typer.Option(None, "--pricing"),
    policy: Path | None = typer.Option(None, "--policy"),
) -> None:
    """Ingest one usage event and optionally evaluate spend policy."""
    if policy is not None and pricing is None:
        _abort("--policy requires --pricing")
    try:
        usage = UsageEvent.model_validate(_load_mapping(event))
        with LedgerStore(db) as store:
            inserted = store.ingest_usage_event(usage)
            payload: dict = {"event_id": usage.event_id, "inserted": inserted}
            if policy is not None and pricing is not None:
                decision = SpendPolicyEngine(
                    store,
                    load_pricing_spec(pricing),
                    load_spend_policy(policy),
                ).evaluate(usage.event_id)
                payload["policy"] = {
                    **asdict(decision),
                    "outcome": decision.outcome.value,
                }
        typer.echo(json.dumps(payload, sort_keys=True))
    except Exception as exc:
        _abort(exc)


@app.command("approve")
def approve_event(
    db: Path = typer.Option(..., "--db"),
    pricing: Path = typer.Option(..., "--pricing"),
    policy: Path = typer.Option(..., "--policy"),
    event_id: str = typer.Option(..., "--event-id"),
    approval_id: str = typer.Option(..., "--approval-id"),
    actor: str = typer.Option(..., "--actor"),
    reason: str = typer.Option(..., "--reason"),
    reject: bool = typer.Option(False, "--reject", help="Reject rather than approve."),
) -> None:
    """Approve or reject a held usage event."""
    try:
        with LedgerStore(db) as store:
            changed = SpendPolicyEngine(
                store,
                load_pricing_spec(pricing),
                load_spend_policy(policy),
            ).approve_held_event(
                event_id=event_id,
                approval_id=approval_id,
                actor=actor,
                approve=not reject,
                reason=reason,
            )
        typer.echo(json.dumps({"changed": changed, "event_id": event_id}, sort_keys=True))
    except Exception as exc:
        _abort(exc)


@app.command("balances")
def balances(
    db: Path = typer.Option(..., "--db"),
    customer: str = typer.Option(..., "--customer"),
    currency: str = typer.Option("USD", "--currency"),
    on_date: str | None = typer.Option(None, "--on-date"),
) -> None:
    """Show current credit balances and bucket scopes."""
    try:
        parsed_date = _date(on_date, "on-date") if on_date else None
        with LedgerStore(db) as store:
            buckets = store.credit_buckets(
                customer_id=customer, currency=currency, on_date=parsed_date
            )
            payload = {
                "buckets": buckets,
                "currency": currency,
                "customer_id": customer,
                "total_minor": sum(bucket["balance_minor"] for bucket in buckets),
            }
        typer.echo(json.dumps(payload, sort_keys=True))
    except Exception as exc:
        _abort(exc)


@app.command("rate")
def rate_period(
    db: Path = typer.Option(..., "--db"),
    pricing: Path = typer.Option(..., "--pricing"),
    customer: str = typer.Option(..., "--customer"),
    start: str = typer.Option(..., "--start"),
    end: str = typer.Option(..., "--end"),
) -> None:
    """Rate accepted usage for a closed period."""
    try:
        with LedgerStore(db) as store:
            result = RatingEngine(store, load_pricing_spec(pricing)).rate_period(
                customer_id=customer,
                period_start=_date(start, "start"),
                period_end=_date(end, "end"),
            )
        typer.echo(json.dumps(asdict(result), sort_keys=True))
    except Exception as exc:
        _abort(exc)


@app.command("invoice")
def invoice(
    db: Path = typer.Option(..., "--db"),
    customer: str = typer.Option(..., "--customer"),
    currency: str = typer.Option("USD", "--currency"),
    start: str = typer.Option(..., "--start"),
    end: str = typer.Option(..., "--end"),
    format: DocumentFormat = typer.Option(DocumentFormat.JSON, "--format"),
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    """Generate a JSON, audit JSON, or Markdown invoice."""
    try:
        with LedgerStore(db) as store:
            service = InvoiceService(store)
            document = service.generate(
                customer_id=customer,
                currency=currency,
                period_start=_date(start, "start"),
                period_end=_date(end, "end"),
            )
            if format == DocumentFormat.JSON:
                text = json.dumps(
                    document.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                )
            elif format == DocumentFormat.AUDIT_JSON:
                text = service.audit_report_json(document)
            else:
                text = service.render_markdown(document)
        _emit(text, output)
    except Exception as exc:
        _abort(exc)


@app.command("reconcile")
def reconcile(
    db: Path = typer.Option(..., "--db"),
    invoice_file: Path = typer.Option(..., "--invoice"),
) -> None:
    """Compare a saved invoice with current database evidence."""
    try:
        document = Invoice.model_validate(_load_mapping(invoice_file))
        with LedgerStore(db) as store:
            result = InvoiceService(store).reconcile(document)
        typer.echo(json.dumps(asdict(result), sort_keys=True))
        if not result.matched:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as exc:
        _abort(exc)


@app.command("simulate")
def simulate(
    pricing: Path = typer.Option(..., "--pricing"),
    workdir: Path = typer.Option(..., "--workdir"),
    seed: int = typer.Option(42, "--seed"),
    format: DocumentFormat = typer.Option(DocumentFormat.JSON, "--format"),
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    """Run deterministic failure and outcome-accounting scenarios."""
    if format == DocumentFormat.AUDIT_JSON:
        _abort("simulate supports json or markdown formats")
    try:
        runner = SimulationRunner(load_pricing_spec(pricing), workdir)
        report = runner.run(seed=seed)
        text = (
            runner.to_json(report)
            if format == DocumentFormat.JSON
            else runner.to_markdown(report)
        )
        _emit(text, output)
        if not all(scenario.passed for scenario in report.scenarios):
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as exc:
        _abort(exc)


if __name__ == "__main__":
    app()
