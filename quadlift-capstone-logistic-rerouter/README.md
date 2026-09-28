# OceanBridge Logistics: Global Supply Chain Disruption & Autonomous Rerouting System

Capstone Pod 3. A multi-agent system (CrewAI + OpenAI) that:

- detects shipment delay risk from weather, maritime, port-congestion and geopolitical feeds;
- evaluates sea, air, rail and multimodal alternatives;
- negotiates carrier SLAs and issues adjusted purchase orders.

Every commitment goes through a deterministic **human-in-the-loop (HITL)** approval gate.

```
 feeds (weather · AIS/port status · congestion index · geopolitical)
        │
        ▼
 ┌──────────────────────────┐   gpt-4o-mini
 │ Disruption Monitoring    │── sensor fusion pre-score + LLM validation / explanation
 └────────────┬─────────────┘
              ▼  at-risk shipments
 ┌──────────────────────────┐   gpt-4o-mini ↔ gpt-4o (complexity-routed, escalates on bad output)
 │ Route & Capacity Optimizer│── MCP: list_route_alternatives, calculate_freight_cost
 └────────────┬─────────────┘   ▲ multi-tier semantic cache (L1 exact · L3 persistent · L2 semantic)
              ▼
 ┌──────────────────────────┐  auto if cost increase < 5 %  AND  cargo value < $250k
 │ HITL gate (plain code)    │─────────────────────────────────────────────┐
 └────────────┬─────────────┘  otherwise → Logistics Operations Manager     │
              │ approval queue (API/UI) ── approve ──┐                      │
              ▼                                      ▼                      ▼
 ┌──────────────────────────┐   gpt-4o
 │ Vendor Negotiation & PO   │── MCP: submit_sla_proposal, issue_purchase_order (re-checks the HITL policy)
 └──────────────────────────┘
```

## Quick start

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # installs all 10 workspace packages (editable)
copy .env.example .env            # optional: set OPENAI_API_KEY to use the real LLM agents
.\.venv\Scripts\python.exe -m oceanbridge.seed

