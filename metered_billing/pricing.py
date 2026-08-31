"""Pricing specification loading and deterministic serialization."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from metered_billing.models import PricingSpec


class PricingSpecError(ValueError):
    """Raised when a pricing specification cannot be parsed or validated."""


def load_pricing_spec(path: str | Path) -> PricingSpec:
    """Load and validate a versioned YAML pricing specification."""
    source = Path(path)
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PricingSpecError(f"unable to read pricing specification {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise PricingSpecError("pricing specification must contain a YAML object")
    try:
        return PricingSpec.model_validate(data)
    except ValidationError as exc:
        raise PricingSpecError(f"invalid pricing specification: {exc}") from exc


def pricing_spec_json(spec: PricingSpec) -> str:
    """Return stable JSON suitable for diffs and fixture checks."""
    return json.dumps(
        spec.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
