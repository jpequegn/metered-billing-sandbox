# Metered Billing Sandbox

A local educational sandbox for usage-based billing, prepaid credits, overage,
invoices, reconciliation, and agent spend controls.

The project uses synthetic customers, prices, and usage. It does not connect to
Stripe, payment rails, bank accounts, or production billing systems. It is not
an accounting system.

## Setup

```bash
uv sync --all-groups --no-editable
uv run --no-editable metered-billing --help
```

## Development

```bash
uv run --no-editable ruff check .
uv run --no-editable pytest
```

## Planned workflow

The completed CLI will validate a pricing specification, initialize a local
database, grant credits, ingest usage, apply spend policies, generate invoices,
reconcile them with the ledger, and run deterministic failure simulations.