# terminal 1: API (http://127.0.0.1:8000/docs)
.\.venv\Scripts\python.exe -m oceanbridge.api --port 8000
# terminal 2: control tower UI (http://localhost:8501)
.\.venv\Scripts\python.exe -m oceanbridge.ui
```

The UI finds the API on ports 8000–8002 by itself; set `OB_API_URL` only if it runs elsewhere. The older commands still work: `python -m uvicorn oceanbridge.api.main:app --port 8000` for the API and `streamlit run packages\ui\src\oceanbridge\ui\app.py` for the UI. With [uv](https://docs.astral.sh/uv/), `uv sync --all-packages` installs the workspace instead of pip.

The agent backend is chosen by `llm_mode` in [config/settings.yaml](config/settings.yaml) (env `OB_LLM_MODE`):

| mode | behaviour |
|---|---|
| `auto` (default) | CrewAI + OpenAI when `OPENAI_API_KEY` is set, otherwise the deterministic offline backend |
| `crewai` | always CrewAI agents (the MCP server is started over stdio automatically) |
| `offline` | deterministic agents with the same contracts. Used by the tests and as the LLM fallback. |

### Demo script
1. In the UI sidebar, toggle **Rotterdam dock-worker strike** and click **Run rerouting pipeline**.
   - Low-value shipments that can be diverted via Antwerp for less than +5% are **auto-executed**: the carrier negotiation runs and a PO is issued that supersedes the original PO.
   - High-value or high-cost reroutes go to the **Approval Queue**.
2. In **Approval Queue**, approve one proposal. This triggers the negotiation agent, and the PO carries the approval id. Reject another; the shipment returns to its booked status.
3. Run the pipeline again. The rejected shipment is re-evaluated, and its decision is served from the **L1/L3 cache**, with no LLM call.
4. In **Reroute Proposals**, run a what-if for the same shipment, then change the weight by ~5%. You get an **L2 semantic** hit. Change it by 50% and the structural guard forces a miss.
5. In **Metrics**, check the cache hit rates per tier and the model-routing split (status and monitoring on gpt-4o-mini; complex optimisation and negotiation on gpt-4o).

Other scenarios: `shanghai_typhoon`, `red_sea_crisis`, `la_lb_congestion`, `panama_drought`.

## Documentation

| Document | What it covers |
|---|---|
| [docs/llm-vs-offline.md](docs/llm-vs-offline.md) | What each part does with an LLM and without one, and which `.py` file holds the logic |
| [docs/chatbot.md](docs/chatbot.md) | Chat Assistant: tool calling over MCP and local tools, memory, sessions, API, safety rules |
| [docs/caching.md](docs/caching.md) | How the L1, L2 and L3 cache tiers are filled, looked up, expired and invalidated |
| [docs/database.md](docs/database.md) | Where the SQLite database is, how it is configured, every table, common tasks, and moving to PostgreSQL |
| [docs/mcp-server.html](docs/mcp-server.html) | MCP server reference: transports, clients, all 5 tools with real examples |
| [docs/request-flow.html](docs/request-flow.html) | Interactive step-by-step animation of the request flows |
| [docs/architecture.jpg](docs/architecture.jpg) | Architecture diagram (rebuild with `python docs/build_architecture.py`) |

## Code layout

The repository is a **workspace of 10 Python packages**, each with its own `pyproject.toml`, dependencies and tests. They share the import namespace `oceanbridge` (and `oceanbridge.agents`), so imports look the same whichever package a module lives in.

```
packages/
├── core/                    oceanbridge-core          config · models · seed · core/ (service, routing, freight,
│                                                      risk, geo, db, repository) · feeds/ · cache/ · llm/router
│                                                      · hitl/ (gate, guard, approvals, errors)
├── mcp-servers/             oceanbridge-mcp-servers   every MCP server + the registry clients use to launch them
│   └── src/oceanbridge/mcp_servers/supply_chain_core/ python -m oceanbridge.mcp_servers.supply_chain_core
├── agents/                  one package per agent
│   ├── common/              oceanbridge-agent-common              backend interface, run context,
│   │                                                               CrewAI runner, MCP tool loader
│   ├── disruption-monitor/  oceanbridge-agent-disruption-monitor  agent.yaml · schemas · agent · feed tools
│   ├── route-optimizer/     oceanbridge-agent-route-optimizer     agent.yaml · schemas · agent · optimizer
│   ├── vendor-negotiator/   oceanbridge-agent-vendor-negotiator   agent.yaml · schemas · agent · workflow
│   ├── status-analyst/      oceanbridge-agent-status-analyst      agent.yaml · schemas · agent
│   └── chat-assistant/      oceanbridge-agent-chat-assistant      agent.yaml · tools · agent · intents · memory
├── app/                     oceanbridge-app           runtime/ (backends, approval wiring) · flow/ · chat/ · api/
└── ui/                      oceanbridge-ui            Streamlit dashboard (python -m oceanbridge.ui)

