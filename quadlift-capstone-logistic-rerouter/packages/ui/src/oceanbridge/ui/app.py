"""OceanBridge Logistics control tower (Streamlit). Talks to the FastAPI backend.

Run:  python -m oceanbridge.ui   (or: streamlit run packages/ui/src/oceanbridge/ui/app.py)
"""

from __future__ import annotations

import json
import os
import time

import httpx
import pandas as pd
import pydeck as pdk
import streamlit as st

SERIES = "#2a78d6"
# Reserved status palette: always shown together with a text label, never colour alone.
STATUS_RGB = {"On track": [12, 163, 12], "Watch": [250, 178, 25], "At risk": [208, 59, 59]}
PORT_COORDS: dict[str, tuple[float, float]] = {}
CANDIDATE_PORTS = (8000, 8001, 8002)

st.set_page_config(page_title="OceanBridge Control Tower", page_icon="🚢", layout="wide")


def _answers(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=1.5).json().get("status") == "ok"
    except Exception:
        return False


def find_api() -> str | None:
    """OB_API_URL if set; otherwise the first local port where the OceanBridge API answers."""
    if os.environ.get("OB_API_URL"):
        return os.environ["OB_API_URL"].rstrip("/")
    cached = st.session_state.get("api_url")
    if cached and _answers(cached):
        return cached
    for port in CANDIDATE_PORTS:
        url = f"http://127.0.0.1:{port}"
        if _answers(url):
            st.session_state["api_url"] = url
            return url
    return None


API = find_api()
if API is None:
    st.error("The OceanBridge API is not running, so the dashboard can't load. "
             f"Nothing answered on ports {', '.join(map(str, CANDIDATE_PORTS))}.")
    st.markdown("Start it in a separate PowerShell window, then refresh this page:")
    st.code("cd C:\\Krishnendu\\Workspace\\quadlift-capstone-logistic-rerouter\n"
            ".\\.venv\\Scripts\\python.exe -m uvicorn oceanbridge.api.main:app --port 8001", language="powershell")
    st.stop()


# ------------------------------------------------------------------ API helpers
def api(method: str, path: str, **kw):
    try:
        r = httpx.request(method, f"{API}{path}", timeout=300, **kw)
    except httpx.HTTPError as e:
        st.session_state.pop("api_url", None)
        st.error(f"Lost the connection to the API at {API}: {e}. Check the API window, then refresh this page.")
        st.stop()
    if r.status_code >= 400:
        st.error(f"{method} {path} -> {r.status_code}: {r.text}")
        return None
    return r.json()


def risk_label(score: float, at_risk: bool) -> str:
    return "At risk" if at_risk else ("Watch" if score > 0 else "On track")


def load_coords():
    if not PORT_COORDS:
        from oceanbridge.core.geo import ALL_LOCATIONS  # local import: coordinates are static reference data

        PORT_COORDS.update({c: (loc.lat, loc.lon) for c, loc in ALL_LOCATIONS.items()})


# ------------------------------------------------------------------ sidebar
health = api("GET", "/health")
cfg = api("GET", "/config")
with st.sidebar:
    st.title("🚢 OceanBridge")
    st.caption(f"Agent backend: **{health['backend']}** · API `{API}`")
    st.caption(f"HITL: auto if cost +<{cfg['hitl']['max_cost_increase_pct']}% "
               f"AND value <${cfg['hitl']['max_cargo_value_usd']:,.0f}")
    st.divider()
    st.subheader("Disruption scenarios")
    for sc in api("GET", "/scenarios"):
        on = st.toggle(sc["title"], value=sc["active"], help=sc["description"], key=f"sc_{sc['name']}")
        if on != sc["active"]:
            api("POST", f"/scenarios/{sc['name']}/{'activate' if on else 'deactivate'}")
            st.rerun()
    st.divider()
    if st.button("▶ Run rerouting pipeline", type="primary", width="stretch"):
        with st.spinner("Monitoring → optimising → HITL gate → negotiating…"):
            res = api("POST", "/pipeline/run-sync", json={})
        if res:
            st.session_state["last_run"] = res
            st.success(f"{res['run_id']}: {res['at_risk']} at risk, {len(res['auto_executed'])} auto-executed, "
                       f"{len(res['queued_for_approval'])} awaiting sign-off")
    if st.button("↺ Reset demo data", width="stretch"):
        api("POST", "/admin/seed")
        st.session_state.pop("last_run", None)
        st.rerun()

