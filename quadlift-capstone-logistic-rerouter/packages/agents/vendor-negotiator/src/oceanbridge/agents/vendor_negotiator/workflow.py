"""Negotiate-and-commit step, used by the pipeline (auto-executed reroutes) and by HITL approvals."""

from __future__ import annotations

from oceanbridge.agents.common.base import AgentBackend
from oceanbridge.agents.common.context import RunContext, Timer
from oceanbridge.llm.router import ComplexitySignals, TaskType
from oceanbridge.models import RouteOption, Shipment


def negotiate_reroute(backend: AgentBackend, ctx: RunContext, shipment: Shipment, option: RouteOption,
                      approval_id: str | None) -> dict:
    decision = ctx.router.select(TaskType.NEGOTIATION, ComplexitySignals(cargo_value_usd=shipment.cargo_value_usd,
                                                                        needs_approval=approval_id is not None))
    with Timer() as t:
        call = backend.negotiate(ctx, shipment, option, approval_id, decision)
    ctx.usage(decision, call.prompt_tokens, call.completion_tokens, executed_model=call.model)
    r = call.output
    ctx.trace("negotiate", f"{shipment.id}: PO {r.po.po_id} ${r.final_amount_usd:,.2f} after {r.rounds} round(s) "
                           f"(saved ${r.savings_usd:,.2f} vs list)", shipment_id=shipment.id, model=call.model,
              duration_ms=t.ms, agent="Vendor Negotiation & PO Agent")
    return {"shipment_id": shipment.id, "po_id": r.po.po_id, "amount_usd": r.final_amount_usd,
            "list_price_usd": r.list_price_usd, "rounds": r.rounds, "carrier_message": r.carrier_message,
            "approval_id": approval_id, "option": option.label, "model": call.model}
