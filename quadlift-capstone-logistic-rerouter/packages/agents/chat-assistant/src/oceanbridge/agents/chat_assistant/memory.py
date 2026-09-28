"""Chat memory.

Three layers, all scoped to one session:
  * short-term   the last `history_messages` user/assistant messages, replayed to the LLM verbatim
  * summary      older messages folded into a running summary once the session grows long
  * working      entities the conversation is about (last shipment, approval, scenario) and any action
                 waiting for the user's confirmation; this is what resolves "it", "that shipment", "approve it"
"""

from __future__ import annotations

import re

from typing import Protocol


class ChatMessage(Protocol):
    """Anything with a role and content (the app's stored messages satisfy this)."""
    role: str
    content: str


SHIPMENT_RE = re.compile(r"\bSHP-\d{4}\b", re.I)
APPROVAL_RE = re.compile(r"\bAPR-[0-9A-F]{8}\b", re.I)
PO_RE = re.compile(r"\bPO-SHP-\d{4}-[0-9A-Z]{4,6}\b", re.I)
RECENT = 5


def extract_ids(text: str) -> dict[str, list[str]]:
    return {
        "shipments": [m.upper() for m in SHIPMENT_RE.findall(text or "")],
        "approvals": [m.upper() for m in APPROVAL_RE.findall(text or "")],
        "purchase_orders": [m.upper() for m in PO_RE.findall(text or "")],
    }


def _push(state: dict, key: str, values: list[str]) -> None:
    """Most recent last, no duplicates, at most RECENT items."""
    items = [v for v in state.get(key, []) if v not in values] + list(dict.fromkeys(values))
    state[key] = items[-RECENT:]


def remember(state: dict, text: str) -> dict:
    """Update working memory with ids mentioned in a user message, tool arguments or tool results."""
    ids = extract_ids(text)
    if ids["shipments"]:
        state["last_shipment_id"] = ids["shipments"][-1]
        _push(state, "recent_shipments", ids["shipments"])
    if ids["approvals"]:
        state["last_approval_id"] = ids["approvals"][-1]
        _push(state, "recent_approvals", ids["approvals"])
    if ids["purchase_orders"]:
        state["last_po_id"] = ids["purchase_orders"][-1]
    return state


def remember_tool_call(state: dict, tool: str, args: dict, result: dict) -> None:
    """Tool arguments are the strongest signal of what the conversation is about."""
    if args.get("shipment_id"):
        state["last_shipment_id"] = str(args["shipment_id"]).upper()
        _push(state, "recent_shipments", [state["last_shipment_id"]])
    if args.get("approval_id"):
        state["last_approval_id"] = str(args["approval_id"]).upper()
        _push(state, "recent_approvals", [state["last_approval_id"]])
    if tool in ("activate_scenario", "deactivate_scenario") and args.get("name"):
        state["last_scenario"] = args["name"]
    if tool == "list_approvals" and isinstance(result, dict):
        ids = [a.get("approval_id") for a in result.get("approvals", []) if a.get("approval_id")]
        if len(ids) == 1:
            state["last_approval_id"] = ids[0]


def context_block(state: dict, summary: str) -> str:
    """Working memory + summary, injected into the LLM system prompt."""
    lines = []
    if summary:
        lines.append(f"Summary of earlier conversation: {summary}")
    if state.get("last_shipment_id"):
        lines.append(f"Shipment currently discussed: {state['last_shipment_id']}"
                     + (f" (recent: {', '.join(state.get('recent_shipments', []))})" if state.get("recent_shipments") else ""))
    if state.get("last_approval_id"):
        lines.append(f"Approval currently discussed: {state['last_approval_id']}")
    if state.get("last_scenario"):
        lines.append(f"Last scenario changed: {state['last_scenario']}")
    if state.get("pending_action"):
        pa = state["pending_action"]
        lines.append(f"Waiting for user confirmation: {pa['decision']} {pa['approval_id']}")
    return "\n".join(lines) or "No earlier context in this session."


def summarize_offline(messages: list[ChatMessage], previous: str = "") -> str:
    """Deterministic summary: what the user asked about and which records were involved."""
    asks = [m.content.strip().split("\n")[0][:80] for m in messages if m.role == "user"]
    ids = extract_ids(" ".join(m.content for m in messages))
    parts = [previous] if previous else []
    if asks:
        parts.append("User asked: " + "; ".join(asks[-6:]))
    if ids["shipments"]:
        parts.append("Shipments: " + ", ".join(dict.fromkeys(ids["shipments"])))
    if ids["approvals"]:
        parts.append("Approvals: " + ", ".join(dict.fromkeys(ids["approvals"])))
    return " | ".join(parts)[-1500:]
