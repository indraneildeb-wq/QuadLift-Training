import pytest

from oceanbridge.core import geo
from oceanbridge.core.freight import calculate_freight_cost, chargeable_air_kg, teu_units
from oceanbridge.core.routing import RouteEngine
from oceanbridge.models import Mode


def test_sea_suez_vs_cape():
    suez, cape = geo.sea_paths("CNSHA", "NLRTM")
    assert "SUEZ" in suez and "CAPE" in cape
    q_suez = calculate_freight_cost("CNSHA", "NLRTM", Mode.SEA, 18_000, 60, via=suez)
    q_cape = calculate_freight_cost("CNSHA", "NLRTM", Mode.SEA, 18_000, 60, via=cape)
    assert q_cape.distance_km > q_suez.distance_km
    assert q_cape.cost_usd > q_suez.cost_usd and q_cape.transit_days > q_suez.transit_days


def test_air_is_faster_and_dearer_than_sea():
    sea = calculate_freight_cost("CNSHA", "NLRTM", Mode.SEA, 5_000, 25)
    air = calculate_freight_cost("CNSHA", "NLRTM", Mode.AIR, 5_000, 25)
    assert air.transit_days < sea.transit_days / 5
    assert air.cost_usd > sea.cost_usd * 3


def test_chargeable_units():
    assert teu_units(10_000, 20) == 1
    assert teu_units(10_000, 70) == 3
    assert chargeable_air_kg(100, 2) == pytest.approx(334)


def test_invalid_lanes_rejected():
    with pytest.raises(ValueError):
        calculate_freight_cost("CNSHA", "KRPUS", Mode.ROAD, 1_000, 5)  # no road across the Yellow Sea
    with pytest.raises(ValueError):
        calculate_freight_cost("SGSIN", "NLRTM", Mode.RAIL, 1_000, 5)
    with pytest.raises(ValueError):
        calculate_freight_cost("XXXXX", "NLRTM", Mode.SEA, 1_000, 5)
    assert geo.rail_available("CNSHA", "DEHAM")


def test_alternatives_mitigate_disruption(disrupted):
    from oceanbridge.core import repository as repo

    s = repo.get_shipment("SHP-1004")  # CNSZX -> NLRTM, hit by the Rotterdam strike
    opts = RouteEngine(repo.all_capacity()).alternatives(s, repo.active_disruptions())
    stay = next(o for o in opts if o.option_id == "STAY")
    reroutes = [o for o in opts if o.option_id != "STAY"]
    assert reroutes, "expected at least one reroute"
    assert all(o.projected_eta <= stay.projected_eta for o in reroutes)
    assert any("BEANR" in o.label for o in reroutes)  # Antwerp + truck bypasses Rotterdam
    assert opts == sorted(opts, key=lambda o: o.landed_cost_score)
