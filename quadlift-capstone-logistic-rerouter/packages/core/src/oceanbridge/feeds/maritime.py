"""Simulated maritime operations feed (AIS-style port / canal status and vessel tracking)."""

from __future__ import annotations

from oceanbridge.core.geo import ALL_LOCATIONS, PORTS
from oceanbridge.feeds import scenarios
from oceanbridge.feeds._baseline import noise
from oceanbridge.models import Mode, Shipment


def get_port_status(location: str) -> dict:
    if location not in ALL_LOCATIONS:
        raise ValueError(f"Unknown location {location}")
    base = {"location": location, "port_status": "open", "vessels_holding": int(10 * noise("hold", location)),
            "source": "sim-ais"}
    base.update(scenarios.overlay("maritime", location))
    return base


def track_vessel(shipment: Shipment) -> dict:
    """Approximate position for a sea shipment: interpolated along its first sea leg."""
    sea = next((leg for leg in shipment.legs if leg.mode == Mode.SEA), None)
    if sea is None:
        return {"shipment_id": shipment.id, "tracking": "not a sea shipment"}
    o, d = PORTS[sea.origin], PORTS[sea.destination]
    progress = 0.0 if shipment.status.value == "booked" else round(0.2 + 0.6 * noise("prog", shipment.id), 2)
    return {
        "shipment_id": shipment.id,
        "vessel": f"MV {shipment.carrier_id.split('-')[-1]} {int(1000 * noise('imo', shipment.id))}",
        "lat": round(o.lat + (d.lat - o.lat) * progress, 3),
        "lon": round(o.lon + (d.lon - o.lon) * progress, 3),
        "speed_kts": round(14 + 5 * noise("spd", shipment.id), 1),
        "voyage_progress": progress,
        "source": "sim-ais",
    }
