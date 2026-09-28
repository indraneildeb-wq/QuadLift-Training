"""Route & Capacity Optimization Agent: ranks the priced alternatives and explains the tradeoff."""

from __future__ import annotations

import json
from pathlib import Path

from oceanbridge.agents.common.base import AgentCall, offline_label
from oceanbridge.agents.common.context import RunContext
from oceanbridge.agents.common.crew_runner import load_config, run_crew
from oceanbridge.agents.common.formatting import compact_option
from oceanbridge.agents.common.mcp_tools import MCPToolbox
from oceanbridge.agents.route_optimizer.schemas import OptimizationChoice
from oceanbridge.config import get_settings
from oceanbridge.llm.router import RoutingDecision
from oceanbridge.models import RiskAssessment, RouteOption, Shipment

CONFIG = load_config(Path(__file__).parent)
MCP_TOOLS = ("list_route_alternatives", "calculate_freight_cost")


def explain_option(shipment: Shipment, opt: RouteOption, stay: RouteOption | None) -> str:
    parts = [f"{opt.label}: ${opt.total_cost_usd:,.0f} ({opt.cost_increase_pct:+.1f}% vs booked), "
             f"ETA {opt.projected_eta} ({opt.lead_time_delta_days:+.0f}d vs plan), residual risk {opt.residual_risk:.2f}"]
    if stay and opt.option_id != "STAY":
        saved = (stay.projected_eta - opt.projected_eta).days
        parts.append(f"lands {saved}d earlier than absorbing the disruption")
        parts.append(f"landed-cost score ${opt.landed_cost_score:,.0f} vs ${stay.landed_cost_score:,.0f} for staying")
    elif opt.option_id == "STAY":
        parts.append("no alternative beats the landed cost of absorbing the delay")
    if not opt.capacity_ok:
        parts.append("WARNING: carrier capacity is tight")
    late = (opt.projected_eta - shipment.required_delivery_date).days
    parts.append(f"{'meets' if late <= 0 else f'misses by {late}d'} the required delivery date {shipment.required_delivery_date}")
    return "; ".join(parts) + "."


def run_llm(ctx: RunContext, decision: RoutingDecision, toolbox: MCPToolbox, shipment: Shipment,
            assessment: RiskAssessment, options: list[RouteOption]) -> AgentCall:
    hitl = get_settings().hitl
    fields = {
        "shipment_id": shipment.id,
        "shipment": json.dumps({k: v for k, v in shipment.model_dump(mode="json").items() if k != "legs"}),
        "assessment": json.dumps(assessment.model_dump(mode="json")),
        "options": json.dumps([compact_option(o) for o in options], indent=1),
        "hitl_policy": f"auto-execute if cost increase < {hitl.max_cost_increase_pct}% AND cargo value < "
                       f"${hitl.max_cargo_value_usd:,.0f}; otherwise {hitl.approver_role} sign-off",
    }
    return run_crew(ctx, CONFIG, fields, OptimizationChoice, decision, toolbox.get(*MCP_TOOLS))


def run_offline(ctx: RunContext, decision: RoutingDecision, shipment: Shipment, assessment: RiskAssessment,
                options: list[RouteOption]) -> AgentCall:
    ranked = sorted(options, key=lambda o: (not o.capacity_ok, o.landed_cost_score))
    best = ranked[0]
    stay = next((o for o in options if o.option_id == "STAY"), None)
    choice = OptimizationChoice(selected_option_id=best.option_id, ranked_option_ids=[o.option_id for o in ranked],
                                rationale=explain_option(shipment, best, stay))
    return AgentCall(choice, offline_label(decision))