tower, assistant_tab, proposals, approvals_tab, pos_tab, metrics_tab = st.tabs(
    ["Control Tower", "💬 Assistant", "Reroute Proposals", "Approval Queue", "Purchase Orders & Audit", "Metrics"])

# ------------------------------------------------------------------ chat assistant
SUGGESTIONS = ["Which shipments are at risk?", "Activate the Rotterdam strike and run the pipeline",
               "Show pending approvals", "Status of SHP-1004", "Quote sea freight Shanghai to Rotterdam 18000 kg 60 cbm",
               "What is the HITL policy?"]


def chat_send(session_id: str, text: str):
    return api("POST", f"/chat/sessions/{session_id}/messages", json={"message": text})


def chat_new(invalidate: str | None = None):
    s = api("POST", "/chat/sessions", json={"user": st.session_state.get("approver", "Logistics Ops Manager"),
                                            "invalidate_session_id": invalidate})
    st.session_state["chat_session"] = s["id"]


def current_chat_session() -> dict:
    if "chat_session" not in st.session_state:
        chat_new()
    sess = api("GET", f"/chat/sessions/{st.session_state['chat_session']}")
    if sess is None:  # deleted or unknown: start over
        chat_new()
        sess = api("GET", f"/chat/sessions/{st.session_state['chat_session']}")
    return sess


def render_messages(sess: dict, compact: bool) -> None:
    for m in sess["messages"]:
        with st.chat_message(m["role"], avatar="🧑‍✈️" if m["role"] == "user" else "🚢"):
            st.markdown(m["content"])
            if not m["tool_calls"]:
                continue
            if compact:
                st.caption("🔧 " + " · ".join(f"{t['tool']} [{t['source']}] {'✓' if t['ok'] else '✗'}"
                                             for t in m["tool_calls"]))
                continue
            with st.expander(f"🔧 {len(m['tool_calls'])} tool call(s) · {m['model']}"):
                st.dataframe(pd.DataFrame([{
                    "tool": t["tool"], "source": t["source"], "effect": t["effect"], "ok": "✓" if t["ok"] else "✗",
                    "result": t["summary"], "ms": t["duration_ms"], "arguments": json.dumps(t["arguments"])}
                    for t in m["tool_calls"]]), hide_index=True, width="stretch")


