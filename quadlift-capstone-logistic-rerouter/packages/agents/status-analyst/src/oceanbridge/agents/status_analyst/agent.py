"""Shipment Status Analyst: plain-language briefing for one shipment (status checks, light model)."""

from __future__ import annotations

from pathlib import Path

from oceanbridge.agents.common.base import AgentCall, offline_label
from oceanbridge.agents.common.context import RunContext
from oceanbridge.agents.common.crew_runner import load_config, run_crew
from oceanbridge.agents.common.mcp_tools import MCPToolbox
from oceanbridge.agents.status_analyst.schemas import StatusBriefing
from oceanbridge.core import service
from oceanbridge.llm.router import RoutingDecision

CONFIG = load_config(Path(__file__).parent)
MCP_TOOLS = ("get_shipment_status",)


def run_llm(ctx: RunContext, decision: RoutingDecision, toolbox: MCPToolbox, shipment_id: str) -> AgentCall:
    return run_crew(ctx, CONFIG, {"shipment_id": shipment_id}, StatusBriefing, decision, toolbox.get(*MCP_TOOLS))


def run_offline(ctx: RunContext, decision: RoutingDecision, shipment_id: str) -> AgentCall:
    st = service.get_shipment_status(shipment_id)
    s, r = st["shipment"], st["risk"]
    headline = (f"{shipment_id} {s['origin']}->{s['destination']} ({s['mode']}) "
                f"{'AT RISK' if r['at_risk'] else 'on track'}, ETA {s['eta']}")
    details = (f"{s['commodity']} for {s['customer']}, value ${s['cargo_value_usd']:,.0f}, status {s['status']}. "
               + (f"Risk {r['risk_score']:.2f}: {'; '.join(r['drivers'])}. Expected delay {r['expected_delay_days']}d."
                  if r["drivers"] else "No active disruptions on route.")
               + (f" Pending approval {st['pending_approval_id']}." if st["pending_approval_id"] else ""))
    return AgentCall(StatusBriefing(headline=headline, details=details), offline_label(decision))
