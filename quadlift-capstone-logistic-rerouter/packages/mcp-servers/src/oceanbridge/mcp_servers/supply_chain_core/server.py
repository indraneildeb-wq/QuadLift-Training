"""Supply Chain Core API exposed over MCP.

Run (stdio, used by the CrewAI agents):   python -m oceanbridge.mcp_servers.supply_chain_core
Run (SSE, for MCP Inspector / remote):    python -m oceanbridge.mcp_servers.supply_chain_core --transport sse --port 8765
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from oceanbridge.core import service
from oceanbridge.models import SLATerms

mcp = FastMCP(
    "oceanbridge-supply-chain-core",
    log_level="WARNING",  # per-request INFO logs would flood the API console when agents call tools
    instructions=(
        "OceanBridge Supply Chain Core. Use get_shipment_status for live status/risk, list_route_alternatives "
        "for priced reroute options, calculate_freight_cost for ad-hoc quotes, submit_sla_proposal to negotiate "
        "with the carrier, and issue_purchase_order to commit. issue_purchase_order enforces the HITL policy: "
        "reroutes with cost increase >= 5% or cargo value >= $250k need an APPROVED approval_id."
    ),
)


def _err(e: Exception) -> dict[str, Any]:
    return {"ok": False, "error": type(e).__name__, "message": str(e)}


@mcp.tool()
def get_shipment_status(shipment_id: str) -> dict[str, Any]:
    """Current status of a shipment: routing legs, ETA, cargo value, booked cost, live vessel tracking,
    disruption risk assessment, purchase orders and any pending approval."""
    try:
        return {"ok": True, **service.get_shipment_status(shipment_id)}
    except Exception as e:
        return _err(e)


@mcp.tool()
def calculate_freight_cost(origin: str, destination: str, mode: str, weight_kg: float, volume_cbm: float,
                           carrier_id: str | None = None, via: list[str] | None = None) -> dict[str, Any]:
    """Quote one freight leg. mode is one of sea|air|rail|road. origin/destination are UN/LOCODE port codes
    (e.g. CNSHA, NLRTM). via optionally forces sea chokepoints, e.g. ["MALACCA","CAPE"].
    Returns cost_usd, transit_days, distance_km, co2_kg and which cache tier served it."""
    try:
        return {"ok": True, **service.calculate_freight_cost(origin, destination, mode, weight_kg, volume_cbm,
                                                             carrier_id, via)}
    except Exception as e:
        return _err(e)


@mcp.tool()
def list_route_alternatives(shipment_id: str, max_options: int = 8) -> dict[str, Any]:
    """Priced reroute alternatives (sea path changes, alternate ports, rail, air, sea-air, land bridge) for a
    shipment given active disruptions, sorted by landed-cost score. Option 'STAY' keeps the current booking."""
    try:
        opts = service.list_route_alternatives(shipment_id, max_options)
        return {"ok": True, "options": [o.model_dump(mode="json") for o in opts]}
    except Exception as e:
        return _err(e)


@mcp.tool()
def submit_sla_proposal(shipment_id: str, option_id: str, offered_amount_usd: float,
                        guaranteed_transit_days: float, late_penalty_pct_per_day: float, max_penalty_pct: float,
                        reserved_capacity: str = "Priority loading", free_demurrage_days: int = 3,
                        notes: str = "") -> dict[str, Any]:
    """Send a price + SLA proposal to the carrier operating the option's main leg. The carrier either accepts
    or counters (max 3 rounds per carrier). Use the counter terms to converge."""
    try:
        terms = SLATerms(guaranteed_transit_days=guaranteed_transit_days,
                         late_penalty_pct_per_day=late_penalty_pct_per_day, max_penalty_pct=max_penalty_pct,
                         reserved_capacity=reserved_capacity, free_demurrage_days=free_demurrage_days, notes=notes)
        return {"ok": True, **service.submit_sla_proposal(shipment_id, option_id, offered_amount_usd, terms)
                .model_dump(mode="json")}
    except Exception as e:
        return _err(e)


@mcp.tool()
def issue_purchase_order(shipment_id: str, option_id: str, amount_usd: float, sla_json: str,
                         approval_id: str | None = None) -> dict[str, Any]:
    """Issue the adjusted purchase order for a reroute and supersede the original PO. sla_json is the agreed
    SLA as JSON with keys guaranteed_transit_days, late_penalty_pct_per_day, max_penalty_pct, reserved_capacity,
    free_demurrage_days, notes. Refused unless the HITL policy allows auto-execution or approval_id refers to an
    APPROVED Logistics Operations Manager sign-off."""
    try:
        sla = SLATerms(**json.loads(sla_json))
        po = service.issue_purchase_order(shipment_id, option_id, amount_usd, sla, approval_id, actor="mcp-client")
        return {"ok": True, "purchase_order": po.model_dump(mode="json")}
    except Exception as e:
        return _err(e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    if args.transport != "stdio":
        mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