def render_chat(prefix: str, compact: bool) -> None:
    """The chat UI. compact=True is the pop-up window opened from the floating 💬 button."""
    rerun = st.rerun  # the pop-up re-opens itself on rerun while st.session_state["chat_open"] is set
    sess = current_chat_session()
    sid = sess["id"]
    badge = {"active": "🟢 active", "ended": "⚪ ended", "expired": "🟠 expired"}[sess["status"]]

    head_l, head_r = st.columns([3, 2])
    with head_l:
        st.markdown(f"**{sess['title']}** · {badge}")
        st.caption(f"`{sid}` · {sess['user']} · {sess['message_count']} messages"
                   + (f" · memory: {sess['state'].get('last_shipment_id')}" if sess["state"].get("last_shipment_id") else ""))
    with head_r:
        b1, b2 = st.columns(2)
        if b1.button("➕ New chat", key=f"{prefix}_new", width="stretch",
                     help="Ends this session and starts a fresh one with empty memory"):
            chat_new(invalidate=sid if sess["status"] == "active" else None)
            rerun()
        if b2.button("⏹ End", key=f"{prefix}_end", width="stretch", disabled=sess["status"] != "active",
                     help="Invalidate this session: it becomes read-only and its memory is wiped"):
            api("POST", f"/chat/sessions/{sid}/invalidate")
            rerun()

    if not compact:
        with st.expander("Previous sessions"):
            for p in api("GET", "/chat/sessions", params={"limit": 20}) or []:
                c1, c2 = st.columns([4, 1])
                c1.markdown(f"`{p['id']}` · {p['title']} · {p['status']} · {p['message_count']} msgs")
                if p["id"] != sid and c2.button("Open", key=f"{prefix}_open_{p['id']}"):
                    st.session_state["chat_session"] = p["id"]
                    rerun()

    if compact:
        with st.container(height=430, autoscroll=True):
            render_messages(sess, compact)
    else:
        render_messages(sess, compact)

    if sess["status"] != "active":
        st.warning(f"This session is {sess['status']}"
                   + (f" ({sess['end_reason']})" if sess.get("end_reason") else "")
                   + ". It is read-only. Click **New chat** to continue.")
        return

    pending = sess["state"].get("pending_action")
    if pending:
        st.info(f"Waiting for your confirmation: **{pending['decision']} {pending['approval_id']}** "
                f"({pending['shipment_id']}, {pending['route']}).")
        c1, c2, _ = st.columns([1, 1, 3])
        if c1.button("✅ Confirm", key=f"{prefix}_confirm", type="primary"):
            with st.spinner("Negotiating with the carrier…"):
                chat_send(sid, "confirm")
            rerun()
        if c2.button("Cancel", key=f"{prefix}_cancel"):
            chat_send(sid, "cancel")
            rerun()

    if not sess["messages"]:
        st.caption("Try one of these:")
        cols = st.columns(2 if compact else 3)
        for i, q in enumerate(SUGGESTIONS):
            if cols[i % len(cols)].button(q, key=f"{prefix}_sugg_{i}", width="stretch"):
                with st.spinner("Calling tools…"):
                    chat_send(sid, q)
                rerun()

    if compact:
        with st.form(f"{prefix}_form", clear_on_submit=True, border=False):
            c1, c2 = st.columns([5, 1])
            text = c1.text_input("Message", key=f"{prefix}_text", label_visibility="collapsed",
                                 placeholder="Ask about shipments, disruptions, quotes or approvals…")
            sent = c2.form_submit_button("Send", type="primary", width="stretch")
        if sent and text.strip():
            with st.spinner("Calling tools…"):
                chat_send(sid, text.strip())
            rerun()
    else:
        prompt = st.chat_input("Ask about shipments, disruptions, quotes or approvals…", key=f"{prefix}_input")
        if prompt:
            with st.spinner("Calling tools…"):
                chat_send(sid, prompt)
            rerun()


def _close_chat() -> None:
    st.session_state["chat_open"] = False


@st.dialog("💬 OceanBridge Assistant", width="large", on_dismiss=_close_chat)
def chat_dialog():
    render_chat("dlg", compact=True)


with assistant_tab:
    render_chat("tab", compact=False)

