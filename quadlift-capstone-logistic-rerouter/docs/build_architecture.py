"""Builds docs/architecture.html and renders it to docs/architecture.jpg (headless Edge + Pillow).

Usage:  .venv\\Scripts\\python.exe docs\\build_architecture.py [--icons DIR]
Logos come from Simple Icons (https://simpleicons.org); pass --icons to use a local folder of SVGs,
otherwise they are downloaded from the jsDelivr CDN.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
import time
import urllib.request
from html import escape
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
W, H = 1820, 1420
SCALE = 2

BRANDS = {  # simple-icons slug -> brand colour
    "streamlit": "#FF4B4B", "fastapi": "#009688", "crewai": "#FF5A50", "openai": "#412991",
    "modelcontextprotocol": "#1A1A1A", "sqlite": "#003B57", "python": "#3776AB", "pydantic": "#E92063",
    "sqlalchemy": "#D71F00", "numpy": "#4D77CF", "pytest": "#0A9EDC", "pandas": "#150458",
}

# Generic symbols (24x24, stroke-based) for components that are not third-party products.
SYMBOLS = {
    "person": '<circle cx="12" cy="7.5" r="3.6"/><path d="M4.5 20.5c.8-4.2 3.8-6.4 7.5-6.4s6.7 2.2 7.5 6.4"/>',
    "terminal": '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M7 10l3 2.5L7 15M12.5 15.5H17"/>',
    "shield": '<path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z"/><path d="M8.6 12.2l2.4 2.4 4.4-4.6"/>',
    "layers": '<path d="M12 3.5l8.5 4.3L12 12 3.5 7.8z"/><path d="M3.5 12L12 16.3 20.5 12M3.5 16.2L12 20.5l8.5-4.3"/>',
    "branch": '<circle cx="6" cy="5.5" r="2.2"/><circle cx="6" cy="18.5" r="2.2"/><circle cx="18" cy="9" r="2.2"/><path d="M6 7.7v8.6M6 13c0-3 2.5-4 9.8-4"/>',
    "radar": '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><path d="M12 12l6-6"/><circle cx="12" cy="12" r="1.2"/>',
    "cube": '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/>',
    "gear": '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.8v3M12 18.2v3M2.8 12h3M18.2 12h3M5.5 5.5l2.1 2.1M16.4 16.4l2.1 2.1M5.5 18.5l2.1-2.1M16.4 7.6l2.1-2.1"/>',
    "agent": '<rect x="5" y="7" width="14" height="11" rx="3"/><circle cx="9.5" cy="12.5" r="1.3"/><circle cx="14.5" cy="12.5" r="1.3"/><path d="M12 7V4M10 4h4"/>',
}

# Company mark: stylised container ship on a wave, drawn for this diagram.
SHIP_MARK = """<svg viewBox="0 0 64 64" width="54" height="54" aria-hidden="true">
  <rect width="64" height="64" rx="14" fill="#0a6f8f"/>
  <rect x="16" y="19" width="9" height="9" rx="1.2" fill="#f2b233"/>
  <rect x="26.5" y="19" width="9" height="9" rx="1.2" fill="#ffffff"/>
  <rect x="37" y="19" width="9" height="9" rx="1.2" fill="#f2b233"/>
  <rect x="21" y="9.5" width="9" height="9" rx="1.2" fill="#ffffff"/>
  <path d="M9 30h46l-6.5 12h-33z" fill="#ffffff"/>
  <path d="M6 48c4.5 0 4.5-3 9-3s4.5 3 9 3 4.5-3 9-3 4.5 3 9 3 4.5-3 9-3 4.5 3 9 3" fill="none" stroke="#ffffff" stroke-width="3" stroke-linecap="round"/>
