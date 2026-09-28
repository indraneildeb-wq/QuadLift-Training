# Chat Assistant

A chatbot in the Control Tower that answers questions and carries out requests by **calling tools on its own**: the Supply Chain Core **MCP server** plus local functions of the application. It keeps **memory** within a session, and sessions can be **invalidated** and replaced by a fresh one with empty memory.

Code lives in the workspace packages under `packages/` (see the README's *Code layout*). A module path such as `core/db.py` means `src/oceanbridge/core/db.py` inside the package that owns it.

---

## 1. What it can do

| You say | What the assistant does | Tools called |
|---|---|---|
| "Status of SHP-1004" | Looks up the shipment | `get_shipment_status` (MCP) |
| "Show its alternatives" | Resolves "its" to SHP-1004 from memory, lists priced reroutes | `list_route_alternatives` (MCP) |
| "Quote sea freight Shanghai to Rotterdam 18000 kg 60 cbm" | Prices the leg | `calculate_freight_cost` (MCP) |
| "Which shipments are at risk?" · "What disruptions are active?" | Lists them | `list_shipments`, `list_disruptions` |
| "Activate the Rotterdam strike and run the pipeline" | Switches on the scenario and runs the full agent pipeline | `run_rerouting_pipeline` |
| "Show pending approvals" · "Tell me about APR-…" | Lists or explains approval requests | `list_approvals`, `get_approval` |
| "Approve APR-…" → "confirm" | Proposes the decision; executes it only after you confirm | `decide_approval`, then the approval workflow |
| "What if SHP-1011 weighed 7.5 t?" | Dry-run optimisation; shows cache tier and whether it would auto-execute | `what_if_optimize` |
| "What is the HITL policy?" · "Cache metrics" | Explains thresholds, shows cache and model usage | `get_hitl_policy`, `get_system_metrics` |

One message can trigger several tools. For example, "status and alternatives for SHP-1004" calls both, and the LLM version can chain calls, using one tool's result to choose the next.

---

## 2. Where it lives

```
packages/agents/chat-assistant/        # package oceanbridge-agent-chat-assistant (the agent)
└── src/oceanbridge/agents/chat_assistant/
    ├── agent.yaml                     #   role + system prompt
    ├── tools.py                       #   tool registry: 3 MCP-backed + 13 local tools, each read / write / confirm
    ├── agent.py                       #   run_llm (OpenAI function-calling loop) · run_offline (planner + templates)
    ├── intents.py                     #   offline planner: message -> tool calls, using session memory
    └── memory.py                      #   history window, running summary, working memory (entities, pending action)
packages/app/                          # package oceanbridge-app (the feature around the agent)
└── src/oceanbridge/
    ├── chat/service.py                #   one chat turn: session check, confirmation, agent call, memory, usage
    ├── chat/sessions.py               #   create / resume / invalidate / expire sessions
    ├── chat/store.py                  #   SQLite persistence
    └── api/chat_routes.py             #   REST endpoints under /chat
packages/ui/src/oceanbridge/ui/app.py  # package oceanbridge-ui: "💬 Assistant" tab and the floating 💬 button
```

The app passes the pipeline and what-if functions to the agent's tools (`ToolContext.run_pipeline`, `ToolContext.optimize_what_if`), so the agent package does not depend on the app.

| Piece | File | Main functions |
|---|---|---|
| Turn orchestration | [chat/service.py](../packages/app/src/oceanbridge/chat/service.py) | `ChatService.send()` (L64), `_agent_turn()` (L106), `_execute_pending()` (L123), `_maintain_summary()` (L143) |
| Sessions | [chat/sessions.py](../packages/app/src/oceanbridge/chat/sessions.py) | `new_session()` (L40), `require_active()` (L57), `end_session()` (L64), `_expire_if_idle()` (L30) |
| Memory | [agents/chat_assistant/memory.py](../packages/agents/chat-assistant/src/oceanbridge/agents/chat_assistant/memory.py) | `remember()` (L43), `remember_tool_call()` (L57), `context_block()` (L73), `summarize_offline()` (L91) |
| Persistence | [chat/store.py](../packages/app/src/oceanbridge/chat/store.py) | `insert_session`, `add_message`, `get_messages`, `update_session`, `delete_session` |
| LLM agent loop | [agents/chat_assistant/agent.py](../packages/agents/chat-assistant/src/oceanbridge/agents/chat_assistant/agent.py#L59) | `run_llm()`, `summarize_llm()` (L101) |
| Offline agent | [agents/chat_assistant/agent.py](../packages/agents/chat-assistant/src/oceanbridge/agents/chat_assistant/agent.py#L226) · [intents.py](../packages/agents/chat-assistant/src/oceanbridge/agents/chat_assistant/intents.py#L120) | `run_offline()`, `plan()` |
| Tools | [agents/chat_assistant/tools.py](../packages/agents/chat-assistant/src/oceanbridge/agents/chat_assistant/tools.py#L299) | `TOOLS`, `call_tool()` (L365), `catalog()` (L383) |
| API | [api/chat_routes.py](../packages/app/src/oceanbridge/api/chat_routes.py) | see section 7 |

---

## 3. How a turn works

```
Assistant tab ──POST /chat/sessions/{id}/messages──► ChatService.send()
   1. session must be active ............................ sessions.require_active()  (else HTTP 409)
   2. store the user message, pick up ids it mentions ... memory.remember()
   3. waiting for a confirmation?
        "confirm" / "yes" (whole message) ─► execute approve/reject in code ── hitl.approvals
        "cancel" / "no"                    ─► drop it
        anything else                      ─► drop it, then continue with step 4
   4. choose the model ................................... ModelRouter.select(TaskType.CHAT)
      run the agent:
        with an LLM ─► run_llm():  model ⇄ tools loop (≤ max_tool_steps rounds) ─► answer
        without     ─► run_offline(): plan() ─► tools ─► templated answer
        LLM error   ─► run_offline() answers instead (the reply says so)
   5. store the reply + every tool call, update memory, tokens, summary, title
```

### The LLM loop (`run_llm`)

1. The system prompt (from `agent.yaml`) is built with the user's name, the HITL rule and the **session memory** block.
2. The last `history_messages` messages are replayed, then the new message is added.
3. The model is called with all 16 tool schemas (`tool_choice="auto"`).
4. If it asks for tools, each one runs through `call_tool()` and its result (trimmed to 6,000 characters) goes back to the model. Tool arguments also update working memory.
5. This repeats until the model answers in text, or `max_tool_steps` rounds are used. The last round is forced to answer with no tools.

### Model routing

| Message | Model | Why |
|---|---|---|
| Lookups and actions ("status of…", "run the pipeline") | `gpt-4o-mini` | `TaskType.CHAT` is light by default |
| Analytical ("why…", "compare…", "recommend…", "explain…", "pros and cons") | `gpt-4o` | `is_analytical()` ([agent.py L43](../packages/agents/chat-assistant/src/oceanbridge/agents/chat_assistant/agent.py#L43)) sets `analytical=True` |

Each turn is written to `llm_usage` with the model, routing reason, tokens and estimated cost, so it appears on the Metrics tab.

### Without an LLM

With no `OPENAI_API_KEY`, `intents.plan()` maps the message to tool calls using patterns (ids, port names, scenarios, weights, values, "what if", "approve"…). It uses working memory for "it", "its" and "that shipment". The replies are templates filled from the tool results. The same tools run, **including the real MCP server**. See [llm-vs-offline.md](llm-vs-offline.md) for the general pattern.

---

## 4. Tools

The assistant sees 16 tools. **It is never given `submit_sla_proposal` or `issue_purchase_order`**: money is only committed by the pipeline or by a confirmed approval, and both go through the HITL gate.

| Tool | Source | Effect | Purpose |
|---|---|---|---|
| `get_shipment_status` | **MCP** | read | Status, route, ETA, risk, POs, pending approval |
| `list_route_alternatives` | **MCP** | write* | Priced reroute options (*saves them as the current proposal) |
| `calculate_freight_cost` | **MCP** | read | Tariff quote for one leg |
| `list_shipments` | local | read | At-risk / all / by status / by lane |
| `list_disruptions` | local | read | Active disruptions |
| `list_scenarios` | local | read | Scenarios and which are active |
| `activate_scenario` · `deactivate_scenario` | local | write | Switch a scenario on or off |
| `run_rerouting_pipeline` | local | write | Full pipeline; the HITL gate decides what executes |
| `list_approvals` · `get_approval` | local | read | Approval queue and details |
| `decide_approval` | local | **confirm** | Proposes approve/reject; nothing runs until the user confirms |
| `what_if_optimize` | local | write* | Dry-run optimisation (*writes to the cache only) |
| `get_hitl_policy` | local | read | Approval thresholds |
| `get_system_metrics` | local | read | Cache hit rates, model usage, LLM cost, approvals, POs |
| `list_purchase_orders` | local | read | Reroute POs, or all POs of a shipment |

**How MCP tools are called:** `ChatService.toolbox()` starts one `MCPToolbox` per API process on the first MCP call. That starts the Supply Chain Core server over stdio, and it is reused for later turns. Calls are serialised with a lock. The server is stopped when the API shuts down (the `lifespan` hook in `api/main.py`).

Every call is recorded as a `ToolCallRecord` (tool, source, effect, arguments, ok, one-line result, duration). It's stored with the assistant message and shown under **🔧 tool calls** in the UI.

---

## 5. Memory

Memory is per session. A new session starts empty, and invalidating a session wipes its working memory.

| Layer | What it holds | Where | Used for |
|---|---|---|---|
| **Short-term** | The last `history_messages` (12) user and assistant messages | `chat_messages` table | Replayed word for word to the LLM each turn |
| **Summary** | Older messages folded into a running summary once the session has `summarize_after_messages` (24) or more | `chat_sessions.summary` | Added to the system prompt. Written by `gpt-4o-mini` with a key, by `summarize_offline()` without |
| **Working memory** | `last_shipment_id`, `recent_shipments` (5), `last_approval_id`, `recent_approvals`, `last_scenario`, `last_po_id`, and `pending_action` | `chat_sessions.state` (JSON) | Resolving "it / its / that shipment / approve it", and the confirmation step |

Working memory is updated from the ids in each user message (`remember()`) and from each tool's arguments and results (`remember_tool_call()`). The LLM sees it as the "Session memory" block in its system prompt (`context_block()`).

---

## 6. Sessions

| State | Meaning | Accepts messages | Memory |
|---|---|---|---|
| `active` | Normal | Yes | Kept |
| `ended` | Invalidated by the user, or replaced by a new session | No (HTTP 409) | Wiped; history stays readable |
| `expired` | Idle longer than `session_idle_timeout_minutes` (60), checked on next access | No (HTTP 409) | Wiped; history stays readable |

**Invalidate and start fresh.** Do either of these:
- In the UI: click **➕ New chat**, which ends the current session and opens a new one, or **⏹ End session**.
- Through the API: `POST /chat/sessions` with `{"invalidate_session_id": "<current id>"}` ends the old session and creates the new one in one call. `POST /chat/sessions/{id}/invalidate` only ends it.

Closed sessions can be reopened read-only from **Previous sessions**. `DELETE /chat/sessions/{id}` removes a session and its history.

Approvals confirmed in chat are recorded under the session's `user`, which the UI takes from the approver name field.

---

## 7. REST API

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/chat/sessions` | Start a session. Body: `{"user": "...", "invalidate_session_id": "..."}` (both optional) |
| GET | `/chat/sessions?include_closed=true` | List sessions, newest first |
| GET | `/chat/sessions/{id}` | Session details + full message history with tool calls |
| POST | `/chat/sessions/{id}/messages` | Send a message. Body: `{"message": "..."}`. Returns the reply, tool calls, model, mode, pending action and the updated session. 409 if the session is closed |
| POST | `/chat/sessions/{id}/invalidate` | End the session (read-only, memory wiped) |
| DELETE | `/chat/sessions/{id}` | Delete the session and its messages |
| GET | `/chat/tools` | Tool catalogue with source and effect |

```powershell
$base = "http://127.0.0.1:8000"
$s = Invoke-RestMethod -Method Post "$base/chat/sessions" -ContentType "application/json" -Body '{"user":"Jane Doe"}'
$say = { param($m) Invoke-RestMethod -Method Post "$base/chat/sessions/$($s.id)/messages" -ContentType "application/json" -Body (@{message=$m} | ConvertTo-Json) }
(& $say "status of SHP-1004").reply
(& $say "show its alternatives").tool_calls | Select-Object tool, source, ok, summary
$new = Invoke-RestMethod -Method Post "$base/chat/sessions" -ContentType "application/json" -Body (@{invalidate_session_id=$s.id} | ConvertTo-Json)
```

---

## 8. Storage

Two tables in the main SQLite database, both defined in [core/db.py](../packages/core/src/oceanbridge/core/db.py) (see also [database.md](database.md)):

| Table | Key columns |
|---|---|
| `chat_sessions` | `id` (CHAT-…), `user`, `title` (first message), `status`, `summary`, `state` (JSON working memory), `message_count`, `prompt_tokens`, `completion_tokens`, `created_at`, `updated_at`, `ended_at`, `end_reason` |
| `chat_messages` | `id`, `session_id`, `role` (user / assistant), `content`, `tool_calls` (JSON list of `ToolCallRecord`), `model`, `created_at` |

---

## 9. Configuration

`chat:` section of [config/settings.yaml](../config/settings.yaml), overridable with `OB_CHAT__<KEY>` environment variables. Defaults are in `ChatSettings` in [config.py](../packages/core/src/oceanbridge/config.py).

| Setting | Default | Controls |
|---|---|---|
| `history_messages` | 12 | Messages replayed to the LLM each turn |
| `summarize_after_messages` | 24 | Session length at which older messages start being summarised |
| `session_idle_timeout_minutes` | 60 | Idle time before a session expires |
| `max_tool_steps` | 6 | Tool-calling rounds per turn before the model must answer |
| `default_user` | Logistics Operations Manager | User recorded when a session is started without a name |

The models come from `models.light` and `models.heavy`, the same as the other agents.

---

## 10. Safety rules

| Rule | Enforced in |
|---|---|
| No tool can issue a PO or negotiate directly | `TOOLS` in `agents/chat_assistant/tools.py` (not registered) |
| Approve/reject needs an explicit confirmation, and the **whole** message must be a confirmation ("confirm", "yes", "go ahead"…). "ok, what about SHP-1004?" does not count | `CONFIRM` / `CANCEL` in `chat/service.py` (L32) |
| A new request drops an unconfirmed proposal, so a later "yes" can't trigger it | `ChatService.send()` |
| Confirmations are executed by code, not by the model | `ChatService._execute_pending()` |
| Pipeline runs and approvals still pass the HITL gate and the PO guard | `hitl/gate.py`, `hitl/guard.py` |
| Closed sessions accept no messages | `sessions.require_active()` |

---

## 11. Tests

`packages/app/tests/test_chat.py` (9 tests): session lifecycle and invalidation, idle expiry, MCP tool calls with pronoun memory, the confirmation safety net, the LLM loop and model routing with a fake OpenAI client, fallback to offline when the LLM fails, summarisation of long sessions, and the REST API.

```powershell
.\.venv\Scripts\python.exe -m pytest packages\app\tests\test_chat.py -v
```

> The LLM path is tested with a fake OpenAI client that returns scripted tool calls. It has not been run against the real OpenAI API in this environment, because no key is configured.
