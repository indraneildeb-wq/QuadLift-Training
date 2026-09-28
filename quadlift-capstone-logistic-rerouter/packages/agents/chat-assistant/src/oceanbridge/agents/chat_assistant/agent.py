"""Chat Assistant agent: agentic tool use over the Supply Chain Core MCP server and local tools.

run_llm      OpenAI function calling in a loop: the model decides which tools to call, sees the results,
             may call more tools, then answers. Up to `chat.max_tool_steps` rounds per turn.
run_offline  the deterministic planner (intents.py) picks the tools; replies are composed from the results.

Both record every tool call (name, source mcp/local, arguments, outcome, duration) for the UI and the audit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from oceanbridge.agents.chat_assistant import intents
from oceanbridge.agents.chat_assistant.tools import TOOLS, ToolCallRecord, ToolContext, call_tool
from oceanbridge.agents.common.base import offline_label
from oceanbridge.agents.chat_assistant import memory
from oceanbridge.agents.chat_assistant.memory import ChatMessage
from oceanbridge.config import get_settings
from oceanbridge.llm.router import RoutingDecision

CONFIG = yaml.safe_load((Path(__file__).parent / "agent.yaml").read_text(encoding="utf-8"))
ANALYTICAL = re.compile(r"\b(why|compare|comparison|recommend|should|best|explain|trade-?offs?|analy[sz]e|"
                        r"which (one|option|route) is better|pros and cons|plan)\b", re.I)
MAX_TOOL_RESULT_CHARS = 6000


@dataclass
class TurnOutput:
    reply: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


def is_analytical(message: str) -> bool:
    return bool(ANALYTICAL.search(message))


def system_prompt(user: str, state: dict, summary: str) -> str:
    return CONFIG["system_prompt"].format(user=user, today=date.today().isoformat(),
                                          hitl_rule=_hitl_rule(), memory=memory.context_block(state, summary))


def _hitl_rule() -> str:
    h = get_settings().hitl
    return (f"auto-execute only if cost increase < {h.max_cost_increase_pct}% AND cargo value < "
            f"${h.max_cargo_value_usd:,.0f}; otherwise {h.approver_role} sign-off")


# ============================================================================ LLM: OpenAI function calling
def run_llm(message: str, history: list[ChatMessage], ctx: ToolContext, summary: str, decision: RoutingDecision,
            client=None) -> TurnOutput:
    if client is None:
        from openai import OpenAI

        client = OpenAI()
    max_steps = get_settings().chat.max_tool_steps
    msgs: list[dict] = [{"role": "system", "content": system_prompt(ctx.user, ctx.state, summary)}]
    msgs += [{"role": m.role, "content": m.content} for m in history if m.role in ("user", "assistant")]
    msgs.append({"role": "user", "content": message})
    tools = [t.openai_schema() for t in TOOLS.values()]
    out = TurnOutput(reply="", model=decision.model)

    for step in range(max_steps + 1):
        last_round = step == max_steps
        resp = client.chat.completions.create(model=decision.model, messages=msgs, tools=tools,
                                              tool_choice="none" if last_round else "auto", temperature=0.2)
        if getattr(resp, "usage", None):
            out.prompt_tokens += resp.usage.prompt_tokens or 0
            out.completion_tokens += resp.usage.completion_tokens or 0
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []
        if not calls or last_round:
            out.reply = (msg.content or "").strip() or "I could not produce an answer. Please rephrase."
            return out
        msgs.append({"role": "assistant", "content": msg.content or "",
                     "tool_calls": [{"id": c.id, "type": "function",
                                     "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                    for c in calls]})
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result, record = call_tool(c.function.name, args, ctx)
            memory.remember_tool_call(ctx.state, c.function.name, args, result)
            out.tool_calls.append(record)
            msgs.append({"role": "tool", "tool_call_id": c.id,
                         "content": json.dumps(result, default=str)[:MAX_TOOL_RESULT_CHARS]})
    return out


def summarize_llm(previous: str, messages: list[ChatMessage], model: str, client=None) -> str:
    if client is None:
        from openai import OpenAI

        client = OpenAI()
    transcript = "\n".join(f"{m.role}: {m.content[:500]}" for m in messages)
    resp = client.chat.completions.create(model=model, temperature=0, messages=[
        {"role": "system", "content": "Summarise this logistics support chat in at most 120 words. Keep shipment, "
                                      "approval and PO ids, decisions taken and open questions."},
        {"role": "user", "content": f"Earlier summary: {previous or 'none'}\n\nNew messages:\n{transcript}"}])
    return (resp.choices[0].message.content or previous).strip()


# ============================================================================ offline: planner + templates
HELP = """I can look things up and act for you. Try:
- **"status of SHP-1004"**, then **"show its alternatives"**
- **"which shipments are at risk?"** · **"what disruptions are active?"**
- **"quote sea freight Shanghai to Rotterdam 18000 kg 60 cbm"**
- **"activate the rotterdam strike"** · **"run the rerouting pipeline"**
- **"pending approvals"** · **"approve APR-XXXXXXXX"** (I will ask you to confirm)
- **"what if SHP-1004 weighed 20 t?"** · **"what is the HITL policy?"** · **"cache metrics"**"""


def _money(v) -> str:
    return f"${v:,.0f}"


def _fmt(tool: str, r: dict) -> str:
    if not r.get("ok", True):
        extra = f" Valid names: {', '.join(r['valid_names'])}." if r.get("valid_names") else ""
        return f"⚠️ `{tool}` failed: {r.get('message') or r.get('error')}.{extra}"
    if tool == "get_shipment_status":
        s, k = r["shipment"], r["risk"]
        risk = (f"**at risk** (score {k['risk_score']}, +{k['expected_delay_days']} d, projected ETA {k['projected_eta']})"
                if k["at_risk"] else f"on track (risk {k['risk_score']})")
        lines = [f"**{s['id']}** · {s['commodity']} for {s['customer']} · {' → '.join(r['route'])}",
                 f"Status {s['status']}, ETA {s['eta']} (required {s['required_delivery_date']}), {risk}.",
                 f"Cargo {_money(s['cargo_value_usd'])}, booked freight {_money(s['current_cost_usd'])}, PO {s['po_id']}."]
        if k["drivers"]:
            lines.append("Drivers: " + "; ".join(d for d in k["drivers"] if not d.startswith("analyst:")))
        if r.get("pending_approval_id"):
            lines.append(f"Pending approval: {r['pending_approval_id']}.")
        return "\n".join(lines)
    if tool == "list_route_alternatives":
        rows = ["| Option | Route | Cost | Δ cost | ETA | Risk |", "|---|---|---|---|---|---|"]
        rows += [f"| {o['option_id']} | {o['label']} | {_money(o['total_cost_usd'])} | {o['cost_increase_pct']:+.1f}% | "
                 f"{o['projected_eta']} | {o['residual_risk']:.2f} |" for o in r["options"]]
        return "Reroute options, best landed cost first:\n\n" + "\n".join(rows)
    if tool == "calculate_freight_cost":
        return (f"{r['mode'].title()} {r['origin']} → {r['destination']} with {r['carrier_id']}"
                + (f" via {'/'.join(r['via'])}" if r.get("via") else "")
                + f": **{_money(r['cost_usd'])}**, {r['transit_days']} days, {r['distance_km']:,.0f} km, "
                  f"{r['chargeable_units']} {r['unit']}, CO₂ {r['co2_kg']:,.0f} kg. (cache: {r['cache_tier']})")
    if tool == "list_shipments":
        if not r["shipments"]:
            return f"No shipments match ({r['filter']})."
        rows = ["| Shipment | Route | Mode | Status | Value | Risk | Delay |", "|---|---|---|---|---|---|---|"]
        rows += [f"| {s['shipment_id']} | {s['route']} | {s['mode']} | {s['status']} | {_money(s['cargo_value_usd'])} | "
                 f"{s['risk_score']} | {s['expected_delay_days']} d |" for s in r["shipments"]]
        more = f"\n\nShowing {len(r['shipments'])} of {r['count']}." if r["count"] > len(r["shipments"]) else ""
        return f"{r['count']} shipments ({r['filter'].replace('_', ' ')}):\n\n" + "\n".join(rows) + more
    if tool == "list_disruptions":
        if not r["disruptions"]:
            return "No active disruptions. Activate a scenario and run the pipeline to see some."
        return "Active disruptions:\n" + "\n".join(
            f"- **{d['type']}** at {d['location']} (severity {d['severity']}, +{d['expected_delay_days']} d): {d['description']}"
            for d in r["disruptions"])
    if tool == "list_scenarios":
        return "Scenarios:\n" + "\n".join(f"- `{s['name']}`: {s['title']}{' (**active**)' if s['active'] else ''}"
                                          for s in r["scenarios"])
    if tool == "activate_scenario":
        return f"Activated **{r['title']}** (`{r['activated']}`). {r['note']}"
    if tool == "deactivate_scenario":
        return f"Deactivated `{r['deactivated']}`."
    if tool == "run_rerouting_pipeline":
        lines = [f"Pipeline run **{r['run_id']}** finished. {r['situation_report']}",
                 f"{r['at_risk']} at risk · {len(r['auto_executed'])} auto-executed · "
                 f"{len(r['queued_for_approval'])} queued for sign-off · {r['kept_current_routing']} kept their routing."]
        lines += [f"- ✅ {x['shipment_id']}: PO {x['po_id']} {_money(x['amount_usd'])} ({x['route']})"
                  for x in r["auto_executed"][:5]]
        lines += [f"- 🛑 {q['shipment_id']}: {q['approval_id']} ({q['cost_increase_pct']:+.1f}%)"
                  for q in r["queued_for_approval"][:5]]
        if r["errors"]:
            lines.append("Errors: " + "; ".join(r["errors"]))
        return "\n".join(lines)
    if tool == "list_approvals":
        if not r["approvals"]:
            return f"No {r['status']} approvals."
        rows = ["| Approval | Shipment | Route | Δ cost | Cargo | Status |", "|---|---|---|---|---|---|"]
        rows += [f"| {a['approval_id']} | {a['shipment_id']} | {a['route']} | {a['cost_increase_pct']:+.1f}% | "
                 f"{_money(a['cargo_value_usd'])} | {a['status']} |" for a in r["approvals"]]
        return f"{r['count']} {r['status']} approvals:\n\n" + "\n".join(rows)
    if tool == "get_approval":
        return (f"**{r['approval_id']}** for {r['shipment_id']} ({r['status']}): {r['route']}, "
                f"{_money(r['new_cost_usd'])} ({r['cost_increase_pct']:+.1f}%), ETA {r['projected_eta']}.\n"
                f"Why sign-off: {'; '.join(r['reasons'])}.\n{r['rationale']}")
    if tool == "decide_approval":
        return (f"Ready to **{r['decision']}** {r['approval_id']} for {r['shipment_id']} ({r['route']}, "
                f"{r['cost_increase_pct']:+.1f}%). {r['message']}")
    if tool == "what_if_optimize":
        over = ", ".join(f"{k} {v:,.0f}" for k, v in r["overrides"].items()) or "no changes"
        if r.get("selected_option_id") == "STAY":
            return (f"What-if for {r['shipment_id']} ({over}): **keep the current routing**, no reroute needed "
                    f"(ETA {r['projected_eta']}). Answered from cache tier `{r['cache_tier']}`.")
        verdict = "✅ would auto-execute" if r["auto_execute"] else "🛑 would need sign-off"
        return (f"What-if for {r['shipment_id']} ({over}): recommended **{r['recommended']}**, "
                f"{_money(r['total_cost_usd'])} ({r['cost_increase_pct']:+.1f}%), ETA {r['projected_eta']}; {verdict}. "
                f"Answered from cache tier `{r['cache_tier']}`" + (f" (similarity {r['similarity']})." if r["similarity"] else "."))
    if tool == "get_hitl_policy":
        return r["rule"]
    if tool == "get_system_metrics":
        cache = "; ".join(f"{ns}: hit rate {c['hit_rate'] * 100:.0f}% of {c['lookups']} lookups" for ns, c in r["cache"].items())
        models = ", ".join(f"{m} ×{n}" for m, n in r["model_calls"].items()) or "none yet"
        return (f"Cache: {cache or 'no lookups yet'}.\nModel calls: {models}. LLM cost ${r['llm_cost_usd']}.\n"
                f"Approvals: {r['approvals'] or 'none'} · reroute POs: {r['reroute_pos']}.")
    if tool == "list_purchase_orders":
        if not r["purchase_orders"]:
            return "No purchase orders found."
        return "Purchase orders:\n" + "\n".join(
            f"- {p['po_id']} ({p['shipment_id']}, {p['carrier_id']}): {_money(p['amount_usd'])}, {p['status']}"
            f"{', auto' if p['auto_executed'] else ''}{', approval ' + p['approval_id'] if p['approval_id'] else ''}"
            for p in r["purchase_orders"])
    return json.dumps(r, default=str)[:1500]


def run_offline(message: str, ctx: ToolContext, decision: RoutingDecision) -> TurnOutput:
    p = intents.plan(message, ctx.state)
    out = TurnOutput(reply="", model=offline_label(decision))
    if p.help and not p.steps:
        out.reply = HELP
        return out
    parts = list(p.notes)
    for tool, args in p.steps:
        result, record = call_tool(tool, args, ctx)
        memory.remember_tool_call(ctx.state, tool, args, result)
        out.tool_calls.append(record)
        parts.append(_fmt(tool, result))
    out.reply = "\n\n".join(parts) or HELP
    return out
