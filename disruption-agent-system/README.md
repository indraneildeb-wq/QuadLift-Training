# Disruption Monitoring Agent System

Detects, correlates, scores, and publishes supply-chain disruption intelligence
for Oceanbridge Logistics. Three independent processes talk to each other only
over REST/JSON, each validated against shared schemas.

```
Test Harness  --POST RawEvent--> Disruption Agent  --POST published report--> Downstream Listener
 (Terminal 3)  <--snapshot-------  (Terminal 2)                                 (Terminal 1)
```

## Modules

| Path | What it is |
|---|---|
| `schemas/` | The three JSON Schemas every boundary in the system validates against: `raw_event.schema.json` (adapter/Harness input), `disruption_intelligence_report.schema.json` (the published report, incl. the embedded `DisruptionSignal` definition), `get_shipment_status_response.schema.json` (MCP tool response). |
| `config/` | One YAML config per process (`agent_config.yaml`, `listener_config.yaml`, `harness_config.yaml`), plus `model_pricing.yaml` (LLM cost table) and `trusted_news_domains.txt` (real news-adapter allow-list, not used by manual test input). |
| `fixtures/shipments.json` | Sample shipment data backing the mock MCP client — the `get_shipment_status` tool's fake dataset. |
| `scenarios/busan_typhoon.yaml` | A scripted 3-signal test scenario for the Harness's `--script` mode. |
| `listener/` | **Downstream Listener** — standalone stand-in for the real Route & Capacity Optimization Agent's ingress. One endpoint (`POST /downstream/reports`), validates against the report schema, prints/logs what it receives. |
| `agent/` | **Disruption Monitoring Agent** — the actual business logic. See breakdown below. |
| `harness/` | **Test Harness** — interactive or scripted REST client that feeds natural-language disruption scenarios into the Agent and prints the resulting report snapshot after each input. |
| `logs/` | Runtime output: `audit_log.jsonl` (every LLM call / cache request / downstream call / schema violation, one JSON line each), process stdout captures, Harness session transcripts. |
| `quarantine/` | Payloads rejected at any schema-validation boundary land here as individual JSON files, keyed by `audit_id`. |
| `docker-compose.yml` | Spins up Redis (state store + semantic cache backend). |

### Inside `agent/agent_core/`

| File | Responsibility |
|---|---|
| `schemas.py` | Loads the shared JSON Schemas once, exposes `validate(payload, boundary)` — the single reject/raise point used everywhere. |
| `normalization.py` | RawEvent → DisruptionSignal: calls the LLM client (with semantic-cache lookup first), model-routing escalation, full audit/metrics instrumentation. |
| `llm_client.py` | `MockLLMClient` (deterministic keyword/gazetteer extraction, no API key needed) and `RealLLMClient` (OpenAI, structured JSON output, prompt-injection-resistant system prompt). |
| `mcp_client.py` | `MockMCPClient` (filters `fixtures/shipments.json` in-memory) and `RealMCPClient` (actual MCP protocol: SSE transport, `ClientSession` handshake, `call_tool`). Same `get_shipment_status(shipment_id, filter)` interface either way. |
| `fusion.py` | Correlates a new signal into an existing open report (by port/region) or opens a new one; recomputes severity, geography, impact assessment (via MCP), and confidence. |
| `scoring.py` | The confidence-scoring algorithm: Bayesian log-odds accumulation with source-reliability weighting, severity weighting, diversity dampening (repeated same-type signals count less), and time decay. |
| `trigger.py` | Publishes immediately when confidence crosses threshold; a background reconciliation loop also publishes on debounce/max-hold timer expiry, and recomputes decay on idle reports. |
| `publisher.py` | POSTs the final report downstream with retry/backoff — never retries a `422` (schema rejection), since retrying a malformed payload can't fix it. |
| `state_store.py` / `cache.py` | Redis-backed open-report store and two-tier semantic cache (separate Redis DB indices so a cache flush can't touch in-flight report state). |
| `audit.py` / `metrics.py` / `pricing.py` / `quarantine.py` | Observability plumbing: append-only audit log, Prometheus-style counters (`/metrics`), USD cost calculation from token counts, and the rejected-payload store. |

## Prerequisites
- Python 3.10+
- Docker Desktop (for Redis) — or point `config/agent_config.yaml`'s `state_store`/`semantic_cache` at your own Redis instance

## Setup

```bash
cd disruption-agent-system
docker compose up -d
python -m pip install -r listener/requirements.txt -r agent/requirements.txt -r harness/requirements.txt
```

(Use `python -m pip` rather than bare `pip` if `pip` isn't on your `PATH`.)

## Running it — one terminal each

```bash
# Terminal 1
python listener/listener_main.py

# Terminal 2 -- mock MCP + mock LLM, no external services or API keys needed
python agent/agent_main.py --mock-mcp fixtures/shipments.json --mock-llm

# Terminal 3
python harness/harness_main.py
```

Health checks: `curl http://127.0.0.1:8000/health` (Agent), `curl http://127.0.0.1:8090/health` (Listener) — both should report `{"status": "ok", ...}`.

## Testing

- **Interactive**: with Terminal 3 running, type natural-language input at each prompt (weather / port status / vessel tracking / news feed); blank skips a category. Watch the snapshot printed after each turn, and watch **Terminal 1** for a `PUBLISHED REPORT` block once confidence crosses threshold (default `0.85`) or a debounce timer expires.
- **Scripted / repeatable**: `python harness/harness_main.py --script scenarios/busan_typhoon.yaml` — replays a fixed scenario, useful for regression-testing after any change to scoring or thresholds.
- **Reset state between runs**: reports persist in Redis by correlation key, so re-running a scenario keeps accumulating rather than starting fresh. Clear it with `docker compose exec redis redis-cli FLUSHALL` (or `docker compose restart redis`).

## Modes

| Flag | Effect |
|---|---|
| `--mock-mcp <fixture_file>` | Forces `MockMCPClient`, ignoring `config/agent_config.yaml`'s `mcp.mode`. Default fixture: `fixtures/shipments.json`. |
| `--mock-llm` | Forces `MockLLMClient` (no API key, no cost, deterministic). |
| *(neither flag)* | Uses whatever `agent_config.yaml` specifies under `mcp.mode` / `llm.mode` — `real` requires filling in `mcp.real.endpoint`/`api_key` and `llm.real.api_key_env_var` respectively. |

## Observability

- `curl http://127.0.0.1:8000/metrics` — LLM call/token/cost counters, cache hit rate, downstream call outcomes, schema-violation counts.
- `logs/audit_log.jsonl` — one line per LLM call, cache request, downstream call, or schema violation, each schema-versioned and correlatable by `report_id`/`raw_event_id`.
- `quarantine/*.json` — the raw payload behind any rejected schema violation, named by its `audit_id` from the audit log.

## Related documents
- [`docs/mcp_get_shipment_status_interface.md`](docs/mcp_get_shipment_status_interface.md) — the interface contract for whoever implements the real `get_shipment_status` MCP server.
- [`docs/route_capacity_agent_interface.md`](docs/route_capacity_agent_interface.md) — the interface contract for whoever consumes published reports on the Route & Capacity Optimization Agent side.