# ------------------------------------------------------------------ control tower
with tower:
    load_coords()
    ships = pd.DataFrame(api("GET", "/shipments"))
    disr = api("GET", "/disruptions")
    pending = api("GET", "/approvals", params={"status": "pending"})
    ships["risk"] = [risk_label(s, a) for s, a in zip(ships["risk_score"], ships["at_risk"])]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Shipments monitored", len(ships))
    c2.metric("At risk", int(ships["at_risk"].sum()))
    c3.metric("Active disruptions", len(disr))
    c4.metric("Awaiting sign-off", len(pending))

    arcs = ships.assign(
        src_lat=ships["origin"].map(lambda c: PORT_COORDS[c][0]), src_lon=ships["origin"].map(lambda c: PORT_COORDS[c][1]),
        dst_lat=ships["destination"].map(lambda c: PORT_COORDS[c][0]),
        dst_lon=ships["destination"].map(lambda c: PORT_COORDS[c][1]),
        color=ships["risk"].map(STATUS_RGB),
    )
    layers = [pdk.Layer("ArcLayer", arcs, get_source_position=["src_lon", "src_lat"],
                        get_target_position=["dst_lon", "dst_lat"], get_source_color="color",
                        get_target_color="color", get_width=2, pickable=True, auto_highlight=True)]
    if disr:
        ddf = pd.DataFrame(disr)
        ddf["radius"] = 120_000 + ddf["severity"] * 380_000
        layers.append(pdk.Layer("ScatterplotLayer", ddf, get_position=["lon", "lat"], get_radius="radius",
                                get_fill_color=[208, 59, 59, 90], get_line_color=[208, 59, 59],
                                line_width_min_pixels=2, stroked=True, pickable=True))
    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=25, longitude=40, zoom=0.9),
                             map_style=None, tooltip={"text": "{id} {origin}->{destination}\n{risk}\n{description}"}))
    st.caption("Arcs: shipments (green = on track, amber = watch, red = at risk). Circles: active disruptions, "
               "sized by severity.")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Shipments")
        view = ships[["id", "customer", "commodity", "origin", "destination", "mode", "status", "cargo_value_usd",
                      "current_cost_usd", "eta", "risk", "risk_score", "expected_delay_days"]]
        only_risk = st.checkbox("Show at-risk only", value=bool(ships["at_risk"].any()))
        st.dataframe(view[view["risk"] == "At risk"] if only_risk else view, hide_index=True, width="stretch",
                     column_config={"cargo_value_usd": st.column_config.NumberColumn("Cargo value", format="$%d"),
                                    "current_cost_usd": st.column_config.NumberColumn("Freight", format="$%d")})
    with right:
        st.subheader("Active disruptions")
        if disr:
            st.dataframe(pd.DataFrame(disr)[["type", "location_name", "severity", "expected_delay_days", "description"]],
                         hide_index=True, width="stretch")
        else:
            st.info("No active disruptions. Toggle a scenario in the sidebar.")

    runs = api("GET", "/runs", params={"limit": 5})
    if runs:
        latest = api("GET", f"/runs/{runs[0]['id']}")
        st.subheader(f"Latest run {latest['id']} · {latest['status']} · {latest['backend']}")
        if latest["summary"].get("situation_report"):
            st.info(latest["summary"]["situation_report"])
        if latest["trace"]:
            st.dataframe(pd.DataFrame(latest["trace"]), hide_index=True, width="stretch")
        if latest["summary"].get("errors"):
            st.warning("\n".join(latest["summary"]["errors"]))

# ------------------------------------------------------------------ proposals / what-if
with proposals:
    ships_df = pd.DataFrame(api("GET", "/shipments"))
    ordered = ships_df.sort_values(["at_risk", "risk_score"], ascending=False)["id"].tolist()
    sid = st.selectbox("Shipment", ordered, format_func=lambda s: f"{s} · " + " → ".join(
        ships_df.loc[ships_df["id"] == s, ["origin", "destination"]].iloc[0]))
    s = ships_df[ships_df["id"] == sid].iloc[0]
    st.caption(f"{s['commodity']} · {s['weight_kg']:,.0f} kg · value ${s['cargo_value_usd']:,.0f} · booked "
               f"${s['current_cost_usd']:,.0f} · ETA {s['eta']} · required {s['required_delivery_date']}")

    with st.form("whatif"):
        st.markdown("**What-if optimisation** (dry run: uses the semantic cache and model routing, issues nothing)")
        w1, w2, w3 = st.columns(3)
        weight = w1.number_input("Weight kg", value=float(s["weight_kg"]), step=500.0)
        value = w2.number_input("Cargo value USD", value=float(s["cargo_value_usd"]), step=10_000.0)
        volume = w3.number_input("Volume cbm", value=float(s["volume_cbm"]), step=5.0)
        go = st.form_submit_button("Optimise")
    if go:
        t0 = time.perf_counter()
        res = api("POST", "/optimize/what-if", json={"shipment_id": sid, "weight_kg": weight,
                                                      "cargo_value_usd": value, "volume_cbm": volume})
        ms = (time.perf_counter() - t0) * 1000
        if res:
            hitl = res["hitl"]
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Recommended", res["selected_option_id"])
            k2.metric("Cache", res["cache_tier"], help=f"similarity {res['similarity']}" if res["similarity"] else None)
            k3.metric("Model", res["model"].split(" ")[0])
            k4.metric("HITL", "✅ Auto-execute" if hitl["auto_execute"] else "🛑 Sign-off")
            st.caption(f"Round trip {ms:,.0f} ms")
            st.write(res["rationale"])
            st.caption(" · ".join(hitl["reasons"]))
            opts = pd.DataFrame(res["options"])
            opts["recommended"] = opts["option_id"].eq(res["selected_option_id"]).map({True: "★", False: ""})
            st.dataframe(opts[["recommended", "option_id", "label", "total_cost_usd", "cost_increase_pct",
                               "transit_days", "projected_eta", "lead_time_delta_days", "residual_risk", "co2_kg",
                               "capacity_ok", "landed_cost_score"]], hide_index=True, width="stretch",
                         column_config={"total_cost_usd": st.column_config.NumberColumn("Freight", format="$%.0f"),
                                        "cost_increase_pct": st.column_config.NumberColumn("Δ cost %", format="%.1f%%"),
                                        "landed_cost_score": st.column_config.NumberColumn("Landed score", format="$%.0f")})