</svg>"""


def load_icons(icon_dir: Path | None) -> dict[str, str]:
    paths = {}
    for slug in BRANDS:
        if icon_dir and (icon_dir / f"{slug}.svg").exists():
            svg = (icon_dir / f"{slug}.svg").read_text(encoding="utf-8")
        else:
            url = f"https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/{slug}.svg"
            svg = urllib.request.urlopen(url, timeout=20).read().decode()
        paths[slug] = re.search(r'<path d="([^"]+)"', svg).group(1)
    return paths


ICONS: dict[str, str] = {}


def icon(name: str, size: int = 22) -> str:
    if name in BRANDS:
        return (f'<svg class="ic" viewBox="0 0 24 24" width="{size}" height="{size}" aria-hidden="true">'
                f'<path fill="{BRANDS[name]}" d="{ICONS[name]}"/></svg>')
    return (f'<svg class="ic sym" viewBox="0 0 24 24" width="{size}" height="{size}" aria-hidden="true" fill="none" '
            f'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{SYMBOLS[name]}</svg>')


# ---------------------------------------------------------------------------------------------- layout
L = 70            # left edge of main column (layer rail lives left of it)
RC = 1160         # right column x
RW = 330          # right column width
SB = 1530         # sidebar x
SBW = 260
BOX = {
    "op":     (L, 110, 220, 150),
    "ui":     (L + 250, 110, 560, 150),
    "cli":    (L + 840, 110, 250, 150),
    "api":    (L + 250, 310, 1090 - 250 + RW - 0, 115),
    "flow":   (L, 475, 690, 230),
    "gate":   (L + 750, 475, 290, 230),
    "cache":  (RC, 475, RW, 230),
    "agents": (L, 755, 1040, 185),
    "router": (RC, 755, RW, 185),
    "feeds":  (L, 990, 360, 170),
    "mcp":    (L + 390, 990, 650, 170),
    "openai": (RC, 990, RW, 170),
    "core":   (L, 1210, 1040, 160),
    "db":     (RC, 1210, RW, 160),
}
LAYERS = [("Presentation", 110, 260), ("API", 310, 425), ("Orchestration", 475, 705),
          ("Agents", 755, 940), ("Tools / MCP", 990, 1160), ("Domain & data", 1210, 1370)]


def pos(key: str) -> str:
    x, y, w, h = BOX[key]
    return f"left:{x}px;top:{y}px;width:{w}px;height:{h}px"


def head(ic: str, title: str, path: str = "", tag: str = "") -> str:
    t = f'<span class="tag">{escape(tag)}</span>' if tag else ""
    p = f'<div class="path">{escape(path)}</div>' if path else ""
    return f'<div class="hd">{icon(ic)}<div class="tt"><div class="title">{escape(title)}{t}</div>{p}</div></div>'


def chips(items, cls="chip") -> str:
    return '<div class="chips">' + "".join(f'<span class="{cls}">{escape(i)}</span>' for i in items) + "</div>"


def build_html() -> str:
    b = []
    b.append(f'<div class="box user" style="{pos("op")}">{head("person", "Logistics Ops Manager", "operator · approver")}'
             '<p>Runs scenarios, reviews proposals, approves or rejects reroutes that exceed the HITL limits.</p></div>')
    b.append(f'<div class="box" style="{pos("ui")}">{head("streamlit", "Control Tower UI", "packages/ui · ui/app.py", "Streamlit")}'
             + chips(["Control Tower map · pydeck", "Reroute Proposals · what-if", "Approval Queue",
                      "Purchase Orders & Audit", "Metrics · cache & model routing"]) + "</div>")
    b.append(f'<div class="box" style="{pos("cli")}">{head("terminal", "API clients", "Swagger /docs · curl · PowerShell")}'
             '<p>Scripted smoke tests and demos against the same endpoints.</p></div>')
    b.append(f'<div class="box api" style="{pos("api")}">{head("fastapi", "REST API", "packages/app · api/main.py · chat_routes.py", "FastAPI · uvicorn")}'
             + chips(["POST /pipeline/run · run-sync", "POST /approvals/{id}/approve · reject", "POST /optimize/what-if",
                      "GET /shipments · /{id}/briefing · /alternatives", "POST /scenarios/{name}/activate",
                      "GET /disruptions · /runs/{id}", "GET /purchase-orders · /audit", "GET /metrics", "POST /admin/seed"],
                     "chip mono") + "</div>")
    b.append(f'<div class="box orch" style="{pos("flow")}">{head("crewai", "Reroute Flow", "packages/app · flow/ · runtime/", "CrewAI Flow")}'
             '<div class="chain">'
             '<span class="st">@start<b>detect_disruptions</b></span><i>→</i>'
             '<span class="st">@listen<b>assess_shipments</b></span><i>→</i>'
             '<span class="st">@listen<b>optimize_routes</b></span><i>→</i>'
             '<span class="st gate">@router<b>hitl_gate</b></span></div>'
             '<div class="chain second"><span class="lbl">"dispatch" →</span>'
             '<span class="st">@listen<b>execute_auto_reroutes</b></span>'
             '<span class="st">@listen<b>queue_for_approval</b></span></div>'
             '<p class="small"><b>RouteOptimizer:</b> semantic cache → routed LLM → escalate once to gpt-4o → deterministic fallback. '
             '<b>Entry points:</b> run_pipeline · optimize_what_if (approvals → hitl/approvals.py)</p></div>')
    b.append(f'<div class="box policy" style="{pos("gate")}">{head("shield", "HITL Gate", "packages/core · hitl/", "plain code")}'
             '<div class="rule">auto-execute iff<br><b>cost increase &lt; 5%</b><br>AND <b>cargo value &lt; $250k</b></div>'
             '<p class="small">Otherwise queued for Logistics Ops Manager sign-off. Never delegated to an LLM; '
             're-checked inside <code>issue_purchase_order</code>.</p></div>')
    b.append(f'<div class="box" style="{pos("cache")}">{head("layers", "Semantic Cache", "packages/core · cache/")}'
             '<div class="tiers"><div><b>L1</b> exact · in-memory TTL LRU <span class="lib">cachetools</span></div>'
             '<div><b>L3</b> exact · persistent <span class="lib">SQLite</span></div>'
             '<div><b>L2</b> semantic · cosine ≥ 0.92 + structural guard <span class="lib">NumPy</span></div></div>'
             '<p class="small">Lookup L1 → L3 → L2 · evicted when a disruption hits the route · strategies re-priced on hit</p></div>')
    agents = [("Disruption Monitor", "gpt-4o-mini", "sensor fusion + risk report"),
              ("Route & Capacity Optimizer", "4o-mini ↔ 4o", "ranks priced alternatives"),
              ("Vendor Negotiation & PO", "gpt-4o", "SLA rounds + adjusted PO"),
              ("Status Analyst", "gpt-4o-mini", "shipment briefings")]
    cards = "".join(f'<div class="agent">{icon("agent", 18)}<div><b>{escape(n)}</b><span class="model">{escape(m)}</span>'
                    f'<span class="what">{escape(w)}</span></div></div>' for n, m, w in agents)
    b.append(f'<div class="box agentsbox" style="{pos("agents")}">{head("crewai", "Agents", "packages/agents/* · one package each", "CrewAI")}'
             f'<div class="agents">{cards}</div>'
             '<div class="backends"><span class="be">CrewAIBackend (app runtime/backends.py) · agents/*/agent.yaml · MCPServerAdapter</span>'
             '<span class="be alt">OfflineBackend · run_offline() per agent</span></div></div>')
    b.append(f'<div class="box" style="{pos("router")}">{head("branch", "Model Router", "packages/core · llm/router.py")}'
             '<div class="tiers"><div>status check · monitoring → <b>gpt-4o-mini</b></div>'
             '<div>optimisation · complexity ≥ 3 → <b>gpt-4o</b></div>'
             '<div>negotiation → <b>gpt-4o</b></div></div>'
             '<p class="small">Escalates on invalid output · logs tokens & cost</p></div>')
    b.append(f'<div class="box" style="{pos("feeds")}">{head("radar", "Feed Tools", "packages/agents/disruption-monitor · tools.py", "CrewAI BaseTool")}'
             + chips(["marine_weather_feed", "port_congestion_index", "maritime_port_status", "geopolitical_risk_feed",
                      "vessel_tracking"], "chip mono") + "</div>")
    b.append(f'<div class="box mcp" style="{pos("mcp")}">{head("modelcontextprotocol", "MCP Server · Supply Chain Core API", "packages/mcp-servers · supply_chain_core/", "FastMCP · stdio / SSE")}'
             + chips(["get_shipment_status", "calculate_freight_cost", "issue_purchase_order", "list_route_alternatives",
                      "submit_sla_proposal"], "chip mono strong")
             + '<p class="small">issue_purchase_order refuses without an APPROVED, unused approval when the HITL limits are exceeded.</p></div>')
    b.append(f'<div class="box ext" style="{pos("openai")}">{head("openai", "OpenAI API", "external · optional")}'
             + chips(["gpt-4o-mini", "gpt-4o", "text-embedding-3-small"], "chip mono")
             + '<p class="small">Without a key the system runs fully offline.</p></div>')
    b.append(f'<div class="box" style="{pos("core")}">{head("cube", "Supply Chain Core · domain", "packages/core · core/ · feeds/")}'
             '<div class="grid2">'
             '<div><code>service.py</code> status · cached freight · alternatives · SLA negotiation · HITL-enforced POs</div>'
             '<div><code>routing.py</code> RouteEngine: Suez/Cape, alt ports + truck, rail, air, sea-air, land bridge</div>'
             '<div><code>risk.py</code> sensor fusion → disruptions → shipment risk</div>'
             '<div><code>freight.py</code> tariffs per mode · TEU / chargeable kg · CO₂</div>'
             '<div><code>geo.py · reference.py</code> 18 ports · 7 chokepoints · 9 carriers</div>'
             '<div><code>feeds/</code> weather · AIS · port congestion · geopolitics · 5 scenarios</div>'
             '</div></div>')
    b.append(f'<div class="box" style="{pos("db")}">{head("sqlite", "SQLite · WAL", "packages/core · core/db.py", "SQLAlchemy")}'
             + chips(["shipments", "carriers", "purchase_orders", "approvals", "disruptions", "runs", "route_proposals",
                      "negotiations", "audit_log", "cache_entries", "llm_usage"], "chip mono tiny") + "</div>")

    # sidebar
    stack = [("python", "Python 3.12"), ("crewai", "CrewAI 1.15"), ("modelcontextprotocol", "MCP SDK 1.28"),
             ("openai", "OpenAI"), ("fastapi", "FastAPI"), ("streamlit", "Streamlit"), ("sqlalchemy", "SQLAlchemy 2"),
             ("sqlite", "SQLite"), ("pydantic", "Pydantic 2"), ("numpy", "NumPy"), ("pandas", "pandas"), ("pytest", "pytest")]
    side = '<div class="side" style="left:{}px;top:110px;width:{}px">'.format(SB, SBW)
    side += '<div class="sbox"><div class="sh">Tech stack</div><div class="stack">' + "".join(
        f'<span>{icon(s, 18)}{escape(t)}</span>' for s, t in stack) + "</div></div>"
    side += (f'<div class="sbox"><div class="sh">{icon("gear", 16)} Configuration</div>'
             '<p><code>config/settings.yaml</code> · <code>.env</code><br>pydantic-settings, <code>OB_*</code> overrides<br>'
             '<code>llm_mode</code>: auto | crewai | offline<br><code>seed.py</code>: 48 shipments, 9 carriers</p></div>')
    side += (f'<div class="sbox"><div class="sh">{icon("pytest", 16)} Tests · 48 passing</div>'
             '<p>HITL boundaries · freight & routing · cache tiers & guard · model router · PO policy · '
             'end-to-end Flow · FastAPI · chat · MCP · package boundaries</p></div>')
    side += ('<div class="sbox"><div class="sh">Legend</div><div class="legend">'
             '<span><svg width="34" height="10"><line x1="0" y1="5" x2="26" y2="5" class="lg"/><path d="M26 1l7 4-7 4z" class="lgh"/></svg>call / data flow</span>'
             '<span><svg width="34" height="10"><line x1="0" y1="5" x2="26" y2="5" class="lg dash"/><path d="M26 1l7 4-7 4z" class="lgh"/></svg>optional / external</span>'
             '<span><i class="sw policy"></i>deterministic policy</span>'
             '<span><i class="sw ext"></i>external service</span></div></div>')
    side += "</div>"

    rails = "".join(f'<div class="rail" style="top:{y0}px;height:{y1 - y0}px"><span>{escape(n)}</span></div>' for n, y0, y1 in LAYERS)
    return "\n".join(b) + side + rails


def arrows_svg() -> str:
    def box(k):
        return BOX[k]

    ox, oy, ow, oh = box("op"); ux, uy, uw, uh = box("ui"); cx_, cy_, cw, ch = box("cli")
    ax, ay, aw, ah = box("api"); fx, fy, fw, fh = box("flow"); gx, gy, gw, gh = box("gate")
    kx, ky, kw, kh = box("cache"); agx, agy, agw, agh = box("agents"); rx, ry, rw, rh = box("router")
    fdx, fdy, fdw, fdh = box("feeds"); mx, my, mw, mh = box("mcp"); oax, oay, oaw, oah = box("openai")
    cox, coy, cow, coh = box("core"); dx, dy, dw, dh = box("db")

    A = []  # (points, label, lx, ly, dashed, anchor)
    A.append(([(ox + ow, oy + 60), (ux, oy + 60)], "", 0, 0, False, "middle"))
    A.append(([(ux + 260, uy + uh), (ux + 260, ay)], "HTTP / JSON (httpx)", ux + 270, uy + uh + 30, False, "start"))
    A.append(([(cx_ + 125, cy_ + ch), (cx_ + 125, ay)], "HTTP", cx_ + 135, cy_ + ch + 30, False, "start"))
    A.append(([(fx + 360, ay + ah), (fx + 360, fy)], "run_pipeline() · optimize_what_if() · hitl.approvals", fx + 370, ay + ah + 20, False, "start"))
    A.append(([(fx + fw, fy + 95), (gx, fy + 95)], "evaluate", (fx + fw + gx) / 2, fy + 87, False, "middle"))
    # flow -> cache: over the top of the gate, through the gap between the API and orchestration rows
    A.append(([(fx + fw - 40, fy), (fx + fw - 40, fy - 12), (kx + 120, fy - 12), (kx + 120, ky)], "lookup · store", kx + 132, fy - 1, False, "start"))
    A.append(([(fx + 180, fy + fh), (fx + 180, agy)], "backend.monitor / optimize / negotiate", fx + 190, fy + fh + 30, False, "start"))
    A.append(([(gx + 145, gy + gh), (gx + 145, agy)], "auto → negotiate", gx + 155, gy + gh + 30, False, "start"))
    A.append(([(agx + agw, agy + 85), (rx, agy + 85)], "LLM", (agx + agw + rx) / 2, agy + 77, False, "middle"))
    A.append(([(rx + rw / 2, ry + rh), (rx + rw / 2, oay)], "crewai.LLM(model)", rx + rw / 2 + 10, ry + rh + 30, False, "start"))
    A.append(([(fdx + 180, agy + agh), (fdx + 180, fdy)], "local tools", fdx + 190, agy + agh + 30, False, "start"))
    A.append(([(mx + 325, agy + agh), (mx + 325, my)], "stdio · MCPServerAdapter", mx + 335, agy + agh + 30, False, "start"))
    A.append(([(fdx + 180, fdy + fdh), (fdx + 180, coy)], "read feeds", fdx + 190, fdy + fdh + 30, False, "start"))
    A.append(([(mx + 325, my + mh), (mx + 325, coy)], "core.service.*", mx + 335, my + mh + 30, False, "start"))
    A.append(([(cox + cow, coy + 75), (dx, coy + 75)], "SQL", (cox + cow + dx) / 2, coy + 67, False, "middle"))
    # cache -> OpenAI embeddings: dashed, down the right edge of the right column
    A.append(([(kx + kw, ky + 150), (kx + kw + 22, ky + 150), (kx + kw + 22, oay + 65), (oax + oaw, oay + 65)],
              "embeddings (optional)", kx + kw + 8, ky + 250, True, "start"))

    out = [f'<svg class="arrows" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
           '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto">'
           '<path d="M0 0L10 5L0 10z" fill="#0a6f8f"/></marker></defs>']
    for pts, label, lx, ly, dashed, anchor in A:
        d = "M" + " L".join(f"{x:.0f} {y:.0f}" for x, y in pts)
        out.append(f'<path d="{d}" class="ar{" dash" if dashed else ""}" marker-end="url(#ah)"/>')
        rot = f' transform="rotate(90 {lx} {ly})"' if label.startswith("embeddings") else ""
        if label:
            out.append(f'<text x="{lx}" y="{ly}" text-anchor="{anchor}" class="al"{rot}>{escape(label)}</text>')
    out.append("</svg>")
    return "".join(out)


CSS = """
*{box-sizing:border-box} html,body{margin:0}
body{width:%(W)spx;height:%(H)spx;background:#ffffff;color:#122229;font-family:"IBM Plex Sans",Segoe UI,Arial,sans-serif;position:relative;overflow:hidden}
.top{position:absolute;left:%(L)spx;top:22px;right:40px;display:flex;align-items:center;gap:16px}
.top h1{margin:0;font:700 30px/1.1 "IBM Plex Sans Condensed","Arial Narrow",sans-serif;letter-spacing:-.01em}
.top .sub{font:400 15px/1.3 "IBM Plex Sans",sans-serif;color:#56686d;margin-top:4px}
.top .meta{margin-left:auto;text-align:right;font:500 12px/1.5 "IBM Plex Mono",Consolas,monospace;color:#56686d}
.rail{position:absolute;left:14px;width:40px;border-right:2px solid #d5dfe1;display:flex;align-items:center;justify-content:center}
.rail span{transform:rotate(-90deg);white-space:nowrap;font:600 11px "IBM Plex Mono",monospace;letter-spacing:.14em;text-transform:uppercase;color:#6d8186}
.box{position:absolute;background:#f6f9f9;border:1.5px solid #b9c7ca;border-radius:10px;padding:12px 14px;overflow:hidden}
.box.api{background:#f1f8f7;border-color:#8cc5bf}
.box.orch{background:#fff7f6;border-color:#f0b5b0}
.box.agentsbox{background:#fff7f6;border-color:#f0b5b0}
.box.policy{background:#fffaf0;border:2px dashed #c98a00}
.box.mcp{background:#f4f4f4;border-color:#8f8f8f}
.box.ext{background:#f6f3fb;border:1.5px dashed #8e7bb8}
.box.user{background:#eef6f9;border-color:#8fbfd0}
.hd{display:flex;gap:10px;align-items:flex-start;margin-bottom:8px}
.ic{flex:none} .sym{color:#0a6f8f}
.title{font:600 16.5px/1.2 "IBM Plex Sans Condensed","Arial Narrow",sans-serif;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tag{font:500 10.5px "IBM Plex Mono",monospace;color:#0a6f8f;background:#e0eef2;border-radius:99px;padding:2px 8px}
.path{font:400 11.5px "IBM Plex Mono",Consolas,monospace;color:#56686d;margin-top:2px}
p{margin:0;font-size:12.5px;line-height:1.45;color:#2e4046} p.small{font-size:11.8px;margin-top:8px}
code{font:500 11.5px "IBM Plex Mono",Consolas,monospace;color:#0a4f66}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{font-size:12px;background:#ffffff;border:1px solid #cfdadc;border-radius:6px;padding:3px 8px;color:#20343a}
.chip.mono{font:500 11.5px "IBM Plex Mono",monospace}
.chip.strong{border-color:#6b6b6b;background:#fff}
.chip.tiny{font-size:10.8px;padding:2px 6px}
.chain{display:flex;align-items:center;gap:6px;margin-top:2px}
.chain.second{margin-top:8px;padding-left:4px}
.chain i{font-style:normal;color:#0a6f8f;font-weight:700}
.chain .lbl{font:500 11.5px "IBM Plex Mono",monospace;color:#56686d}
.st{display:flex;flex-direction:column;background:#fff;border:1px solid #efc2bd;border-radius:6px;padding:4px 8px;font:500 10.5px "IBM Plex Mono",monospace;color:#b4483f}
.st b{font:600 12px "IBM Plex Mono",monospace;color:#122229}
.st.gate{border:1.5px dashed #c98a00;color:#9a6a00}
.rule{background:#fff;border:1px solid #efd9a6;border-radius:8px;padding:8px 10px;font-size:13.5px;line-height:1.5;text-align:center}
.tiers{display:grid;gap:5px;font-size:12.3px}
.tiers div{background:#fff;border:1px solid #d6e0e2;border-radius:6px;padding:4px 8px}
.lib{font:500 10.5px "IBM Plex Mono",monospace;color:#56686d;float:right}
.agents{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.agent{display:flex;gap:8px;background:#fff;border:1px solid #efc2bd;border-radius:8px;padding:8px}
.agent .ic{color:#e05348;margin-top:1px}
.agent b{display:block;font-size:13px;line-height:1.25}
.agent .model{display:inline-block;margin-top:3px;font:500 11px "IBM Plex Mono",monospace;color:#412991;background:#efeaf8;border-radius:99px;padding:1px 7px}
.agent .what{display:block;font-size:11.5px;color:#56686d;margin-top:3px}
.backends{display:flex;gap:8px;margin-top:9px}
.be{font:500 11.2px "IBM Plex Mono",monospace;background:#fff;border:1px solid #cfdadc;border-radius:6px;padding:4px 8px}
.be.alt{color:#56686d}
.grid2{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px 14px;font-size:12.2px;line-height:1.4}
.side{position:absolute;display:grid;gap:14px}
.sbox{border:1.5px solid #d5dfe1;border-radius:10px;padding:12px 14px;background:#fbfcfc}
.sh{display:flex;align-items:center;gap:7px;font:600 12px "IBM Plex Mono",monospace;letter-spacing:.08em;text-transform:uppercase;color:#56686d;margin-bottom:8px}
.stack{display:grid;grid-template-columns:1fr 1fr;gap:8px 10px;font-size:12.5px}
.stack span{display:flex;align-items:center;gap:7px}
.legend{display:grid;gap:7px;font-size:12.3px}
.legend span{display:flex;align-items:center;gap:8px}
.lg{stroke:#0a6f8f;stroke-width:2} .lg.dash{stroke-dasharray:5 4} .lgh{fill:#0a6f8f}
.sw{width:30px;height:14px;border-radius:4px;display:inline-block}
.sw.policy{border:2px dashed #c98a00;background:#fffaf0}
.sw.ext{border:1.5px dashed #8e7bb8;background:#f6f3fb}
.arrows{position:absolute;left:0;top:0;pointer-events:none}
.ar{fill:none;stroke:#0a6f8f;stroke-width:2} .ar.dash{stroke-dasharray:6 5}
.al{font:500 11.5px "IBM Plex Mono",Consolas,monospace;fill:#0a4f66;paint-order:stroke;stroke:#ffffff;stroke-width:5px;stroke-linejoin:round}
.foot{position:absolute;left:%(L)spx;bottom:18px;font:400 11.5px "IBM Plex Mono",monospace;color:#7a8b90}
""" % {"W": W, "H": H, "L": L}


def page() -> str:
    top = (f'<div class="top">{SHIP_MARK}<div><h1>OceanBridge Logistics · Disruption &amp; Autonomous Rerouting</h1>'
           '<div class="sub">Code architecture of quadlift-capstone-logistic-rerouter · multi-agent pipeline with MCP, '
           'HITL approval gate, semantic caching and dynamic model routing</div></div>'
           '<div class="meta">packages/ · 10 packages · Python 3.12<br>CrewAI · MCP · FastAPI · Streamlit</div></div>')
    foot = ('<div class="foot">Product logos from Simple Icons (CC0); trademarks belong to their owners. '
            'Arrows show the direction of calls; responses flow back along the same path.</div>')
    return ('<!doctype html><html><head><meta charset="utf-8"><title>OceanBridge Architecture</title>'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600'
            '&family=IBM+Plex+Sans+Condensed:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">'
            f'<style>{CSS}</style></head><body>{top}{build_html()}{arrows_svg()}{foot}</body></html>')


def find_browser() -> str:
    for p in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"]:
        if Path(p).exists():
            return p
    raise SystemExit("Edge or Chrome is required to render the JPEG")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--icons", type=Path, default=None, help="folder with simple-icons SVGs (optional)")
    args = ap.parse_args()
    ICONS.update(load_icons(args.icons))
    html_path = HERE / "architecture.html"
    html_path.write_text(page(), encoding="utf-8")
    png = HERE / "architecture.png"
    # A throwaway profile keeps headless Edge/Chrome from attaching to an already-running browser.
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([find_browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        f"--user-data-dir={profile}", f"--force-device-scale-factor={SCALE}",
                        f"--window-size={W},{H}", "--virtual-time-budget=6000", f"--screenshot={png}",
                        html_path.as_uri()], check=True, capture_output=True, timeout=120)
        # msedge.exe hands off to a child process and returns early; wait for the screenshot to land.
        for _ in range(120):
            if png.exists() and png.stat().st_size > 0:
                time.sleep(0.5)
                break
            time.sleep(0.5)
        else:
            raise SystemExit("Browser did not produce a screenshot")
    Image.open(png).convert("RGB").save(HERE / "architecture.jpg", "JPEG", quality=92, optimize=True)
    png.unlink(missing_ok=True)
    print(f"wrote {HERE / 'architecture.jpg'}")


if __name__ == "__main__":
    main()