config/settings.yaml · data/ · docs/ · conftest.py (shared test fixtures)
requirements.txt (installs every package) · pyproject.toml (workspace: pytest, ruff, uv members)
```

Every package has the same shape: `packages/<name>/pyproject.toml`, code under `src/oceanbridge/...`, tests in `tests/`.

**Dependencies between packages.** A package may import only the packages it depends on. `packages/app/tests/test_package_boundaries.py` scans every import and fails the build otherwise.

| Package | Depends on |
|---|---|
| `core` | third-party libraries only |
| `mcp-servers` | core |
| `agents/common` | core, mcp-servers |
| each agent (`disruption-monitor`, `route-optimizer`, `vendor-negotiator`, `status-analyst`, `chat-assistant`) | core, agents/common |
| `app` | core, mcp-servers, agents/common, all five agents |
| `ui` | core (talks to the app only over HTTP) |

Two things are connected only inside the app, so that no lower-level package depends on a higher one:

| Connection | Where | Why |
|---|---|---|
| Approving an approval runs the Vendor Negotiation agent | `oceanbridge.runtime.execute_approval` passes the agent into `hitl.approvals.approve_and_execute` | `hitl` is part of core, which must not import agents |
| The chatbot runs the pipeline and what-if | `chat.service` passes `run_pipeline` / `optimize_what_if` into the chatbot's `ToolContext` | the agent package must not import the app |

Each agent package has the same files:
- `agent.yaml` holds the role, goal, backstory and task prompt.
- `schemas.py` holds the structured output the LLM must return.
- `agent.py` has `run_llm()` for CrewAI and `run_offline()` for the deterministic version.

**Adding to the workspace:**
- **An MCP server:** add `packages/mcp-servers/src/oceanbridge/mcp_servers/<name>/` with `server.py` and `__main__.py`, and register it in `mcp_servers/__init__.py`. The agents find its tools by name through `agents/common/mcp_tools.py`.
- **An agent:** copy an agent package and rename it. Then add it to `requirements.txt` and to `oceanbridge-app`'s dependencies, and wire it in `runtime/backends.py`.

## Components

| Requirement | Where |
|---|---|
| Disruption Monitoring Agent | [agents/disruption_monitor/](packages/agents/disruption-monitor/src/oceanbridge/agents/disruption_monitor), sensor fusion in [core/risk.py](packages/core/src/oceanbridge/core/risk.py), feeds in [feeds/](packages/core/src/oceanbridge/feeds) |
| Route & Capacity Optimization Agent | [agents/route_optimizer/](packages/agents/route-optimizer/src/oceanbridge/agents/route_optimizer), alternatives and landed-cost score in [core/routing.py](packages/core/src/oceanbridge/core/routing.py) |
| Vendor Negotiation & PO Agent | [agents/vendor_negotiator/](packages/agents/vendor-negotiator/src/oceanbridge/agents/vendor_negotiator), carrier simulation and POs in [core/service.py](packages/core/src/oceanbridge/core/service.py) |
| MCP Supply Chain Core API | [mcp_servers/supply_chain_core/server.py](packages/mcp-servers/src/oceanbridge/mcp_servers/supply_chain_core/server.py): `get_shipment_status`, `calculate_freight_cost`, `issue_purchase_order`, plus `list_route_alternatives`, `submit_sla_proposal` |
| HITL gate | [hitl/](packages/core/src/oceanbridge/hitl): policy in `gate.py`, enforced in the flow **and** again inside `issue_purchase_order` via `guard.py`; workflow in `approvals.py` |
| Multi-tier semantic cache | [cache/semantic_cache.py](packages/core/src/oceanbridge/cache/semantic_cache.py) |
| Dynamic model routing | [llm/router.py](packages/core/src/oceanbridge/llm/router.py) |
| Orchestration | [flow/reroute_flow.py](packages/app/src/oceanbridge/flow/reroute_flow.py) (CrewAI Flow with `@start` / `@listen` / `@router`) |
| Chatbot (agentic, memory, sessions) | [agents/chat_assistant/](packages/agents/chat-assistant/src/oceanbridge/agents/chat_assistant), [chat/](packages/app/src/oceanbridge/chat), endpoints in [api/chat_routes.py](packages/app/src/oceanbridge/api/chat_routes.py); see [docs/chatbot.md](docs/chatbot.md) |
| API / UI | [api/main.py](packages/app/src/oceanbridge/api/main.py), [packages/ui/src/oceanbridge/ui/app.py](packages/ui/src/oceanbridge/ui/app.py) |

### HITL policy
`evaluate(cost_increase_pct, cargo_value)` auto-executes only when **cost increase < 5% AND cargo value < $250,000**. Anything else creates an `ApprovalRequest` for the Logistics Operations Manager.

The policy is never decided by an LLM. `issue_purchase_order` re-applies it, so even a misbehaving agent calling the MCP tool directly is refused unless it presents the id of an **APPROVED**, not-yet-executed approval. The PO amount also cannot exceed the quoted route cost.

The thresholds are configurable (`OB_HITL__MAX_COST_INCREASE_PCT`, `OB_HITL__MAX_CARGO_VALUE_USD`). Every decision is written to the audit log.

### Semantic cache
The cache has three tiers:

| Tier | Storage | Match | Speed |
|---|---|---|---|
| L1 | in-process TTL LRU | exact key | sub-ms |
| L3 | SQLite (persistent) | exact key | exact primary-key read |
| L2 | embeddings | semantic: cosine ≥ 0.92 plus a structural guard | approximate |

**Lookup order:** L1 → L3 → L2, so an exact answer always wins over a similar one.

**Structural guard (L2):** a hit requires the same ports, route and mode, the same disruption fingerprint, the same value band and delivery-slack band, and weight/volume within ±10%. This stops similar-sounding queries for a different lane from reusing a wrong route.

**What gets cached:** route decisions are cached as a *strategy* (a route signature) and re-priced for each shipment, so cached answers never reuse stale prices. Freight quotes are cached by lane, carrier and chargeable units.

**Invalidation:** each entry records the ports and chokepoints its route touches. A new disruption at any of those locations evicts the entry, and entries created during a disruption get a short TTL.

**Embeddings:** `text-embedding-3-small` when an OpenAI key is present, otherwise a local hashed n-gram embedder.

### Model routing
| Task | Model |
|---|---|
| Status checks, disruption monitoring | light (`gpt-4o-mini`) |
| Route optimisation | complexity score over: number of candidates, multimodal options, cargo value, compounding disruptions, in-transit diversion. **≥ 3 → gpt-4o**, else gpt-4o-mini |
| Negotiation | heavy (`gpt-4o`) |

If the light model's output fails validation, the task escalates to gpt-4o once, then falls back to the deterministic agent. Each call is recorded with its routing reason, token counts and estimated cost (`/metrics`).

## How to test

There are four ways to test the system. Start with the automated tests. They need no API key and no running servers.

| # | Method | Needs servers? | Needs OpenAI key? |
|---|---|---|---|
| 1 | Automated test suite (pytest) | No | No |
| 2 | REST API (Swagger UI or PowerShell) | API | No |
| 3 | Streamlit dashboard | API + UI | No |
| 4 | MCP server on its own | No | No |
| 5 | Real LLM agents (CrewAI + OpenAI) | API + UI | **Yes** |

All commands run from the project root in PowerShell. Complete the [Quick start](#quick-start) first (venv, `pip install -r requirements.txt`, seed).

### 1. Automated test suite

```powershell
# everything (about 40 s, 48 tests)
.\.venv\Scripts\python.exe -m pytest -q

