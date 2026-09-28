"""Tools the Chat Assistant can call.

  source "mcp"    served by the Supply Chain Core MCP server (called through MCPToolbox, over stdio)
  source "local"  in-process functions of this application (pipeline, approvals, scenarios, metrics)

  effect "read"     no side effects
  effect "write"    changes state but is safe to run on request (scenarios, pipeline run, what-if proposals)
  effect "confirm"  only *proposes* an action; it runs after the user replies "confirm" (approve / reject)

The assistant is never given submit_sla_proposal or issue_purchase_order: money is committed only by the
pipeline or by an approval, both of which go through the HITL gate.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from oceanbridge.agents.common.mcp_tools import MCPToolbox
from oceanbridge.cache.semantic_cache import all_caches
from oceanbridge.config import get_settings
from oceanbridge.core import repository as repo
from oceanbridge.core.risk import assess
from oceanbridge.feeds import scenarios
from oceanbridge.models import ApprovalStatus


@dataclass
class ToolContext:
    """Everything a tool may use. The pipeline functions are passed in by the app, so this package does not
    depend on the app (oceanbridge.flow)."""
    session_id: str
    user: str
    state: dict
    toolbox_factory: Callable[[], MCPToolbox]
    run_pipeline: Callable[..., dict] | None = None
    optimize_what_if: Callable[..., dict] | None = None


@dataclass
class ToolCallRecord:
    tool: str
    source: str
    effect: str
    arguments: dict
    ok: bool
    summary: str
    duration_ms: float

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ChatTool:
    name: str
    description: str
    parameters: dict
    source: str
    effect: str
    handler: Callable[[dict, ToolContext], dict]
    summarize: Callable[[dict], str] = field(default=lambda r: "done")

    def openai_schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


S = {"type": "string"}
N = {"type": "number"}
I = {"type": "integer"}
SHIPMENT = {"type": "string", "description": "Shipment id, e.g. SHP-1004"}
APPROVAL = {"type": "string", "description": "Approval id, e.g. APR-1A2B3C4D"}

# ---------------------------------------------------------------------------- MCP-backed tools
_mcp_lock = threading.Lock()


def _mcp(name: str):
    def call(args: dict, ctx: ToolContext) -> dict:
        with _mcp_lock:  # one stdio session per process; serialise calls on it
            tool = ctx.toolbox_factory().get(name)[0]
            raw = tool.run(**{k: v for k, v in args.items() if v is not None})
        return json.loads(raw) if isinstance(raw, str) else raw
    return call


def _compact_status(r: dict) -> dict:
    if not r.get("ok"):
        return r
    s, risk = r["shipment"], r["risk"]
    return {
        "ok": True,
        "shipment": {k: s[k] for k in ("id", "customer", "commodity", "origin", "destination", "mode", "status",
                                       "carrier_id", "cargo_value_usd", "current_cost_usd", "eta",
                                       "required_delivery_date", "po_id")},
        "route": [f"{leg['mode']} {leg['origin']}->{leg['destination']}"
                  + (f" via {'/'.join(leg['via'])}" if leg.get("via") else "") for leg in s["legs"]],
        "risk": {k: risk[k] for k in ("risk_score", "at_risk", "expected_delay_days", "projected_eta", "drivers")},
        "purchase_orders": [{"po_id": p["po_id"], "status": p["status"], "amount_usd": p["amount_usd"]}
                            for p in r.get("purchase_orders", [])][:5],
        "pending_approval_id": r.get("pending_approval_id"),
    }


def _compact_alternatives(r: dict) -> dict:
    if not r.get("ok"):
        return r
    keep = ("option_id", "label", "mode", "total_cost_usd", "cost_increase_pct", "transit_days", "projected_eta",
            "lead_time_delta_days", "residual_risk", "co2_kg", "capacity_ok", "landed_cost_score")
    return {"ok": True, "options": [{k: o[k] for k in keep} for o in r["options"]]}


def get_shipment_status(args, ctx):
    return _compact_status(_mcp("get_shipment_status")(args, ctx))


def list_route_alternatives(args, ctx):
    return _compact_alternatives(_mcp("list_route_alternatives")({"max_options": 5, **args}, ctx))


def calculate_freight_cost(args, ctx):
    return _mcp("calculate_freight_cost")(args, ctx)


# ---------------------------------------------------------------------------- local tools
def list_shipments(args, ctx):
    flt = args.get("filter", "at_risk")
    active = repo.active_disruptions()
    rows = []
    for s in repo.list_shipments():
        a = assess(s, active)
        if flt == "at_risk" and not a.at_risk:
            continue
        if flt in ("booked", "in_transit", "rerouted", "pending_approval") and s.status.value != flt:
            continue
        if args.get("origin") and s.origin != args["origin"].upper():
            continue
        if args.get("destination") and s.destination != args["destination"].upper():
            continue
        rows.append({"shipment_id": s.id, "route": f"{s.origin}->{s.destination}", "mode": s.mode.value,
                     "status": s.status.value, "cargo_value_usd": s.cargo_value_usd, "eta": str(s.eta),
                     "risk_score": a.risk_score, "expected_delay_days": a.expected_delay_days})
    rows.sort(key=lambda r: -r["risk_score"])
    return {"ok": True, "filter": flt, "count": len(rows), "shipments": rows[: args.get("limit", 10)]}


def list_disruptions(args, ctx):
    ds = repo.active_disruptions()
    return {"ok": True, "count": len(ds), "disruptions": [
        {"id": d.id, "type": d.type.value, "location": d.location, "severity": d.severity,
         "expected_delay_days": d.expected_delay_days, "description": d.description} for d in ds]}


def list_scenarios(args, ctx):
    active = {s.name for s in scenarios.active()}
    return {"ok": True, "scenarios": [{"name": s.name, "title": s.title, "active": s.name in active}
                                      for s in scenarios.SCENARIOS.values()]}


def _scenario_name(value: str | None) -> str | None:
    name = (value or "").strip().lower().replace(" ", "_")
    return name if name in scenarios.SCENARIOS else None


def activate_scenario(args, ctx):
    name = _scenario_name(args.get("name"))
    if not name:
        return {"ok": False, "error": "UnknownScenario", "valid_names": list(scenarios.SCENARIOS)}
    scenarios.activate(name)
    return {"ok": True, "activated": name, "title": scenarios.SCENARIOS[name].title,
            "note": "Feeds now show this disruption. Run the rerouting pipeline to detect it and reroute shipments."}


def deactivate_scenario(args, ctx):
    name = _scenario_name(args.get("name"))
    if not name:
        return {"ok": False, "error": "UnknownScenario", "valid_names": list(scenarios.SCENARIOS)}
    scenarios.deactivate(name)
    return {"ok": True, "deactivated": name}


def run_rerouting_pipeline(args, ctx):
    if ctx.run_pipeline is None:
        return {"ok": False, "error": "The pipeline is not available in this context"}
    scenario = _scenario_name(args.get("scenario")) if args.get("scenario") else None
    if args.get("scenario") and not scenario:
        return {"ok": False, "error": "UnknownScenario", "valid_names": list(scenarios.SCENARIOS)}
    r = ctx.run_pipeline(scenario=scenario)
    return {"ok": True, "run_id": r["run_id"], "situation_report": r["situation_report"],
            "disruptions": r["disruptions"], "at_risk": r["at_risk"],
            "auto_executed": [{"shipment_id": x["shipment_id"], "po_id": x["po_id"], "amount_usd": x["amount_usd"],
                               "route": x["option"]} for x in r["auto_executed"]],
            "queued_for_approval": [{"approval_id": q["approval_id"], "shipment_id": q["shipment_id"],
                                     "route": q["option"], "cost_increase_pct": q["cost_increase_pct"]}
                                    for q in r["queued_for_approval"]],
            "kept_current_routing": len(r["kept_current_routing"]), "errors": r["errors"]}


def _approval_view(a) -> dict:
    return {"approval_id": a.id, "shipment_id": a.shipment_id, "status": a.status.value, "route": a.option.label,
            "new_cost_usd": a.option.total_cost_usd, "cost_increase_pct": a.option.cost_increase_pct,
            "projected_eta": str(a.option.projected_eta), "cargo_value_usd": a.decision.cargo_value_usd,
            "reasons": a.decision.reasons, "approver": a.approver}


def list_approvals(args, ctx):
    status = args.get("status", "pending")
    items = repo.list_approvals(None if status == "all" else ApprovalStatus(status))
    return {"ok": True, "status": status, "count": len(items),
            "approvals": [_approval_view(a) for a in items[: args.get("limit", 10)]]}


def get_approval(args, ctx):
    a = repo.get_approval(str(args["approval_id"]).upper())
    if a is None:
        return {"ok": False, "error": "UnknownApproval", "approval_id": args["approval_id"]}
    return {"ok": True, **_approval_view(a), "rationale": a.rationale,
            "alternatives": [{"route": o.label, "cost_increase_pct": o.cost_increase_pct,
                              "projected_eta": str(o.projected_eta)} for o in a.alternatives]}


def decide_approval(args, ctx):
    """Proposes approve/reject. Nothing happens until the user confirms (handled in chat.service)."""
    approval_id = str(args["approval_id"]).upper()
    decision = args.get("decision", "").lower()
    if decision not in ("approve", "reject"):
        return {"ok": False, "error": "decision must be 'approve' or 'reject'"}
    a = repo.get_approval(approval_id)
    if a is None:
        return {"ok": False, "error": "UnknownApproval", "approval_id": approval_id}
    if a.status != ApprovalStatus.PENDING:
        return {"ok": False, "error": f"Approval {approval_id} is {a.status.value}, not pending"}
    ctx.state["pending_action"] = {"type": "approval_decision", "approval_id": approval_id, "decision": decision,
                                   "comment": args.get("comment"), "shipment_id": a.shipment_id,
                                   "route": a.option.label}
    return {"ok": True, "requires_confirmation": True, "approval_id": approval_id, "decision": decision,
            "shipment_id": a.shipment_id, "route": a.option.label, "cost_increase_pct": a.option.cost_increase_pct,
            "message": f"Reply 'confirm' to {decision} {approval_id} as {ctx.user}, or 'cancel'."}


def what_if_optimize(args, ctx):
    if ctx.optimize_what_if is None:
        return {"ok": False, "error": "What-if optimisation is not available in this context"}
    overrides = {k: args[k] for k in ("weight_kg", "volume_cbm", "cargo_value_usd") if args.get(k) is not None}
    r = ctx.optimize_what_if(str(args["shipment_id"]).upper(), overrides or None)
    chosen = next(o for o in r["options"] if o["option_id"] == r["selected_option_id"])
    return {"ok": True, "shipment_id": r["shipment_id"], "overrides": overrides,
            "selected_option_id": chosen["option_id"], "recommended": chosen["label"],
            "cost_increase_pct": chosen["cost_increase_pct"], "total_cost_usd": chosen["total_cost_usd"],
            "projected_eta": chosen["projected_eta"], "cache_tier": r["cache_tier"], "similarity": r["similarity"],
            "auto_execute": r["hitl"]["auto_execute"], "hitl_reasons": r["hitl"]["reasons"],
            "rationale": r["rationale"]}


def get_hitl_policy(args, ctx):
    h = get_settings().hitl
    return {"ok": True, "max_cost_increase_pct": h.max_cost_increase_pct, "max_cargo_value_usd": h.max_cargo_value_usd,
            "approver_role": h.approver_role,
            "rule": f"Auto-execute only if cost increase < {h.max_cost_increase_pct}% AND cargo value < "
                    f"${h.max_cargo_value_usd:,.0f}; otherwise {h.approver_role} sign-off."}


def get_system_metrics(args, ctx):
    usage = repo.llm_usage_rows(2000)
    return {"ok": True,
            "cache": {ns: {k: c.snapshot()[k] for k in ("hit_rate", "lookups", "l1_hits", "l3_hits", "l2_hits",
                                                        "misses", "invalidated")}
                      for ns, c in all_caches().items()},
            "model_calls": dict(Counter(u["model"] for u in usage)),
            "llm_cost_usd": round(sum(u["cost_usd"] for u in usage), 4),
            "approvals": dict(Counter(a.status.value for a in repo.list_approvals())),
            "reroute_pos": sum(not p.po_id.endswith("-ORIG") for p in repo.list_pos())}


def list_purchase_orders(args, ctx):
    pos = repo.list_pos(args.get("shipment_id"))
    if not args.get("shipment_id"):
        pos = [p for p in pos if not p.po_id.endswith("-ORIG")]
    return {"ok": True, "count": len(pos), "purchase_orders": [
        {"po_id": p.po_id, "shipment_id": p.shipment_id, "carrier_id": p.carrier_id, "amount_usd": p.amount_usd,
         "route": p.route_label, "status": p.status, "auto_executed": p.auto_executed, "approval_id": p.approval_id}
        for p in pos[: args.get("limit", 10)]]}


# ---------------------------------------------------------------------------- registry
def _err(r: dict) -> str | None:
    return None if r.get("ok", True) else f"error: {r.get('message') or r.get('error')}"


TOOLS: dict[str, ChatTool] = {t.name: t for t in [
    ChatTool("get_shipment_status",
             "Live status of one shipment: route, ETA, cargo value, booked cost, disruption risk, POs, pending approval.",
             _obj({"shipment_id": SHIPMENT}, ["shipment_id"]), "mcp", "read", get_shipment_status,
             lambda r: _err(r) or f"{r['shipment']['id']} {r['shipment']['status']}, risk {r['risk']['risk_score']}"),
    ChatTool("list_route_alternatives",
             "Priced reroute options for a shipment under current disruptions, best landed cost first. STAY = keep booking.",
             _obj({"shipment_id": SHIPMENT, "max_options": I}, ["shipment_id"]), "mcp", "write", list_route_alternatives,
             lambda r: _err(r) or f"{len(r['options'])} options, best {r['options'][0]['option_id']}"),
    ChatTool("calculate_freight_cost",
             "Quote one freight leg. Ports are UN/LOCODEs (CNSHA, NLRTM, USLAX...). mode: sea|air|rail|road.",
             _obj({"origin": S, "destination": S, "mode": {"type": "string", "enum": ["sea", "air", "rail", "road"]},
                   "weight_kg": N, "volume_cbm": N, "carrier_id": S,
                   "via": {"type": "array", "items": S, "description": "Sea chokepoints, e.g. [\"MALACCA\",\"CAPE\"]"}},
                  ["origin", "destination", "mode", "weight_kg", "volume_cbm"]), "mcp", "read", calculate_freight_cost,
             lambda r: _err(r) or f"${r['cost_usd']:,.0f}, {r['transit_days']} days ({r['cache_tier']})"),
    ChatTool("list_shipments",
             "List shipments. filter: at_risk (default) | all | booked | in_transit | rerouted | pending_approval.",
             _obj({"filter": {"type": "string", "enum": ["at_risk", "all", "booked", "in_transit", "rerouted",
                                                         "pending_approval"]},
                   "origin": S, "destination": S, "limit": I}), "local", "read", list_shipments,
             lambda r: f"{r['count']} shipments ({r['filter']})"),
    ChatTool("list_disruptions", "Active disruptions detected from the feeds.", _obj({}), "local", "read",
             list_disruptions, lambda r: f"{r['count']} active disruptions"),
    ChatTool("list_scenarios", "Available disruption scenarios and which are active.", _obj({}), "local", "read",
             list_scenarios, lambda r: f"{sum(s['active'] for s in r['scenarios'])} active"),
    ChatTool("activate_scenario",
             "Switch on a disruption scenario: shanghai_typhoon, red_sea_crisis, la_lb_congestion, rotterdam_strike, "
             "panama_drought.", _obj({"name": S}, ["name"]), "local", "write", activate_scenario,
             lambda r: _err(r) or f"activated {r['activated']}"),
    ChatTool("deactivate_scenario", "Switch off a disruption scenario.", _obj({"name": S}, ["name"]), "local", "write",
             deactivate_scenario, lambda r: _err(r) or f"deactivated {r['deactivated']}"),
    ChatTool("run_rerouting_pipeline",
             "Run the full agent pipeline: detect disruptions, optimise at-risk shipments, apply the HITL gate, "
             "auto-execute cheap reroutes and queue the rest for approval. Optionally activate a scenario first.",
             _obj({"scenario": S}), "local", "write", run_rerouting_pipeline,
             lambda r: _err(r) or f"{r['at_risk']} at risk, {len(r['auto_executed'])} auto, "
                                  f"{len(r['queued_for_approval'])} queued"),
    ChatTool("list_approvals", "HITL approval requests. status: pending (default) | approved | rejected | executed | all.",
             _obj({"status": {"type": "string", "enum": ["pending", "approved", "rejected", "executed", "all"]},
                   "limit": I}), "local", "read", list_approvals, lambda r: f"{r['count']} {r['status']}"),
    ChatTool("get_approval", "Details of one approval request, including why sign-off is needed and alternatives.",
             _obj({"approval_id": APPROVAL}, ["approval_id"]), "local", "read", get_approval,
             lambda r: _err(r) or f"{r['approval_id']} {r['status']}"),
    ChatTool("decide_approval",
             "PROPOSE approving or rejecting a pending approval. This does not execute anything: the user must reply "
             "'confirm'. Never tell the user it is done after calling this.",
             _obj({"approval_id": APPROVAL, "decision": {"type": "string", "enum": ["approve", "reject"]},
                   "comment": S}, ["approval_id", "decision"]), "local", "confirm", decide_approval,
             lambda r: _err(r) or f"awaiting confirmation to {r['decision']} {r['approval_id']}"),
    ChatTool("what_if_optimize",
             "Dry-run the route optimiser for a shipment with optional hypothetical weight, volume or cargo value. "
             "Issues nothing. Shows the recommended route, cache tier and whether it would auto-execute.",
             _obj({"shipment_id": SHIPMENT, "weight_kg": N, "volume_cbm": N, "cargo_value_usd": N}, ["shipment_id"]),
             "local", "write", what_if_optimize,
             lambda r: _err(r) or f"{r['recommended'][:40]}… ({r['cache_tier']})"),
    ChatTool("get_hitl_policy", "The human-in-the-loop approval thresholds.", _obj({}), "local", "read",
             get_hitl_policy, lambda r: r["rule"]),
    ChatTool("get_system_metrics", "Cache hit rates, model usage, LLM cost, approval and PO counts.", _obj({}),
             "local", "read", get_system_metrics, lambda r: f"{r['reroute_pos']} reroute POs"),
    ChatTool("list_purchase_orders", "Reroute purchase orders, or all POs of one shipment.",
             _obj({"shipment_id": SHIPMENT, "limit": I}), "local", "read", list_purchase_orders,
             lambda r: f"{r['count']} POs"),
]}


def call_tool(name: str, args: dict, ctx: ToolContext) -> tuple[dict, ToolCallRecord]:
    tool = TOOLS.get(name)
    t0 = time.perf_counter()
    if tool is None:
        result = {"ok": False, "error": f"Unknown tool {name}"}
        return result, ToolCallRecord(name, "unknown", "read", args, False, result["error"], 0.0)
    try:
        result = tool.handler(args or {}, ctx)
    except Exception as e:  # tools report errors to the agent instead of breaking the turn
        result = {"ok": False, "error": type(e).__name__, "message": str(e)}
    ms = round((time.perf_counter() - t0) * 1000, 1)
    try:
        summary = tool.summarize(result)
    except Exception:
        summary = "done" if result.get("ok", True) else "error"
    return result, ToolCallRecord(name, tool.source, tool.effect, args or {}, bool(result.get("ok", True)), summary, ms)


def catalog() -> list[dict]:
    return [{"name": t.name, "source": t.source, "effect": t.effect, "description": t.description}
            for t in TOOLS.values()]
