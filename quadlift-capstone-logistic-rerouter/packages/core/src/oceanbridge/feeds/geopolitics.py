"""Simulated geopolitical / security risk feed (threat level 1-5)."""

from __future__ import annotations

from oceanbridge.core.geo import ALL_LOCATIONS
from oceanbridge.feeds import scenarios
from oceanbridge.feeds._baseline import noise


def get_alerts(location: str) -> dict:
    if location not in ALL_LOCATIONS:
        raise ValueError(f"Unknown location {location}")
    base = {"location": location, "threat_level": 1 + int(2 * noise("geo", location)), "alert": "none",
            "expected_delay_days": 0, "source": "sim-geo-risk"}
    base.update(scenarios.overlay("geopolitics", location))
    return base
