import pytest

from oceanbridge.config import HitlSettings
from oceanbridge.hitl import gate

POLICY = HitlSettings(max_cost_increase_pct=5.0, max_cargo_value_usd=250_000)


@pytest.mark.parametrize(
    "cost_pct, value, auto",
    [
        (4.99, 249_999, True),     # both under -> auto
        (-12.0, 10_000, True),     # cheaper reroute, low value -> auto
        (0.0, 0, True),
        (5.0, 100_000, False),     # cost exactly at limit -> sign-off
        (4.0, 250_000, False),     # value exactly at limit -> sign-off
        (12.0, 50_000, False),     # cost over
        (1.0, 3_000_000, False),   # value over
        (25.0, 900_000, False),    # both over
    ],
)
def test_gate_boundaries(cost_pct, value, auto):
    assert gate.evaluate(cost_pct, value, POLICY).auto_execute is auto


def test_reasons_name_each_breached_limit():
    d = gate.evaluate(9.0, 400_000, POLICY)
    assert len(d.reasons) == 2
    assert any("cost increase" in r.lower() for r in d.reasons)
    assert any("cargo value" in r.lower() for r in d.reasons)


def test_thresholds_are_configurable():
    loose = HitlSettings(max_cost_increase_pct=15.0, max_cargo_value_usd=1_000_000)
    assert gate.evaluate(9.0, 400_000, loose).auto_execute


def test_cost_increase_pct():
    assert gate.cost_increase_pct(105, 100) == pytest.approx(5.0)
