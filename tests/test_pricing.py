from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from metered_billing.pricing import PricingSpecError, load_pricing_spec, pricing_spec_json

FIXTURE = Path(__file__).parents[1] / "examples" / "pricing.yaml"


def load_data() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def write_spec(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "pricing.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_loads_complete_pricing_fixture() -> None:
    spec = load_pricing_spec(FIXTURE)

    assert spec.schema_version == 1
    assert spec.customers[0].id == "acme"
    assert spec.credit_buckets[0].initial_balance_minor == 2500
    assert spec.discounts[0].basis_points == 1000


def test_serialization_is_deterministic() -> None:
    first = pricing_spec_json(load_pricing_spec(FIXTURE))
    second = pricing_spec_json(load_pricing_spec(FIXTURE))

    assert first == second
    assert '"currency":"USD"' in first


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data["prices"][0].update(currency="usd"), "currency"),
        (lambda data: data["prices"][0].update(unit_amount_minor=-1), "greater than"),
        (lambda data: data["prices"][0].update(unit_amount_minor=1.5), "integer"),
        (lambda data: data["prices"].append(deepcopy(data["prices"][0])), "duplicate price"),
        (lambda data: data["prices"][0].update(product_id="missing"), "unknown product"),
        (
            lambda data: data["prices"][0].update(effective_to="2025-01-01"),
            "effective_to",
        ),
        (
            lambda data: data["credit_buckets"][0].update(customer_id="missing"),
            "unknown customer",
        ),
    ],
)
def test_rejects_invalid_specs(tmp_path: Path, mutate, message: str) -> None:
    data = load_data()
    mutate(data)

    with pytest.raises(PricingSpecError, match=message):
        load_pricing_spec(write_spec(tmp_path, data))


def test_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(PricingSpecError, match="YAML object"):
        load_pricing_spec(path)
