"""Immutable domain models for pricing, usage, billing, and approvals."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{1,63}$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
MinorUnits = Annotated[int, Field(strict=True, ge=0)]
PositiveMinorUnits = Annotated[int, Field(strict=True, gt=0)]
PositiveQuantity = Annotated[int, Field(strict=True, gt=0)]
BasisPoints = Annotated[int, Field(strict=True, ge=0, le=10_000)]


class FrozenModel(BaseModel):
    """Base model with immutable fields and rejected unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    HOLD = "hold"
    DENY = "deny"


class LedgerEntryKind(StrEnum):
    CREDIT_GRANT = "credit_grant"
    CREDIT_CONSUMPTION = "credit_consumption"
    CHARGE = "charge"
    DISCOUNT = "discount"
    RECHARGE = "recharge"
    ADJUSTMENT = "adjustment"


class Customer(FrozenModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=200)


class Product(FrozenModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=200)
    unit: str = Field(min_length=1, max_length=40)


class Price(FrozenModel):
    id: Identifier
    product_id: Identifier
    currency: Currency
    unit_amount_minor: MinorUnits
    unit_size: PositiveQuantity = 1
    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def validate_date_range(self) -> Price:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not precede effective_from")
        return self


class CreditBucket(FrozenModel):
    id: Identifier
    customer_id: Identifier
    currency: Currency
    initial_balance_minor: MinorUnits
    product_ids: tuple[Identifier, ...] = ()
    valid_from: date
    valid_to: date | None = None

    @model_validator(mode="after")
    def validate_date_range(self) -> CreditBucket:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        return self


class RechargePolicy(FrozenModel):
    id: Identifier
    customer_id: Identifier
    currency: Currency
    threshold_minor: MinorUnits
    recharge_amount_minor: PositiveMinorUnits
    max_recharges_per_period: Annotated[int, Field(strict=True, ge=0)] = 1


class MonthlyCommitment(FrozenModel):
    id: Identifier
    customer_id: Identifier
    currency: Currency
    amount_minor: MinorUnits


class Discount(FrozenModel):
    id: Identifier
    customer_id: Identifier
    basis_points: BasisPoints
    product_ids: tuple[Identifier, ...] = ()


class UsageEvent(FrozenModel):
    event_id: Identifier
    idempotency_key: str = Field(min_length=1, max_length=200)
    customer_id: Identifier
    product_id: Identifier
    quantity: PositiveQuantity
    occurred_at: datetime
    agent_id: Identifier | None = None
    workflow_id: Identifier | None = None
    risk_level: RiskLevel = RiskLevel.LOW
    metadata: dict[str, Any] = Field(default_factory=dict)


class Approval(FrozenModel):
    id: Identifier
    usage_event_id: Identifier
    outcome: PolicyOutcome
    reason_code: str = Field(min_length=1, max_length=80)
    actor: str = Field(min_length=1, max_length=200)
    decided_at: datetime


class LedgerEntry(FrozenModel):
    id: Identifier
    customer_id: Identifier
    currency: Currency
    kind: LedgerEntryKind
    amount_minor: int = Field(strict=True)
    occurred_at: datetime
    source_event_id: Identifier | None = None
    credit_bucket_id: Identifier | None = None
    description: str = Field(min_length=1, max_length=500)


class InvoiceLine(FrozenModel):
    id: Identifier
    description: str = Field(min_length=1, max_length=500)
    amount_minor: int = Field(strict=True)
    usage_event_ids: tuple[Identifier, ...] = ()
    price_id: Identifier | None = None
    ledger_entry_ids: tuple[Identifier, ...] = ()


class Invoice(FrozenModel):
    id: Identifier
    customer_id: Identifier
    currency: Currency
    period_start: date
    period_end: date
    lines: tuple[InvoiceLine, ...]
    total_minor: int = Field(strict=True)

    @model_validator(mode="after")
    def validate_period_and_total(self) -> Invoice:
        if self.period_end < self.period_start:
            raise ValueError("period_end must not precede period_start")
        if self.total_minor != sum(line.amount_minor for line in self.lines):
            raise ValueError("total_minor must equal the sum of invoice lines")
        return self


class PricingSpec(FrozenModel):
    schema_version: Annotated[int, Field(strict=True, ge=1)]
    customers: tuple[Customer, ...]
    products: tuple[Product, ...]
    prices: tuple[Price, ...]
    credit_buckets: tuple[CreditBucket, ...] = ()
    recharge_policies: tuple[RechargePolicy, ...] = ()
    monthly_commitments: tuple[MonthlyCommitment, ...] = ()
    discounts: tuple[Discount, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> PricingSpec:
        collections = {
            "customer": self.customers,
            "product": self.products,
            "price": self.prices,
            "credit bucket": self.credit_buckets,
            "recharge policy": self.recharge_policies,
            "monthly commitment": self.monthly_commitments,
            "discount": self.discounts,
        }
        for label, items in collections.items():
            ids = [item.id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label} id")

        customer_ids = {customer.id for customer in self.customers}
        product_ids = {product.id for product in self.products}
        for price in self.prices:
            if price.product_id not in product_ids:
                raise ValueError(f"price {price.id} references unknown product {price.product_id}")

        customer_models = (
            *self.credit_buckets,
            *self.recharge_policies,
            *self.monthly_commitments,
            *self.discounts,
        )
        for model in customer_models:
            if model.customer_id not in customer_ids:
                raise ValueError(
                    f"{type(model).__name__} {model.id} references unknown customer "
                    f"{model.customer_id}"
                )

        scoped_models = (*self.credit_buckets, *self.discounts)
        for model in scoped_models:
            unknown_products = set(model.product_ids) - product_ids
            if unknown_products:
                raise ValueError(
                    f"{type(model).__name__} {model.id} references unknown products "
                    f"{sorted(unknown_products)}"
                )
        return self
