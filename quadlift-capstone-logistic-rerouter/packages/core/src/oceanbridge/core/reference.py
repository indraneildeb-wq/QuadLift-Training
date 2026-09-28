"""Static carrier master data (mock). Remaining capacity lives in the database."""

from __future__ import annotations

from dataclasses import dataclass

from oceanbridge.models import Mode


@dataclass(frozen=True)
class CarrierProfile:
    id: str
    name: str
    mode: Mode
    rate_multiplier: float   # applied to the base tariff
    reliability: float       # on-time performance 0..1
    flexibility: float       # willingness to concede in negotiation 0..1
    capacity: float          # initial capacity: TEU for sea/rail/road, kg for air
    min_margin: float        # lowest acceptable price as a fraction of list price

    @property
    def capacity_unit(self) -> str:
        return "kg" if self.mode == Mode.AIR else "TEU"


CARRIERS: list[CarrierProfile] = [
    CarrierProfile("SEA-OCL", "Oceanic Container Line", Mode.SEA, 1.00, 0.86, 0.55, 400, 0.93),
    CarrierProfile("SEA-BWS", "BlueWave Shipping", Mode.SEA, 0.95, 0.78, 0.35, 300, 0.95),
    CarrierProfile("SEA-MCL", "Meridian Container Lines", Mode.SEA, 1.06, 0.92, 0.60, 250, 0.92),
    CarrierProfile("SEA-PSL", "Pacific Star Lines", Mode.SEA, 0.98, 0.83, 0.45, 350, 0.94),
    CarrierProfile("AIR-SKB", "SkyBridge Cargo", Mode.AIR, 1.00, 0.95, 0.40, 120_000, 0.94),
    CarrierProfile("AIR-KES", "Kestrel Air Cargo", Mode.AIR, 0.93, 0.90, 0.55, 80_000, 0.92),
    CarrierProfile("RAIL-SRE", "Silk Rail Express", Mode.RAIL, 1.00, 0.88, 0.50, 120, 0.93),
    CarrierProfile("RAIL-TCR", "TransContinental Rail", Mode.RAIL, 0.97, 0.91, 0.45, 160, 0.94),
    CarrierProfile("ROAD-PLT", "PortLink Trucking", Mode.ROAD, 1.00, 0.95, 0.30, 500, 0.96),
]

CARRIERS_BY_ID: dict[str, CarrierProfile] = {c.id: c for c in CARRIERS}


def carriers_for(mode: Mode) -> list[CarrierProfile]:
    return [c for c in CARRIERS if c.mode == mode]
