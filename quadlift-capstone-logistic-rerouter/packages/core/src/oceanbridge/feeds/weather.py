"""Simulated marine weather feed (think NOAA / ECMWF marine forecast)."""

from __future__ import annotations

from oceanbridge.core.geo import ALL_LOCATIONS
from oceanbridge.feeds import scenarios
from oceanbridge.feeds._baseline import noise


def get_conditions(location: str) -> dict:
    if location not in ALL_LOCATIONS:
        raise ValueError(f"Unknown location {location}")
    base = {
        "location": location,
        "wind_kts": round(8 + 17 * noise("wind", location), 1),
        "wave_m": round(0.5 + 2.0 * noise("wave", location), 1),
        "storm_category": 0,
        "advisory": "none",
        "closure_days": 0,
        "source": "sim-marine-weather",
    }
    base.update(scenarios.overlay("weather", location))
    return base
