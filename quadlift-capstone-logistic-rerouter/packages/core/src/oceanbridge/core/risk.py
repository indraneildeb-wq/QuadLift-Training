"""Deterministic disruption detection (sensor fusion over feeds) and shipment risk scoring.

Both agent backends use this as the reproducible pre-score; the LLM monitoring agent then
contextualises and may adjust it."""

from __future__ import annotations

import math
from datetime import date, timedelta

from oceanbridge.config import get_settings
from oceanbridge.core.geo import ALL_LOCATIONS, PORTS
from oceanbridge.feeds import geopolitics, maritime, port_congestion, weather
from oceanbridge.models import Disruption, DisruptionType, Leg, Mode, RiskAssessment, Shipment, ShipmentStatus


def _mk(dtype: DisruptionType, loc: str, severity: float, delay: float, desc: str, source: str) -> Disruption:
    today = date.today()
    return Disruption(
        id=f"DSR-{dtype.value}-{loc}",
        type=dtype,
        location=loc,
        severity=round(min(1.0, max(0.0, severity)), 2),
        expected_delay_days=round(delay, 1),
        start_date=today,
        end_date=today + timedelta(days=max(1, math.ceil(delay))),
        description=desc,
        source=source,
    )


def detect_at(location: str) -> list[Disruption]:
    """Turn raw feed readings for one location into disruption events."""
    found: list[Disruption] = []
    w = weather.get_conditions(location)
    if w["storm_category"] >= 1 or w["wind_kts"] >= 34:
        sev = min(1.0, w["wind_kts"] / 120 + 0.1 * w["storm_category"])
        # Terminal closure time is modelled by the PORT_CLOSURE event; weather adds the storm passage itself.
        delay = 1.5 + 0.5 * w["storm_category"]
        found.append(_mk(DisruptionType.WEATHER, location, sev, delay,
                         f"{w['advisory']} (wind {w['wind_kts']} kts, waves {w['wave_m']} m)", w["source"]))

    m = maritime.get_port_status(location)
    status = m.get("port_status", "open")
    if status == "closed":
        found.append(_mk(DisruptionType.PORT_CLOSURE, location, 0.95, 5 + m.get("vessels_holding", 0) / 30,
                         f"Port closed; {m.get('vessels_holding', 0)} vessels holding", m["source"]))
    elif status == "strike":
        found.append(_mk(DisruptionType.LABOR, location, 0.7, m.get("strike_days", 3),
                         f"Labour action; {m.get('vessels_holding', 0)} vessels holding", m["source"]))
    elif status == "restricted":
        found.append(_mk(DisruptionType.CANAL_RESTRICTION, location, 0.6, m.get("queue_days", 5),
                         f"Transit restrictions (draft {m.get('draft_limit_m')} m, {m.get('daily_transits')} transits/day)",
                         m["source"]))

    if location in PORTS:
        c = port_congestion.get_index(location)
        if c["index"] >= 70:
            found.append(_mk(DisruptionType.PORT_CONGESTION, location, (c["index"] - 50) / 50, c["avg_wait_days"],
                             f"Congestion index {c['index']}; {c['vessels_waiting']} vessels waiting, "
                             f"yard {int(c['yard_utilisation'] * 100)}%", c["source"]))

    g = geopolitics.get_alerts(location)
    if g["threat_level"] >= 3:
        found.append(_mk(DisruptionType.GEOPOLITICAL, location, g["threat_level"] / 5,
                         g.get("expected_delay_days", 3), g["alert"], g["source"]))
    return found


def detect_all() -> list[Disruption]:
    out: list[Disruption] = []
    for code in ALL_LOCATIONS:
        out.extend(detect_at(code))
    return out


# Which disruption types affect a leg of each mode at the locations it touches.
_ALL = set(DisruptionType)
LEG_EXPOSURE: dict[Mode, set[DisruptionType]] = {
    Mode.SEA: _ALL,
    Mode.RAIL: {DisruptionType.WEATHER, DisruptionType.LABOR, DisruptionType.PORT_CONGESTION},
    Mode.ROAD: {DisruptionType.WEATHER},
    Mode.AIR: {DisruptionType.WEATHER},
}


def applicable_disruptions(legs: list[Leg], disruptions: list[Disruption], skip: set[str] | None = None) -> list[Disruption]:
    """Disruptions that actually hit a set of legs (each counted once)."""
    skip = skip or set()
    hit: dict[str, Disruption] = {}
    for leg in legs:
        exposure = LEG_EXPOSURE[leg.mode]
        points = {leg.origin, leg.destination, *leg.via} - skip
        for d in disruptions:
            if d.location in points and d.type in exposure:
                hit[d.id] = d
    return list(hit.values())


def relevant_disruptions(shipment: Shipment, disruptions: list[Disruption]) -> list[Disruption]:
    skip = {shipment.origin} if shipment.status == ShipmentStatus.IN_TRANSIT else set()  # already departed
    return applicable_disruptions(shipment.legs, disruptions, skip)


def combine_risk(disruptions: list[Disruption]) -> tuple[float, float]:
    """(risk score, expected delay days). Delay is the worst single delay plus 50% of the others."""
    if not disruptions:
        return 0.0, 0.0
    risk = 1 - math.prod(1 - d.severity for d in disruptions)
    # Several disruptions at one location (e.g. closure + weather) overlap heavily - keep the worst per location.
    per_loc: dict[str, float] = {}
    for d in disruptions:
        per_loc[d.location] = max(per_loc.get(d.location, 0.0), d.expected_delay_days)
    delays = sorted(per_loc.values(), reverse=True)
    delay = delays[0] + 0.5 * sum(delays[1:])
    return round(risk, 3), round(delay, 1)


def assess(shipment: Shipment, disruptions: list[Disruption]) -> RiskAssessment:
    cfg = get_settings().monitoring
    rel = relevant_disruptions(shipment, disruptions)
    risk, delay = combine_risk(rel)
    projected = shipment.eta + timedelta(days=math.ceil(delay))
    misses = delay > 0 and projected > shipment.required_delivery_date
    at_risk = bool(rel) and (risk >= cfg.risk_threshold or delay >= cfg.delay_threshold_days or misses)
    drivers = [f"{d.type.value} @ {d.location} (sev {d.severity:.2f}, +{d.expected_delay_days}d)" for d in rel]
    return RiskAssessment(
        shipment_id=shipment.id,
        risk_score=risk,
        expected_delay_days=delay,
        disruption_ids=[d.id for d in rel],
        drivers=drivers,
        at_risk=at_risk,
        projected_eta=projected,
        misses_required_date=misses,
    )
