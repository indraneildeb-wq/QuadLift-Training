"""Canned disruption scenarios. Activating one overlays signals onto the simulated feeds;
the Disruption Monitoring Agent then has to *detect* the disruption from those raw signals."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from oceanbridge.core.db import ScenarioRow, session_scope
from oceanbridge.core.repository import utcnow


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    description: str
    weather: dict[str, dict] = field(default_factory=dict)
    congestion: dict[str, dict] = field(default_factory=dict)
    maritime: dict[str, dict] = field(default_factory=dict)
    geopolitics: dict[str, dict] = field(default_factory=dict)


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario(
            "shanghai_typhoon",
            "Super Typhoon hits Shanghai",
            "Category 4 typhoon forces closure of Shanghai terminals; Ningbo sees heavy weather.",
            weather={
                "CNSHA": {"wind_kts": 115, "wave_m": 9.5, "storm_category": 4, "advisory": "Typhoon warning - terminals closed", "closure_days": 5},
                "CNNGB": {"wind_kts": 40, "wave_m": 3.5, "storm_category": 0, "advisory": "Gale warning", "closure_days": 0},
            },
            maritime={"CNSHA": {"port_status": "closed", "vessels_holding": 150}},
        ),
        Scenario(
            "red_sea_crisis",
            "Red Sea security crisis",
            "Attacks on merchant vessels near Bab-el-Mandeb; carriers suspend Suez transits.",
            geopolitics={
                "BAB_EL_MANDEB": {"threat_level": 5, "alert": "Armed attacks on merchant shipping; JWC listed area", "expected_delay_days": 14},
                "SUEZ": {"threat_level": 4, "alert": "Transit suspensions by major carriers", "expected_delay_days": 10},
            },
        ),
        Scenario(
            "la_lb_congestion",
            "LA/Long Beach congestion",
            "Import surge and yard saturation at San Pedro Bay ports.",
            congestion={
                "USLAX": {"index": 94, "vessels_waiting": 58, "avg_wait_days": 9.0, "yard_utilisation": 0.97},
                "USLGB": {"index": 90, "vessels_waiting": 51, "avg_wait_days": 8.0, "yard_utilisation": 0.95},
            },
        ),
        Scenario(
            "rotterdam_strike",
            "Rotterdam dock-worker strike",
            "72-hour rolling strikes at Rotterdam container terminals.",
            maritime={"NLRTM": {"port_status": "strike", "vessels_holding": 22, "strike_days": 6}},
            congestion={"NLRTM": {"index": 78, "vessels_waiting": 22, "avg_wait_days": 4.0, "yard_utilisation": 0.9}},
        ),
        Scenario(
            "panama_drought",
            "Panama Canal drought restrictions",
            "Low Gatun Lake levels cut daily transits and draft limits.",
            maritime={"PANAMA": {"port_status": "restricted", "draft_limit_m": 13.4, "daily_transits": 22, "queue_days": 11}},
        ),
    ]
}


def activate(name: str) -> Scenario:
    if name not in SCENARIOS:
        raise KeyError(name)
    with session_scope() as s:
        if s.get(ScenarioRow, name) is None:
            s.add(ScenarioRow(name=name, activated_at=utcnow()))
    return SCENARIOS[name]


def deactivate(name: str) -> None:
    with session_scope() as s:
        row = s.get(ScenarioRow, name)
        if row:
            s.delete(row)


def clear_all() -> None:
    with session_scope() as s:
        for row in s.scalars(select(ScenarioRow)):
            s.delete(row)


def active() -> list[Scenario]:
    with session_scope() as s:
        names = [r.name for r in s.scalars(select(ScenarioRow))]
    return [SCENARIOS[n] for n in names if n in SCENARIOS]


def overlay(feed: str, location: str) -> dict:
    merged: dict = {}
    for sc in active():
        merged.update(getattr(sc, feed).get(location, {}))
    return merged