# ------------------------------------------------------------------ approval queue
with approvals_tab:
    st.subheader(f"Pending sign-off · {cfg['hitl']['approver_role']}")
    approver = st.text_input("Approver name", value=st.session_state.get("approver", "Logistics Ops Manager"))
    st.session_state["approver"] = approver
    pending = api("GET", "/approvals", params={"status": "pending"})
    if not pending:
        st.success("Nothing awaiting approval.")
    for a in pending:
        opt = a["option"]
        with st.expander(f"{a['id']} · {a['shipment_id']} · {opt['label']} · {opt['cost_increase_pct']:+.1f}% · "
                         f"value ${a['decision']['cargo_value_usd']:,.0f}", expanded=len(pending) <= 3):
            st.markdown("**Why sign-off is required:** " + "; ".join(a["decision"]["reasons"]))
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("New freight", f"${opt['total_cost_usd']:,.0f}", f"{opt['cost_delta_usd']:+,.0f}", delta_color="inverse")
            m2.metric("ETA", opt["projected_eta"], f"{opt['lead_time_delta_days']:+.0f} d vs plan", delta_color="inverse")
            m3.metric("Residual risk", f"{opt['residual_risk']:.2f}")
            m4.metric("CO₂", f"{opt['co2_kg']:,.0f} kg")
            st.write(a["rationale"])
            if a["alternatives"]:
                st.dataframe(pd.DataFrame(a["alternatives"])[["option_id", "label", "total_cost_usd",
                                                              "cost_increase_pct", "projected_eta", "residual_risk"]],
                             hide_index=True, width="stretch")
            comment = st.text_input("Comment", key=f"c_{a['id']}")
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button("Approve & execute", key=f"ok_{a['id']}", type="primary"):
                with st.spinner("Negotiating with carrier and issuing PO…"):
                    r = api("POST", f"/approvals/{a['id']}/approve", json={"approver": approver, "comment": comment})
                if r:
                    st.success(f"PO {r['po_id']} issued at ${r['amount_usd']:,.2f} ({r['rounds']} rounds)")
                    st.info(r["carrier_message"])
            if b2.button("Reject", key=f"no_{a['id']}"):
                if api("POST", f"/approvals/{a['id']}/reject", json={"approver": approver, "comment": comment}):
                    st.rerun()
    decided = [a for a in api("GET", "/approvals") if a["status"] != "pending"]
    if decided:
        st.subheader("Decided")
        st.dataframe(pd.DataFrame([{"id": a["id"], "shipment": a["shipment_id"], "status": a["status"],
                                    "approver": a["approver"], "comment": a["comment"], "decided_at": a["decided_at"],
                                    "option": a["option"]["label"]} for a in decided]),
                     hide_index=True, width="stretch")

