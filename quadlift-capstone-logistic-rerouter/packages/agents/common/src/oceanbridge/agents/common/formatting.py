"""Compact, LLM-friendly views of domain objects used in agent prompts."""

from __future__ import annotations

from oceanbridge.models import RouteOption


def compact_option(o: RouteOption) -> dict:
    return {
        "option_id": o.option_id, "label": o.label, "mode": o.mode.value,
        "legs": [f"{leg.mode.value} {leg.origin}->{leg.destination}"
                 + (f" via {'/'.join(leg.via)}" if leg.via else "") + f" [{leg.carrier_id}]" for leg in o.legs],
        "total_cost_usd": o.total_cost_usd, "cost_increase_pct": o.cost_increase_pct,
        "transit_days": o.transit_days, "projected_eta": str(o.projected_eta),
        "lead_time_delta_days": o.lead_time_delta_days, "residual_risk": o.residual_risk,
        "co2_kg": o.co2_kg, "capacity_ok": o.capacity_ok, "landed_cost_score": o.landed_cost_score,
    }
