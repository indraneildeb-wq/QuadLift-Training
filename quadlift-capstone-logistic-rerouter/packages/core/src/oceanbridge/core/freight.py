"""Deterministic freight tariff: cost, transit time and emissions per mode."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from oceanbridge.core import geo
from oceanbridge.core.reference import CARRIERS_BY_ID, carriers_for
from oceanbridge.models import Mode

TEU_CBM = 33.0
TEU_MAX_KG = 21_000.0
AIR_VOLUMETRIC_KG_PER_CBM = 167.0

# Base tariffs. Sea/rail/road are priced per TEU, air per chargeable kg.
TARIFF = {
    Mode.SEA: dict(per_unit_per_1000km=105.0, fixed_per_unit=450.0, km_per_day=700.0, handling_days=4.0, co2_g_per_tkm=10.0, detour=1.12),
    Mode.RAIL: dict(per_unit_per_1000km=390.0, fixed_per_unit=350.0, km_per_day=800.0, handling_days=3.0, co2_g_per_tkm=25.0, detour=1.30),
    Mode.ROAD: dict(per_unit_per_km=1.4, fixed_per_unit=120.0, km_per_day=600.0, handling_days=0.5, co2_g_per_tkm=62.0, detour=1.25),
    Mode.AIR: dict(per_kg_base=1.9, per_kg_per_km=0.00028, fixed=250.0, km_per_day=9000.0, handling_days=1.5, co2_g_per_tkm=600.0, detour=1.05),
}


@dataclass
class FreightQuote:
    origin: str
    destination: str
    mode: str
    carrier_id: str
    via: list[str]
    distance_km: float
    chargeable_units: float
    unit: str
    cost_usd: float
    transit_days: float
    co2_kg: float

    def to_dict(self) -> dict:
        return asdict(self)


def teu_units(weight_kg: float, volume_cbm: float) -> int:
    return max(1, math.ceil(max(volume_cbm / TEU_CBM, weight_kg / TEU_MAX_KG)))


def chargeable_air_kg(weight_kg: float, volume_cbm: float) -> float:
    return max(weight_kg, volume_cbm * AIR_VOLUMETRIC_KG_PER_CBM)


def capacity_required(mode: Mode, weight_kg: float, volume_cbm: float) -> float:
    return chargeable_air_kg(weight_kg, volume_cbm) if mode == Mode.AIR else float(teu_units(weight_kg, volume_cbm))


def default_carrier(mode: Mode) -> str:
    return carriers_for(mode)[0].id


def calculate_freight_cost(
    origin: str,
    destination: str,
    mode: Mode | str,
    weight_kg: float,
    volume_cbm: float,
    carrier_id: str | None = None,
    via: list[str] | None = None,
) -> FreightQuote:
    """Price a single leg. Raises ValueError for unknown locations or unsupported mode/lane combinations."""
    mode = Mode(mode)
    if mode == Mode.MULTIMODAL:
        raise ValueError("Price multimodal routes leg by leg")
    for code in (origin, destination):
        if code not in geo.PORTS:
            raise ValueError(f"Unknown port code: {code}")
    if mode == Mode.RAIL and not geo.rail_available(origin, destination) and geo.region(origin) != geo.region(destination):
        raise ValueError(f"No rail corridor between {origin} and {destination}")
    if mode == Mode.ROAD and not geo.road_connected(origin, destination):
        raise ValueError(f"No road connection between {origin} and {destination}")

    carrier_id = carrier_id or default_carrier(mode)
    carrier = CARRIERS_BY_ID.get(carrier_id)
    if carrier is None or carrier.mode != mode:
        raise ValueError(f"Carrier {carrier_id} does not operate {mode.value}")

    t = TARIFF[mode]
    if mode == Mode.SEA:
        via = list(via) if via is not None else geo.sea_paths(origin, destination)[0]
        distance = geo.path_distance_km([origin, *via, destination], t["detour"])
    else:
        via = []
        distance = geo.haversine_km(geo.PORTS[origin], geo.PORTS[destination]) * t["detour"]

    if mode == Mode.AIR:
        units = chargeable_air_kg(weight_kg, volume_cbm)
        cost = t["fixed"] + units * (t["per_kg_base"] + t["per_kg_per_km"] * distance)
        unit = "kg"
    elif mode == Mode.ROAD:
        units = teu_units(weight_kg, volume_cbm)
        cost = units * (t["fixed_per_unit"] + t["per_unit_per_km"] * distance)
        unit = "TEU"
    else:
        units = teu_units(weight_kg, volume_cbm)
        cost = units * (t["fixed_per_unit"] + t["per_unit_per_1000km"] * distance / 1000.0)
        unit = "TEU"

    cost *= carrier.rate_multiplier
    transit = t["handling_days"] + distance / t["km_per_day"]
    co2 = weight_kg / 1000.0 * distance * t["co2_g_per_tkm"] / 1000.0
    return FreightQuote(
        origin=origin,
        destination=destination,
        mode=mode.value,
        carrier_id=carrier_id,
        via=via,
        distance_km=round(distance, 1),
        chargeable_units=round(units, 2),
        unit=unit,
        cost_usd=round(cost, 2),
        transit_days=round(transit, 1),
        co2_kg=round(co2, 1),
    )