# ------------------------------------------------------------------ POs & audit
with pos_tab:
    pos = [p for p in api("GET", "/purchase-orders") if not p["po_id"].endswith("-ORIG")]
    st.subheader(f"Adjusted purchase orders ({len(pos)})")
    if pos:
        st.dataframe(pd.DataFrame([{
            "po_id": p["po_id"], "shipment": p["shipment_id"], "carrier": p["carrier_id"], "amount_usd": p["amount_usd"],
            "route": p["route_label"], "auto": "auto" if p["auto_executed"] else "approved",
            "approval_id": p["approval_id"], "supersedes": p["supersedes_po_id"],
            "transit_guarantee_d": p["sla"]["guaranteed_transit_days"],
            "penalty_%/day": p["sla"]["late_penalty_pct_per_day"], "penalty_cap_%": p["sla"]["max_penalty_pct"],
        } for p in pos]), hide_index=True, width="stretch",
            column_config={"amount_usd": st.column_config.NumberColumn("Amount", format="$%.2f")})
    else:
        st.info("No reroute POs yet.")
    st.subheader("Audit log")
    st.dataframe(pd.DataFrame(api("GET", "/audit", params={"limit": 200})), hide_index=True, width="stretch")

# ------------------------------------------------------------------ metrics
with metrics_tab:
    m = api("GET", "/metrics")
    st.subheader("Semantic cache (this API process)")
    cols = st.columns(max(1, len(m["cache"])))
    for col, (ns, c) in zip(cols, m["cache"].items()):
        with col:
            st.markdown(f"**{ns}** · embedder `{c['embedder']}`")
            a, b = st.columns(2)
            a.metric("Hit rate", f"{c['hit_rate'] * 100:.0f}%")
            b.metric("Lookups", c["lookups"])
            tiers = pd.DataFrame({"tier": ["L1 exact", "L3 persistent", "L2 semantic", "miss"],
                                  "count": [c["l1_hits"], c["l3_hits"], c["l2_hits"], c["misses"]]}).set_index("tier")
            st.bar_chart(tiers, color=SERIES, horizontal=True, height=180)
            st.caption(f"{c['invalidated']} invalidated by disruptions · ~{c['tokens_saved']:,} LLM tokens saved")

    st.subheader("Dynamic model routing")
    by_task = pd.DataFrame(m["llm_by_task"]).fillna(0).astype(int)
    if not by_task.empty:
        st.dataframe(by_task, width="stretch")
    by_model = pd.DataFrame(m["llm_by_model"]).T
    if not by_model.empty:
        st.dataframe(by_model, width="stretch",
                     column_config={"cost_usd": st.column_config.NumberColumn("Est. cost", format="$%.4f")})
    st.subheader("Recent routing decisions")
    st.dataframe(pd.DataFrame(m["recent_llm_calls"]), hide_index=True, width="stretch")
    a1, a2, a3 = st.columns(3)
    a1.metric("Reroute POs", m["purchase_orders"]["reroute_pos"])
    a2.metric("Auto-executed", m["purchase_orders"]["auto_executed"])
    a3.metric("Approvals pending", m["approvals"]["pending"])

# ------------------------------------------------------------------ floating chat button (every tab)
st.markdown("""
<style>
.st-key-chat_fab { position: fixed; right: 28px; bottom: 28px; z-index: 1000; width: auto !important; }
.st-key-chat_fab button {
  width: 64px; height: 64px; border-radius: 50%; font-size: 28px; line-height: 1;
  background: #0a6f8f; color: #ffffff; border: none; box-shadow: 0 6px 18px rgba(10, 60, 80, 0.35);
}
.st-key-chat_fab button:hover { background: #085a74; color: #ffffff; }
.st-key-chat_fab button:focus-visible { outline: 3px solid #f2b233; outline-offset: 3px; }
</style>
""", unsafe_allow_html=True)
with st.container(key="chat_fab"):
    if st.button("💬", key="open_chat", help="Open the OceanBridge Assistant"):
        st.session_state["chat_open"] = True
# Stays open across reruns until the user closes it (X, Esc or clicking outside).
if st.session_state.get("chat_open"):
    chat_dialog()
