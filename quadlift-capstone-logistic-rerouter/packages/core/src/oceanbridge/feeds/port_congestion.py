"""Simulated port congestion index feed (0-100; >70 is considered congested)."""

from __future__ import annotations

from oceanbridge.core.geo import PORTS
from oceanbridge.feeds import scenarios
from oceanbridge.feeds._baseline import noise


def get_index(port: str) -> dict:
    if port not in PORTS:
        raise ValueError(f"Unknown port {port}")
    idx = 25 + 35 * noise("cong", port)
    base = {
        "port": port,
        "index": round(idx, 1),
        "vessels_waiting": int(idx / 6),
        "avg_wait_days": round(idx / 40, 1),
        "yard_utilisation": round(0.5 + idx / 250, 2),
        "source": "sim-port-congestion-index",
    }
    base.update(scenarios.overlay("congestion", port))
    return base
