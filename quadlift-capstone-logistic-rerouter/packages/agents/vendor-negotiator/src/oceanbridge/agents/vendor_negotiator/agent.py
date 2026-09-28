"""Vendor Negotiation & PO Agent: negotiates price + SLA with the carrier, then issues the adjusted PO."""

from __future__ import annotations

import json
from pathlib import Path

from oceanbridge.agents.common.base import AgentCall, offline_label
from oceanbridge.agents.common.context import RunContext
from oceanbridge.agents.common.crew_runner import load_config, run_crew
from oceanbridge.agents.common.formatting import compact_option
from oceanbridge.agents.common.mcp_tools import MCPToolbox
from oceanbridge.agents.vendor_negotiator.schemas import NegotiationSummary
from oceanbridge.core import repository as repo
from oceanbridge.core import service
from oceanbridge.core.reference import CARRIERS_BY_ID
from oceanbridge.llm.router import RoutingDecision
from oceanbridge.models import NegotiationResult, RouteOption, Shipment

CONFIG = load_config(Path(__file__).parent)
MCP_TOOLS = ("submit_sla_proposal", "issue_purchase_order", "get_shipment_status")
OPENING_DISCOUNT = 0.08


def negotiate_deterministic(shipment: Shipment, option: RouteOption, approval_id: str | None) -> NegotiationResult:
    """Anchor below list, split the difference with each counter, accept the carrier's final counter."""
    list_price = option.total_cost_usd
    offer = round(list_price * (1 - OPENING_DISCOUNT), 2)
    terms = service.requested_sla(option)
    rounds, agreed = 0, None
    for _ in range(service.MAX_NEGOTIATION_ROUNDS):
        rounds += 1
        resp = service.submit_sla_proposal(shipment.id, option.option_id, offer, terms)
        if resp.accepted:
            agreed = offer
            break
        terms = resp.counter_terms
        if rounds == service.MAX_NEGOTIATION_ROUNDS - 1:
            offer = resp.counter_amount_usd  # final round: meet the counter so the carrier accepts
        else:
            offer = round((offer + resp.counter_amount_usd) / 2, 2)
    if agreed is None:
        agreed = min(list_price, offer)
    agreed = min(agreed, list_price)
    po = service.issue_purchase_order(shipment.id, option.option_id, agreed, terms, approval_id)
    carrier = CARRIERS_BY_ID[option.primary_carrier_id]
    msg = (f"Dear {carrier.name}, confirming PO {po.po_id} for shipment {shipment.id} ({option.label}) at "
           f"${agreed:,.2f}. SLA: guaranteed transit {terms.guaranteed_transit_days} days, late penalty "
           f"{terms.late_penalty_pct_per_day}%/day capped at {terms.max_penalty_pct}%, "
           f"{terms.free_demurrage_days} free demurrage days, {terms.reserved_capacity}. "
           f"This PO supersedes {po.supersedes_po_id or 'no prior PO'}. - OceanBridge Procurement")
    return NegotiationResult(shipment_id=shipment.id, po=po, rounds=rounds, list_price_usd=list_price,
                             final_amount_usd=agreed, savings_usd=round(list_price - agreed, 2), carrier_message=msg)


def run_offline(ctx: RunContext, decision: RoutingDecision, shipment: Shipment, option: RouteOption,
                approval_id: str | None) -> AgentCall:
    return AgentCall(negotiate_deterministic(shipment, option, approval_id), offline_label(decision))


def run_llm(ctx: RunContext, decision: RoutingDecision, toolbox: MCPToolbox, shipment: Shipment,
            option: RouteOption, approval_id: str | None) -> AgentCall:
    before = shipment.po_id
    fields = {
        "shipment_id": shipment.id, "option_id": option.option_id,
        "option": json.dumps(compact_option(option)), "list_price": option.total_cost_usd,
        "sla_ask": service.requested_sla(option).model_dump_json(), "approval_id": approval_id or "none",
    }
    call = run_crew(ctx, CONFIG, fields, NegotiationSummary, decision, toolbox.get(*MCP_TOOLS))
    summary: NegotiationSummary = call.output
    after = repo.get_shipment(shipment.id)
    po = repo.get_po(summary.po_id) if summary and summary.po_id else None
    if po is None and after and after.po_id != before:
        po = repo.get_po(after.po_id)  # the agent issued the PO but mis-reported the id
    if po is None or po.shipment_id != shipment.id:
        # The LLM did not commit a PO: finish deterministically (the same HITL guard applies).
        ctx.trace("negotiate", f"{shipment.id}: LLM negotiator did not issue a PO; deterministic fallback",
                  shipment_id=shipment.id)
        result = negotiate_deterministic(shipment, option, approval_id)
        return AgentCall(result, f"{decision.model}+fallback", call.prompt_tokens, call.completion_tokens)
    result = NegotiationResult(
        shipment_id=shipment.id, po=po, rounds=summary.rounds, list_price_usd=option.total_cost_usd,
        final_amount_usd=po.amount_usd, savings_usd=round(option.total_cost_usd - po.amount_usd, 2),
        carrier_message=summary.carrier_message, model_used=decision.model,
    )
    return AgentCall(result, decision.model, call.prompt_tokens, call.completion_tokens)
