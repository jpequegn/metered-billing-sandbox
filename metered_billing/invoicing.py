"""Traceable invoice generation, rendering, and reconciliation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta

from metered_billing.ledger import LedgerStore
from metered_billing.models import Invoice, InvoiceLine, LedgerEntryKind


@dataclass(frozen=True)
class ReconciliationResult:
    invoice_total_minor: int
    evidence_total_minor: int
    difference_minor: int
    matched: bool
    evidence_entry_ids: tuple[str, ...]


class InvoiceService:
    def __init__(self, store: LedgerStore) -> None:
        self.store = store

    def generate(
        self,
        *,
        customer_id: str,
        currency: str,
        period_start: date,
        period_end: date,
    ) -> Invoice:
        """Build an invoice from rated overage and period-level ledger evidence."""
        if period_end < period_start:
            raise ValueError("period_end must not precede period_start")
        lines: list[InvoiceLine] = []
        rated = self.store.connection.execute(
            """
            SELECT r.*, u.customer_id
            FROM rated_events r
            JOIN usage_events u ON u.event_id = r.event_id
            WHERE u.customer_id = ? AND r.currency = ?
              AND r.period_start = ? AND r.period_end = ?
            ORDER BY r.event_id
            """,
            (customer_id, currency, period_start.isoformat(), period_end.isoformat()),
        ).fetchall()
        for row in rated:
            if row["overage_minor"] == 0:
                continue
            evidence = self.store.connection.execute(
                """
                SELECT entry_id FROM ledger_entries
                WHERE source_event_id = ? ORDER BY entry_id
                """,
                (row["event_id"],),
            ).fetchall()
            lines.append(
                InvoiceLine(
                    id=f"line_usage_{row['event_id']}",
                    description=f"Overage for usage event {row['event_id']}",
                    amount_minor=row["overage_minor"],
                    usage_event_ids=(row["event_id"],),
                    price_id=row["price_id"],
                    ledger_entry_ids=tuple(item["entry_id"] for item in evidence),
                )
            )

        period_entries = self._period_payable_entries(
            customer_id=customer_id,
            currency=currency,
            period_start=period_start,
            period_end=period_end,
        )
        for row in period_entries:
            label = "Auto-recharge" if row["kind"] == "recharge" else "Commitment shortfall"
            lines.append(
                InvoiceLine(
                    id=f"line_{row['entry_id']}",
                    description=f"{label}: {row['description']}",
                    amount_minor=row["amount_minor"],
                    ledger_entry_ids=(row["entry_id"],),
                )
            )

        ordered = tuple(sorted(lines, key=lambda line: line.id))
        return Invoice(
            id=(
                f"inv_{customer_id}_{period_start.strftime('%Y%m%d')}_"
                f"{period_end.strftime('%Y%m%d')}_{currency.lower()}"
            ),
            customer_id=customer_id,
            currency=currency,
            period_start=period_start,
            period_end=period_end,
            lines=ordered,
            total_minor=sum(line.amount_minor for line in ordered),
        )

    def reconcile(self, invoice: Invoice) -> ReconciliationResult:
        """Compare an invoice with current rated and ledger evidence."""
        rated = self.store.connection.execute(
            """
            SELECT COALESCE(SUM(r.overage_minor), 0)
            FROM rated_events r
            JOIN usage_events u ON u.event_id = r.event_id
            WHERE u.customer_id = ? AND r.currency = ?
              AND r.period_start = ? AND r.period_end = ?
            """,
            (
                invoice.customer_id,
                invoice.currency,
                invoice.period_start.isoformat(),
                invoice.period_end.isoformat(),
            ),
        ).fetchone()[0]
        entries = self._period_payable_entries(
            customer_id=invoice.customer_id,
            currency=invoice.currency,
            period_start=invoice.period_start,
            period_end=invoice.period_end,
        )
        evidence_total = int(rated) + sum(row["amount_minor"] for row in entries)
        difference = evidence_total - invoice.total_minor
        event_entry_ids = tuple(
            sorted(entry_id for line in invoice.lines for entry_id in line.ledger_entry_ids)
        )
        return ReconciliationResult(
            invoice_total_minor=invoice.total_minor,
            evidence_total_minor=evidence_total,
            difference_minor=difference,
            matched=difference == 0,
            evidence_entry_ids=event_entry_ids,
        )

    def audit_report_json(self, invoice: Invoice) -> str:
        """Render byte-stable JSON containing the invoice and reconciliation result."""
        reconciliation = self.reconcile(invoice)
        payload = {
            "invoice": invoice.model_dump(mode="json"),
            "reconciliation": asdict(reconciliation),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def render_markdown(self, invoice: Invoice) -> str:
        """Render a human-readable invoice with source evidence."""
        reconciliation = self.reconcile(invoice)
        rows = [
            f"# Invoice {invoice.id}",
            "",
            f"Customer: `{invoice.customer_id}`  ",
            f"Period: {invoice.period_start.isoformat()} to {invoice.period_end.isoformat()}  ",
            f"Currency: {invoice.currency}",
            "",
            "| Line | Description | Evidence | Amount |",
            "|---|---|---|---:|",
        ]
        for line in invoice.lines:
            evidence = ", ".join((*line.usage_event_ids, *line.ledger_entry_ids))
            rows.append(
                f"| `{line.id}` | {line.description} | `{evidence}` | "
                f"{self._format_money(line.amount_minor, invoice.currency)} |"
            )
        rows.extend(
            [
                "",
                f"**Total: {self._format_money(invoice.total_minor, invoice.currency)}**",
                "",
                "## Reconciliation",
                "",
                "Evidence total: "
                f"{self._format_money(reconciliation.evidence_total_minor, invoice.currency)}  ",
                "Difference: "
                f"{self._format_money(reconciliation.difference_minor, invoice.currency)}  ",
                f"Matched: {'yes' if reconciliation.matched else 'no'}",
                "",
                "Synthetic educational output. This is not a financial accounting document.",
            ]
        )
        return "\n".join(rows) + "\n"

    def _period_payable_entries(
        self,
        *,
        customer_id: str,
        currency: str,
        period_start: date,
        period_end: date,
    ):
        start = datetime.combine(period_start, time.min, tzinfo=UTC).isoformat()
        end = datetime.combine(period_end + timedelta(days=1), time.min, tzinfo=UTC).isoformat()
        return self.store.connection.execute(
            """
            SELECT * FROM ledger_entries
            WHERE customer_id = ? AND currency = ?
              AND kind IN (?, ?) AND occurred_at >= ? AND occurred_at < ?
            ORDER BY occurred_at, entry_id
            """,
            (
                customer_id,
                currency,
                LedgerEntryKind.RECHARGE.value,
                LedgerEntryKind.COMMITMENT.value,
                start,
                end,
            ),
        ).fetchall()

    @staticmethod
    def _format_money(amount_minor: int, currency: str) -> str:
        sign = "-" if amount_minor < 0 else ""
        absolute = abs(amount_minor)
        return f"{sign}{currency} {absolute // 100}.{absolute % 100:02d}"