# verbose, one file, or one test
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m pytest packages\core\tests\test_hitl_gate.py -v
.\.venv\Scripts\python.exe -m pytest packages\app\tests\test_pipeline.py::test_end_to_end_rotterdam_strike -v

# one package's tests only
.\.venv\Scripts\python.exe -m pytest packages\core
.\.venv\Scripts\python.exe -m pytest packages\app
```

The tests always run in offline mode. Each test gets its own temporary, freshly seeded database, so they never touch `data/oceanbridge.db`.

| File | What it proves |
|---|---|
| `test_hitl_gate.py` | Gate boundaries: 4.99% / $249,999 auto-executes; exactly 5% or exactly $250k needs sign-off; reasons name each breached limit; thresholds are configurable |
| `test_freight_and_routing.py` | Suez vs Cape distance and cost, air vs sea tradeoff, TEU and chargeable-kg maths, impossible lanes rejected (e.g. no truck Shanghai→Busan), reroutes never land later than absorbing the delay |
| `test_semantic_cache.py` | L1 exact hit, L2 semantic hit within the guard, guard blocks a different port or a +40% weight, L3 survives a restart, eviction when a disruption hits the route |
| `test_model_router.py` | Status checks and monitoring go to gpt-4o-mini, negotiation to gpt-4o, optimisation by complexity score, escalation, cost estimate |
| `test_service_policy.py` | Auto PO supersedes the original; PO refused without sign-off; approval must be APPROVED and is single-use; PO amount capped at the quote; negotiation limited to 3 rounds |
| `test_pipeline.py` | Full Flow on the Rotterdam strike: auto-executes, queues approvals, approve/reject, then a cache hit on the next run; what-if reaches the L2 semantic tier |
| `test_api.py` | FastAPI endpoints end to end, including 409 on a double approval and 404s |
| `test_mcp_server.py` | Starts the real MCP server over stdio, lists its tools and calls them; `issue_purchase_order` refuses a $10M shipment with no sign-off |
| `test_chat.py` | Chat Assistant: session invalidation and idle expiry, MCP tool calls with pronoun memory, approve-only-after-confirm, LLM tool loop and model routing (fake OpenAI client), offline fallback, summaries, `/chat` API |

### 2. REST API

Start the API: `.\.venv\Scripts\python.exe -m oceanbridge.api --port 8000`.

**Interactive testing.** Open **http://127.0.0.1:8000/docs** (Swagger UI). Every endpoint has a "Try it out" button.

**Scripted smoke test.** Paste this into a second PowerShell window. The expected output is in the comments.

```powershell
$base = "http://127.0.0.1:8000"
Invoke-RestMethod "$base/health"                                   # backend = offline | crewai

