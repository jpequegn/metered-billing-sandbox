# Metered Billing Sandbox

A local educational implementation of usage-based billing, prepaid credits,
auto-recharge, overage invoices, reconciliation, and agent spend controls.

The project uses synthetic customers, prices, usage, outcomes, and business
values. It does not connect to Stripe, payment rails, bank accounts, or
production billing systems. It is not an accounting system.

## What it demonstrates

- Integer minor-unit money and effective-dated prices
- Exactly-once usage ingestion with transactional rollback
- Scoped and unscoped prepaid credits
- Discounts, monthly commitments, auto-recharge, and overage
- Customer, workflow, and agent daily spending authority
- Allow, hold, deny, and idempotent human approval decisions
- Traceable invoices with source events, price versions, and ledger entries
- Reconciliation that reports drift without rewriting evidence
- Seeded failure scenarios and completed-outcome cost comparisons

## Setup

Python 3.12 and `uv` are required.

```bash
uv sync --all-groups --no-editable
uv run --no-editable metered-billing --help
```

The non-editable installation avoids a macOS filesystem flag that can cause
Python to skip editable `.pth` files in some synchronized folders.

## Complete example

Run these commands from the repository root:

```bash
DB=/tmp/metered-billing-example.sqlite
rm -f "$DB" "$DB-wal" "$DB-shm"

uv run --no-editable metered-billing init --db "$DB"
uv run --no-editable metered-billing validate-pricing --pricing examples/pricing.yaml
uv run --no-editable metered-billing grant-credits \
  --db "$DB" --pricing examples/pricing.yaml --customer acme
uv run --no-editable metered-billing ingest \
  --db "$DB" \
  --event examples/usage-event.json \
  --pricing examples/pricing.yaml \
  --policy examples/policies.yaml
uv run --no-editable metered-billing rate \
  --db "$DB" --pricing examples/pricing.yaml --customer acme \
  --start 2026-08-01 --end 2026-08-31
uv run --no-editable metered-billing balances \
  --db "$DB" --customer acme --currency USD
uv run --no-editable metered-billing invoice \
  --db "$DB" --customer acme --currency USD \
  --start 2026-08-01 --end 2026-08-31 \
  --format json --output /tmp/metered-billing-invoice.json
uv run --no-editable metered-billing reconcile \
  --db "$DB" --invoice /tmp/metered-billing-invoice.json
```

The example task costs USD 5.00 before its configured 10% discount. Prepaid
task credits cover the resulting USD 4.50. The synthetic monthly commitment
still creates a traceable period charge.

## Failure simulations

```bash
uv run --no-editable metered-billing simulate \
  --pricing examples/pricing.yaml \
  --workdir /tmp/metered-billing-simulations \
  --seed 42 \
  --format markdown \
  --output /tmp/metered-billing-simulation.md
```

The scenarios cover duplicate events, out-of-order delivery, transactional
retry rollback, partial recharge, negative-balance prevention, and effective
price changes. The outcome comparison reports token cost, completed-task cost,
and synthetic value separately.

## Policy behavior

Spend decisions reserve estimated net charges for the event's calendar day.
Any applicable hard limit can deny an event. High risk or an approval threshold
can hold it. Held events cannot be rated until an explicit approval changes
their state to accepted. Denied events never consume credits.

The precedence order is agent, workflow, then customer for display and reason
codes. Enforcement is conservative: exceeding any applicable limit denies the
event.

## Extension paths

- Add a Stripe test-mode adapter behind the existing typed boundary.
- Compare model-provider invoices with locally recorded token usage.
- Replace synthetic task value with reviewed business outcome labels.
- Add multi-currency conversion using versioned external rate snapshots.
- Export OpenTelemetry events for spend and policy decisions.
- Feed held and denied cases into an agent-routing evaluation suite.

## Development

```bash
uv sync --all-groups --no-editable --reinstall-package metered-billing-sandbox
uv run --no-editable ruff check .
uv run --no-editable pytest
uv build
```
