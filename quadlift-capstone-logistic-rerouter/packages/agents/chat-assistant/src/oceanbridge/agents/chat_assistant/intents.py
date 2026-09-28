"""Offline planner: maps a message to tool calls with patterns, using working memory for references.

Used when no LLM is configured (and as the fallback if the LLM fails). It supports several requests in
one message ("status and alternatives for SHP-1004") and resolves "it", "that shipment", "approve it"
from the session's working memory."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from oceanbridge.agents.chat_assistant.memory import extract_ids

PORT_ALIASES = {
    "shanghai": "CNSHA", "ningbo": "CNNGB", "shenzhen": "CNSZX", "yantian": "CNSZX", "busan": "KRPUS",
    "tokyo": "JPTYO", "singapore": "SGSIN", "nhava sheva": "INNSA", "mumbai": "INNSA", "jebel ali": "AEJEA",
    "dubai": "AEJEA", "rotterdam": "NLRTM", "antwerp": "BEANR", "hamburg": "DEHAM", "felixstowe": "GBFXT",
    "los angeles": "USLAX", "long beach": "USLGB", "seattle": "USSEA", "new york": "USNYC", "savannah": "USSAV",
    "santos": "BRSSZ",
}
PORT_CODES = set(PORT_ALIASES.values())

SCENARIO_ALIASES = {
    "shanghai_typhoon": ("typhoon", "shanghai"),
    "red_sea_crisis": ("red sea", "suez", "bab-el-mandeb", "houthi"),
    "la_lb_congestion": ("la/lb", "los angeles", "long beach", "congestion"),
    "rotterdam_strike": ("rotterdam", "strike"),
    "panama_drought": ("panama", "drought"),
}

PRONOUN = re.compile(r"\b(it|its|it's|this|that|same|the shipment|this shipment|that shipment|them)\b", re.I)
WEIGHT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(kg|kgs|kilograms?|t|tonnes?|tons?)\b", re.I)
VOLUME = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(cbm|m3|m³|cubic metres?|cubic meters?)", re.I)
VALUE = re.compile(r"(?:value[d]?|worth|valued at)\D{0,12}\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|m|million|thousand)?"
                   r"|\$\s*(\d[\d,]*(?:\.\d+)?)\s*(k|m|million|thousand)?", re.I)
PO_WORDS = re.compile(r"\b(purchase orders?|pos|po)\b", re.I)
SHIPMENT_NUM = re.compile(r"\bshipment\s+(?:no\.?\s*)?(\d{4})\b", re.I)


@dataclass
class Plan:
    steps: list[tuple[str, dict]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    help: bool = False

    def add(self, tool: str, args: dict | None = None) -> None:
        if all(t != tool or a != (args or {}) for t, a in self.steps):
            self.steps.append((tool, args or {}))


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _has(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}", text) for w in words)


def _shipment(text: str, state: dict, ids: dict) -> str | None:
    if ids["shipments"]:
        return ids["shipments"][0]
    m = SHIPMENT_NUM.search(text)
    if m:
        return f"SHP-{m.group(1)}"
    return state.get("last_shipment_id")


def _scenario(text: str) -> str | None:
    for name, aliases in SCENARIO_ALIASES.items():
        if name in text.replace(" ", "_") or any(a in text for a in aliases):
            return name
    return None


def _ports(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for code in PORT_CODES:
        for m in re.finditer(rf"\b{code.lower()}\b", text):
            found.append((m.start(), code))
    for alias, code in PORT_ALIASES.items():
        for m in re.finditer(rf"\b{re.escape(alias)}\b", text):
            found.append((m.start(), code))
    ordered = [c for _, c in sorted(found)]
    return list(dict.fromkeys(ordered))


def _mode(text: str) -> str:
    if _has(text, "air", "fly", "plane"):
        return "air"
    if _has(text, "rail", "train"):
        return "rail"
    if _has(text, "road", "truck"):
        return "road"
    return "sea"


def _weight(text: str) -> float | None:
    m = WEIGHT.search(text)
    if not m:
        return None
    v = _num(m.group(1))
    return v * 1000 if m.group(2).lower().startswith("t") else v


def _volume(text: str) -> float | None:
    m = VOLUME.search(text)
    return _num(m.group(1)) if m else None


def _value(text: str) -> float | None:
    m = VALUE.search(text)
    if not m:
        return None
    raw, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    v = _num(raw)
    unit = (unit or "").lower()
    return v * 1_000_000 if unit in ("m", "million") else v * 1000 if unit in ("k", "thousand") else v


def plan(message: str, state: dict) -> Plan:
    text = message.lower().strip()
    ids = extract_ids(message)
    p = Plan()

    if not text or re.fullmatch(r"(hi|hello|hey|help|\?|what can you do\??|commands)", text):
        p.help = True
        return p

    # --- approvals: decide (needs confirmation), inspect, list
    decide = re.search(r"\b(approve|reject|decline|deny)\b", text)
    approval_id = ids["approvals"][0] if ids["approvals"] else state.get("last_approval_id")
    if decide and not _has(text, "approvals", "pending approvals", "list"):
        if approval_id:
            verb = "approve" if decide.group(1) == "approve" else "reject"
            comment = message.split(":", 1)[1].strip() if ":" in message else None
            p.add("decide_approval", {"approval_id": approval_id, "decision": verb, "comment": comment})
        else:
            p.notes.append("Which approval? Give an id like APR-1A2B3C4D, or ask for the pending approvals first.")
            p.add("list_approvals", {"status": "pending"})
        return p

    # --- pipeline run (optionally with a scenario)
    if (_has(text, "run", "start", "execute", "trigger", "kick off") and
            _has(text, "pipeline", "rerout", "agents", "optimis", "optimiz", "monitoring")):
        scenario = _scenario(text)
        p.add("run_rerouting_pipeline", {"scenario": scenario} if scenario else {})
        return p

    # --- scenarios
    if _has(text, "scenario", "simulate", "typhoon", "strike", "drought", "red sea", "congestion") or \
            re.search(r"\b(activate|deactivate|switch on|switch off|turn on|turn off|enable|disable)\b", text):
        scenario = _scenario(text)
        if re.search(r"\b(deactivate|switch off|turn off|disable|stop|clear|end)\b", text) and scenario:
            p.add("deactivate_scenario", {"name": scenario})
            return p
        if re.search(r"\b(activate|switch on|turn on|enable|simulate|trigger|start|inject)\b", text) and scenario:
            p.add("activate_scenario", {"name": scenario})
            return p
        if _has(text, "scenario"):
            p.add("list_scenarios")
            return p

    shipment = _shipment(message, state, ids)
    explicit_shipment = bool(ids["shipments"] or SHIPMENT_NUM.search(message) or PRONOUN.search(text))

    # --- what-if
    if re.search(r"\bwhat[\s-]?if\b|\bsuppose\b|\bhypothetical\b|\bif (the|its) (weight|value|cargo)", text):
        if shipment:
            args = {"shipment_id": shipment}
            for k, v in (("weight_kg", _weight(text)), ("volume_cbm", _volume(text)), ("cargo_value_usd", _value(text))):
                if v is not None:
                    args[k] = v
            p.add("what_if_optimize", args)
        else:
            p.notes.append("Which shipment should I run the what-if for?")
        return p

    # --- freight quote
    if _has(text, "quote", "freight cost", "price", "how much", "rate", "cost to ship", "cost from"):
        ports = _ports(text)
        if len(ports) >= 2:
            weight = _weight(text) or 10_000.0
            volume = _volume(text) or 30.0
            if not _weight(text) or not _volume(text):
                p.notes.append(f"Assumed {weight:,.0f} kg / {volume:,.0f} cbm where not given.")
            p.add("calculate_freight_cost", {"origin": ports[0], "destination": ports[1], "mode": _mode(text),
                                             "weight_kg": weight, "volume_cbm": volume})
            return p
        if not shipment:
            p.notes.append("For a quote, name two ports, e.g. 'quote sea freight Shanghai to Rotterdam 18000 kg 60 cbm'.")
            return p

    # --- shipment-specific requests (several may apply)
    wants_alts = _has(text, "alternative", "option", "reroute", "re-route", "other route", "routes")
    wants_status = _has(text, "status", "where", "eta", "track", "risk", "delay", "details", "tell me about",
                        "how is", "update")
    wants_pos = bool(PO_WORDS.search(text))
    if shipment and (explicit_shipment or wants_alts or wants_status or wants_pos):
        if wants_status or not (wants_alts or wants_pos):
            p.add("get_shipment_status", {"shipment_id": shipment})
        if wants_alts:
            p.add("list_route_alternatives", {"shipment_id": shipment})
        if wants_pos:
            p.add("list_purchase_orders", {"shipment_id": shipment})
        return p

    # --- collections
    if ids["approvals"] and not decide:
        p.add("get_approval", {"approval_id": ids["approvals"][0]})
    if _has(text, "approval", "sign-off", "sign off", "waiting for me", "queue"):
        status = next((s for s in ("approved", "rejected", "executed") if _has(text, s)), "all" if _has(text, "all") else "pending")
        p.add("list_approvals", {"status": status})
    if PO_WORDS.search(text):
        p.add("list_purchase_orders")
    if _has(text, "disruption", "incident", "alert", "what's happening", "what is happening", "situation"):
        p.add("list_disruptions")
    if _has(text, "at risk", "at-risk", "delayed", "affected", "impacted", "risky"):
        p.add("list_shipments", {"filter": "at_risk"})
    elif _has(text, "shipments", "all shipments", "list shipments"):
        flt = next((f for f in ("pending_approval", "rerouted", "in_transit", "booked")
                    if _has(text, f.replace("_", " "))), "all")
        p.add("list_shipments", {"filter": flt})
    if _has(text, "policy", "threshold", "hitl", "limit", "rule", "human in the loop"):
        p.add("get_hitl_policy")
    if _has(text, "metric", "cache", "token", "llm cost", "model usage", "hit rate", "spend"):
        p.add("get_system_metrics")

    if not p.steps and not p.notes:
        p.help = True
    return p