# 1) Reset data and inject a disruption
Invoke-RestMethod -Method Post "$base/admin/seed"
Invoke-RestMethod -Method Post "$base/scenarios/rotterdam_strike/activate"

# 2) Run the full agent pipeline and wait for the result
$run = Invoke-RestMethod -Method Post "$base/pipeline/run-sync" -ContentType "application/json" -Body '{}'
$run.situation_report
"at_risk=$($run.at_risk) auto=$($run.auto_executed.Count) queued=$($run.queued_for_approval.Count)"
#    -> at_risk=12 auto=2 queued=10
$run.auto_executed | Select-Object shipment_id, po_id, amount_usd, rounds    # auto-executed POs (< 5% and < $250k)

# 3) HITL: approve one request, reject another
$pending = Invoke-RestMethod "$base/approvals?status=pending"
$pending | Select-Object id, shipment_id, @{n="reasons";e={$_.decision.reasons -join "; "}}
$ok = Invoke-RestMethod -Method Post "$base/approvals/$($pending[0].id)/approve" `
      -ContentType "application/json" -Body '{"approver":"Jane Doe","comment":"Approved"}'
$ok.po_id; $ok.carrier_message                                     # PO issued under the approval id
Invoke-RestMethod -Method Post "$base/approvals/$($pending[1].id)/reject" `
      -ContentType "application/json" -Body '{"approver":"Jane Doe","comment":"Too expensive"}'
# approving the same request twice returns HTTP 409 Conflict
try { Invoke-RestMethod -Method Post "$base/approvals/$($pending[0].id)/approve" -ContentType "application/json" -Body '{"approver":"x"}' } catch { $_.Exception.Response.StatusCode }

# 4) Semantic cache via a what-if dry run (issues nothing)
$sid = $pending[2].shipment_id
$w   = (Invoke-RestMethod "$base/shipments/$sid").shipment.weight_kg
$q   = { param($b) (Invoke-RestMethod -Method Post "$base/optimize/what-if" -ContentType "application/json" -Body ($b | ConvertTo-Json)).cache_tier }
& $q @{ shipment_id = $sid }                                       # -> miss        (first time)
& $q @{ shipment_id = $sid }                                       # -> L1-exact    (identical query)
& $q @{ shipment_id = $sid; weight_kg = [math]::Round($w * 1.04) } # -> L2-semantic (similar query, inside guard)
& $q @{ shipment_id = $sid; weight_kg = [math]::Round($w * 1.5) }  # -> miss        (guard: weight off by 50%)

# 5) Model routing and metrics
(Invoke-RestMethod "$base/shipments/$sid/briefing").model          # status check -> routed to gpt-4o-mini
$m = Invoke-RestMethod "$base/metrics"
$m.cache.route_decisions | Select-Object l1_hits, l2_hits, l3_hits, misses, hit_rate
$m.llm_by_task                                                     # which model served which task type
Invoke-RestMethod "$base/audit" | Select-Object -First 10 ts, actor, action, subject
```

**Other useful endpoints:**

| Endpoint | Purpose |
|---|---|
| `GET /shipments` | All shipments with live risk |
| `GET /shipments/{id}` | Status, tracking and POs for one shipment (same data as the MCP tool) |
| `GET /shipments/{id}/alternatives` | Priced reroute options |
| `GET /disruptions` | Active disruptions |
| `GET /runs/{id}` | Step-by-step agent trace for a run |
| `GET /purchase-orders` | All POs |
| `POST /pipeline/run` | Asynchronous run; poll `GET /runs/{id}` for progress |

### 3. Streamlit dashboard

Start the UI: `.\.venv\Scripts\python.exe -m oceanbridge.ui`, then open **http://localhost:8501**. Work through this checklist:

1. **Sidebar → Reset demo data.** Every tab should show a clean state (48 shipments, 0 disruptions).
2. **Toggle *Rotterdam dock-worker strike* → Run rerouting pipeline.**
   - **Control Tower:** red arcs into Rotterdam, a disruption circle on Rotterdam, and a run trace showing each agent step, the model it used and the cache tier.
   - **Approval Queue:** 10 requests, each stating why sign-off is required (cost % and/or cargo value).
   - **Purchase Orders & Audit:** 2 auto-executed POs that supersede the `-ORIG` POs.
3. **Approval Queue → Approve & execute** one request. A PO is issued after up to 3 negotiation rounds and the carrier confirmation message is shown. **Reject** another; it moves to *Decided*.
4. **Run the pipeline again.** The rejected shipment is re-evaluated, and the trace shows `cache_tier = L1-exact`: no LLM call is made.
5. **Reroute Proposals:** pick a shipment and click **Optimise** twice. The cache readout goes from *miss* to *L1-exact*. Change the weight by about 4% to get *L2-semantic*, or by 50% to get *miss*. Raise the cargo value above $250k and the HITL readout flips to *Sign-off*.
6. **Metrics:** hit rate per cache tier, the model-routing split per task, and estimated LLM spend.
7. Try the other scenarios (Shanghai typhoon, Red Sea crisis, LA/LB congestion, Panama drought). Reset in between for clean results.

### 4. MCP server on its own

The Supply Chain Core MCP server runs over stdio (the agents use this) or SSE:

```powershell
# SSE transport on port 8765 (for remote clients)
.\.venv\Scripts\python.exe -m oceanbridge.mcp_servers.supply_chain_core --transport sse --port 8765

