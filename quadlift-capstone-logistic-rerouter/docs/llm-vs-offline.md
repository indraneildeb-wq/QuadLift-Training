# With LLM vs without LLM

The OceanBridge rerouting system runs in two modes:

| Mode | When | What runs the agents |
|---|---|---|
| **With LLM** (`crewai` backend) | `OPENAI_API_KEY` is set (or `llm_mode: crewai`) | CrewAI agents calling OpenAI `gpt-4o-mini` / `gpt-4o` |
| **Without LLM** (`offline` backend) | No key (or `llm_mode: offline`) | Deterministic Python with the same inputs and outputs. No network calls |

Everything that must be exact or auditable is plain code in **both** modes: disruption detection, pricing, the approval rule, carrier responses and purchase orders. The LLM only replaces the *judgement and wording* inside four agents.

Code lives in the workspace packages under `packages/` (see the README's *Code layout*). A module path such as `core/db.py` means `src/oceanbridge/core/db.py` inside the package that owns it.

---

## 1. The switch

| Step | File | Function | What it does |
|---|---|---|---|
| Decide the mode | [config.py](../packages/core/src/oceanbridge/config.py#L113) | `Settings.use_crewai` | Returns true if `llm_mode` is `crewai`, or if it is `auto` and `OPENAI_API_KEY` is set |
| Pick the backend | [runtime/\_\_init\_\_.py](../packages/app/src/oceanbridge/runtime/__init__.py#L14) | `make_backend()` | Returns `CrewAIBackend()` when `use_crewai` is true, otherwise `OfflineBackend()` |
| Route each call | [runtime/backends.py](../packages/app/src/oceanbridge/runtime/backends.py) | `OfflineBackend`, `CrewAIBackend` | Each backend method calls the agent's `run_offline()` or `run_llm()` |
| Pick the embedder | [cache/embeddings.py](../packages/core/src/oceanbridge/cache/embeddings.py#L61) | `make_embedder()` | `OpenAIEmbedder` with a key, `LocalHashEmbedder` without |

---

## 2. Agents: with LLM vs without LLM

| Agent | Called from (flow step) | With LLM | Without LLM | Logic file |
|---|---|---|---|---|
| **Disruption Monitor** | `assess_shipments` in [flow/reroute_flow.py](../packages/app/src/oceanbridge/flow/reroute_flow.py#L98) | `run_llm()`: **gpt-4o-mini** reviews the code-computed risk scores, may check the feeds with its tools, may move each score by at most ±0.15, and writes the situation report and one explanation per shipment | `run_offline()`: keeps the code-computed scores and fills a text template, e.g. *"2 active disruptions detected … 12 of 48 monitored shipments are at risk"* | [agents/disruption_monitor/agent.py](../packages/agents/disruption-monitor/src/oceanbridge/agents/disruption_monitor/agent.py) (`run_llm` L19, `run_offline` L32) · prompt in [agent.yaml](../packages/agents/disruption-monitor/src/oceanbridge/agents/disruption_monitor/agent.yaml) · feed tools in [tools.py](../packages/agents/disruption-monitor/src/oceanbridge/agents/disruption_monitor/tools.py) |
| **Route & Capacity Optimizer** | `optimize_routes`, through `RouteOptimizer.optimize()` in [agents/route_optimizer/optimizer.py](../packages/agents/route-optimizer/src/oceanbridge/agents/route_optimizer/optimizer.py#L76) | `run_llm()`: **gpt-4o-mini**, or **gpt-4o** when the complexity score is 3 or more, ranks the priced options and explains the tradeoff. It must answer with one of the listed option ids | `run_offline()`: picks the option with the **lowest landed-cost score** that has carrier capacity, and `explain_option()` writes the rationale from its numbers | [agents/route_optimizer/agent.py](../packages/agents/route-optimizer/src/oceanbridge/agents/route_optimizer/agent.py) (`explain_option` L22, `run_llm` L38, `run_offline` L52) · prompt in [agent.yaml](../packages/agents/route-optimizer/src/oceanbridge/agents/route_optimizer/agent.yaml) |
| **Vendor Negotiation & PO** | `execute_auto_reroutes`, and `execute_approval` in [runtime/\_\_init\_\_.py](../packages/app/src/oceanbridge/runtime/__init__.py#L20) (which passes the agent to `approve_and_execute` in `hitl/approvals.py`), both through `negotiate_reroute()` in [workflow.py](../packages/agents/vendor-negotiator/src/oceanbridge/agents/vendor_negotiator/workflow.py#L11) | `run_llm()`: **gpt-4o** chooses offers and which counter-terms to accept via the MCP tool `submit_sla_proposal`, calls `issue_purchase_order`, and writes the carrier message | `run_offline()` → `negotiate_deterministic()`: offers 8% below list, meets each counter halfway, accepts the counter in round 3, issues the PO and fills a message template | [agents/vendor_negotiator/agent.py](../packages/agents/vendor-negotiator/src/oceanbridge/agents/vendor_negotiator/agent.py) (`negotiate_deterministic` L25, `run_offline` L56, `run_llm` L61) · prompt in [agent.yaml](../packages/agents/vendor-negotiator/src/oceanbridge/agents/vendor_negotiator/agent.yaml) |
| **Status Analyst** | `GET /shipments/{id}/briefing` in [api/main.py](../packages/app/src/oceanbridge/api/main.py) | `run_llm()`: **gpt-4o-mini** reads `get_shipment_status` over MCP and writes the briefing | `run_offline()`: fills a headline and details template from the shipment data | [agents/status_analyst/agent.py](../packages/agents/status-analyst/src/oceanbridge/agents/status_analyst/agent.py) (`run_llm` L19, `run_offline` L23) · prompt in [agent.yaml](../packages/agents/status-analyst/src/oceanbridge/agents/status_analyst/agent.yaml) |

| **Chat Assistant** | `POST /chat/sessions/{id}/messages` → `ChatService.send()` in [chat/service.py](../packages/app/src/oceanbridge/chat/service.py) | `run_llm()`: OpenAI function calling. **gpt-4o-mini**, or **gpt-4o** for analytical questions, decides which of 16 tools to call (3 on the MCP server), may chain several, then answers | `run_offline()`: `intents.plan()` maps the message to tool calls with patterns and session memory; the same tools run, including the MCP server; replies are templates | [agents/chat_assistant/agent.py](../packages/agents/chat-assistant/src/oceanbridge/agents/chat_assistant/agent.py) · planner in [intents.py](../packages/agents/chat-assistant/src/oceanbridge/agents/chat_assistant/intents.py) · tools in [tools.py](../packages/agents/chat-assistant/src/oceanbridge/agents/chat_assistant/tools.py) · see [chatbot.md](chatbot.md) |

### Shared agent plumbing

| Concern | With LLM | Without LLM | Logic file |
|---|---|---|---|
| Building and running the agent | `run_crew()` builds a CrewAI `Agent` + `Task` from `agent.yaml`, runs it, and returns the structured output and token counts | Not used | [agents/common/crew_runner.py](../packages/agents/common/src/oceanbridge/agents/common/crew_runner.py#L30) |
| Access to the Supply Chain Core | Through the **MCP server** over stdio: `MCPToolbox.get()` starts it and hands the tools to the agent | Direct in-process calls to [core/service.py](../packages/core/src/oceanbridge/core/service.py), the same functions the MCP tools wrap | [agents/common/mcp_tools.py](../packages/agents/common/src/oceanbridge/agents/common/mcp_tools.py#L22) · [mcp_servers/supply_chain_core/server.py](../packages/mcp-servers/src/oceanbridge/mcp_servers/supply_chain_core/server.py) |
| Model label recorded | The model that ran, e.g. `gpt-4o` | `offline-heuristic (routed:gpt-4o)`, i.e. the model it *would* have used | [agents/common/base.py](../packages/agents/common/src/oceanbridge/agents/common/base.py#L39) (`offline_label`) |
| Tokens and cost | Real token counts, cost estimated from `models.pricing` | Always 0 tokens and $0 | [agents/common/context.py](../packages/agents/common/src/oceanbridge/agents/common/context.py#L26) (`RunContext.usage`) |

---

## 3. Cross-cutting features

| Feature | With LLM | Without LLM | Logic file |
|---|---|---|---|
| **Model routing** | `select()` picks the model, and `build_llm()` in `agents/common/crew_runner.py` creates `crewai.LLM(model=...)`, which calls OpenAI | `select()` still runs and records its choice; `build_llm()` is never called | [llm/router.py](../packages/core/src/oceanbridge/llm/router.py) (`select` L73) · [crew_runner.py](../packages/agents/common/src/oceanbridge/agents/common/crew_runner.py#L25) (`build_llm`) |
| **Escalation on a bad answer** | An invalid gpt-4o-mini answer is retried once on gpt-4o, then falls back to `run_offline()` | Not needed: offline output is always valid | [agents/route_optimizer/optimizer.py](../packages/agents/route-optimizer/src/oceanbridge/agents/route_optimizer/optimizer.py#L76) |
| **Fallback if the LLM fails** | Monitoring: `run_offline()` takes over ([flow/reroute_flow.py](../packages/app/src/oceanbridge/flow/reroute_flow.py#L98)). Negotiation: if the LLM does not issue a PO, `negotiate_deterministic()` finishes the step | n/a | [flow/reroute_flow.py](../packages/app/src/oceanbridge/flow/reroute_flow.py) · [agents/vendor_negotiator/agent.py](../packages/agents/vendor-negotiator/src/oceanbridge/agents/vendor_negotiator/agent.py#L61) |
| **Semantic cache embeddings (L2)** | `OpenAIEmbedder` (`text-embedding-3-small`) | `LocalHashEmbedder`: hashed word and character n-grams, no network | [cache/embeddings.py](../packages/core/src/oceanbridge/cache/embeddings.py) (`LocalHashEmbedder` L20, `OpenAIEmbedder` L47) |
| **Semantic cache tiers** | Same in both modes: L1 exact → L3 persistent → L2 semantic + structural guard. A hit skips the LLM entirely | Same | [cache/semantic_cache.py](../packages/core/src/oceanbridge/cache/semantic_cache.py#L123) (`SemanticCache.get`) |

---

## 4. Never an LLM (identical in both modes)

| Logic | What it does | Logic file |
|---|---|---|
| **Disruption detection** | Threshold rules turn weather, AIS, congestion and security feed readings into disruptions | [core/risk.py](../packages/core/src/oceanbridge/core/risk.py) (`detect_at` L32, `detect_all` L70) |
| **Shipment risk scoring** | Combines the disruptions on a route into a risk score, expected delay and a missed-date check | [core/risk.py](../packages/core/src/oceanbridge/core/risk.py#L119) (`assess`) |
| **Simulated feeds & scenarios** | Weather, AIS, port congestion, geopolitics; 5 scenarios | [feeds/](../packages/core/src/oceanbridge/feeds) (`scenarios.py`, `weather.py`, `maritime.py`, `port_congestion.py`, `geopolitics.py`) |
| **Route alternatives** | Generates Suez/Cape, alternate port + truck, rail, air, sea-air and land-bridge options, and scores their landed cost | [core/routing.py](../packages/core/src/oceanbridge/core/routing.py#L55) (`RouteEngine.alternatives`) |
| **Freight tariff** | Cost, transit days and CO₂ per mode, TEU and chargeable-kg rules | [core/freight.py](../packages/core/src/oceanbridge/core/freight.py#L59) (`calculate_freight_cost`) |
| **HITL approval rule** | Auto-execute only if cost increase < 5% **and** cargo value < $250,000 | [hitl/gate.py](../packages/core/src/oceanbridge/hitl/gate.py#L19) (`evaluate`) |
| **PO guard** | Checks the approval, option, amount and HITL rule before any PO | [hitl/guard.py](../packages/core/src/oceanbridge/hitl/guard.py#L24) (`authorize_purchase_order`) |
| **Approval workflow** | Request, approve & execute, reject | [hitl/approvals.py](../packages/core/src/oceanbridge/hitl/approvals.py) (`request_approval` L19, `approve_and_execute` L42, `reject_approval` L50). The negotiation step is passed in by `oceanbridge.runtime.execute_approval`, so core never imports the agents |
| **Carrier negotiation responses** | Simulated carrier: price floor, concession per round, SLA limits, max 3 rounds | [core/service.py](../packages/core/src/oceanbridge/core/service.py#L136) (`submit_sla_proposal`) |
| **Purchase orders** | Reserve capacity, issue the PO, supersede the old PO, reroute the shipment, write the audit log | [core/service.py](../packages/core/src/oceanbridge/core/service.py#L181) (`issue_purchase_order`) |
| **Orchestration** | CrewAI Flow; `@router hitl_gate` is a plain Python function, not an LLM router | [flow/reroute_flow.py](../packages/app/src/oceanbridge/flow/reroute_flow.py#L62) (`RerouteFlow`) |
| **MCP server** | Exposes the Supply Chain Core as 5 tools | [mcp_servers/supply_chain_core/server.py](../packages/mcp-servers/src/oceanbridge/mcp_servers/supply_chain_core/server.py) |
| **Data** | SQLite tables and data access | [core/db.py](../packages/core/src/oceanbridge/core/db.py) · [core/repository.py](../packages/core/src/oceanbridge/core/repository.py) |

---

## 5. What you gain with an LLM

| | With LLM | Without LLM |
|---|---|---|
| Situation report and explanations | Tailored reasoning written for the manager | Templates filled with the same numbers |
| Route choice on close calls | Weighs cost against delivery date, capacity and risk | Always the lowest landed-cost option |
| Negotiation | Adapts offers and terms to each counter | Fixed "8% below list, meet halfway, accept in round 3" script |
| Repeatability | Answers can vary between runs | Same input gives the same output (used by the tests) |
| Cost | Tokens billed per call; see the Metrics tab | $0 |
| Needs | `OPENAI_API_KEY`, internet | Nothing |

## 6. Switching modes

```powershell
# With LLM: put the key in .env at the project root, then restart the API
OPENAI_API_KEY=sk-...

# Force a mode regardless of the key
$env:OB_LLM_MODE = "offline"   # never call an LLM
$env:OB_LLM_MODE = "crewai"    # always use the LLM (fails without a key)
```

`GET /health` reports the active backend (`"backend": "offline"` or `"crewai"`). The default is `llm_mode: auto` in [config/settings.yaml](../config/settings.yaml).

> The offline mode is what the 46 tests use. The LLM mode has been verified to load the MCP tools and build every agent, but it has not been run against OpenAI in this environment because no key is configured.