# Optional: visual testing with MCP Inspector (requires Node.js)
npx @modelcontextprotocol/inspector .\.venv\Scripts\python.exe -m oceanbridge.mcp_servers.supply_chain_core
```

In the Inspector, call these tools in order:
1. `get_shipment_status` with `{"shipment_id": "SHP-1017"}`.
2. `list_route_alternatives` for the same shipment.
3. `issue_purchase_order` with one of the returned option ids. It returns `PolicyViolation` because the cargo is worth $10M and there is no approval.

The automated equivalent is `pytest packages\mcp-servers\tests\test_mcp_server.py -v`.

### 5. Real LLM agents (CrewAI + OpenAI)

1. Put `OPENAI_API_KEY=sk-...` in `.env`. With `llm_mode: auto`, the key is detected automatically; to require it, set `OB_LLM_MODE=crewai`.
2. Restart the API. `GET /health` should now report `"backend": "crewai"`.
3. Repeat steps 2–6 of the dashboard checklist. Differences to look for:
   - The situation report and rationales are written by the LLM.
   - The **Metrics** tab shows real token counts and cost, split between `gpt-4o-mini` (monitoring, status checks, simple optimisations) and `gpt-4o` (complex optimisations, negotiation).
   - The negotiation agent calls `submit_sla_proposal` and `issue_purchase_order` through MCP.
4. **Safety checks still hold:** a high-value reroute is never executed without approval. The MCP tool refuses it even if the LLM tries, and the refusal appears in the audit log as `po_refused`.
5. **If the LLM misbehaves:** a warning in `errors` in the run summary means an agent's output was unusable. The task escalated to gpt-4o and/or the deterministic fallback finished it, and the run still completes.

### Testing different HITL thresholds

Thresholds come from `config/settings.yaml` and can be overridden with environment variables. Restart the API after changing them:

```powershell
$env:OB_HITL__MAX_COST_INCREASE_PCT = "15"
$env:OB_HITL__MAX_CARGO_VALUE_USD  = "1000000"
.\.venv\Scripts\python.exe -m oceanbridge.api --port 8000
```

With these looser limits, the Rotterdam strike run auto-executes more reroutes and queues fewer approvals.

### Troubleshooting

| Symptom | Fix |
|---|---|
| UI says "The OceanBridge API is not running" | Start the API first (`python -m oceanbridge.api --port 8000`), or set `OB_API_URL` if it is not on ports 8000–8002 |
| Stale results after many runs | Sidebar → **Reset demo data**, or `POST /admin/seed` |
| `database is locked` | Stop duplicate API processes; only one API should use `data/oceanbridge.db` |
| Port 8000 / 8501 already in use | Add `--port 8001` (API; then set `OB_API_URL`) or `--server.port 8502` (UI) |

## Notes & limitations
- All external feeds and carriers are simulated (deterministic per day, with scenario overlays). Freight tariffs are illustrative.
- The in-memory cache tiers are per process: the API and the MCP subprocess each keep their own L1/L2 and share L3.
- The CrewAI backend has been checked for MCP tool loading and agent/task construction. Running the LLM agents end to end requires an `OPENAI_API_KEY`.
