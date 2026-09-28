# Database

The OceanBridge rerouting system stores everything in **one SQLite file** on the local machine. There is no database server, account or password, and it is **not deployed** anywhere. Section 7 covers moving to a hosted database.

Code lives in the workspace packages under `packages/` (see the README's *Code layout*). A module path such as `core/db.py` means `src/oceanbridge/core/db.py` inside the package that owns it.

---

## 1. Where it is

| Item | Value |
|---|---|
| Engine | SQLite 3 (built into Python), accessed through SQLAlchemy 2 |
| Default file | `data/oceanbridge.db` under the project root, i.e. `C:\Krishnendu\Workspace\quadlift-capstone-logistic-rerouter\data\oceanbridge.db` |
| Companion files | `oceanbridge.db-wal` (write-ahead log) and `oceanbridge.db-shm` (shared-memory index). SQLite creates them because of WAL mode and merges them back into the main file automatically. Do not delete them while the app is running |
| Created by | The first process that opens it. Tables are created automatically, and `python -m oceanbridge.seed` fills them with demo data |
| In git | No. `.gitignore` excludes `data/*.db` and `data/*.db-*` |

---

## 2. How it is configured

| Setting | Where | Notes |
|---|---|---|
| **Path** | `db_path: data/oceanbridge.db` in [config/settings.yaml](../config/settings.yaml) | A relative path is resolved from the project root |
| **Override** | Environment variable `OB_DB_PATH`, or the same line in `.env` | e.g. `OB_DB_PATH=D:\data\oceanbridge.db`. Wins over `settings.yaml` |
| **Settings class** | [config.py](../packages/core/src/oceanbridge/config.py#L93) `Settings.db_path`, and `db_file` at [L108](../packages/core/src/oceanbridge/config.py#L108) | `db_file` turns the setting into an absolute path |
| **Engine** | [core/db.py](../packages/core/src/oceanbridge/core/db.py#L172) `get_engine()` | `sqlite:///<path>` with `check_same_thread=False` (the API uses threads) and a 30 s lock timeout. One engine per file, cached per process |
| **Connection settings** | [core/db.py](../packages/core/src/oceanbridge/core/db.py#L182) | Every new connection runs `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=30000`, so readers don't block the writer and two processes can share the file |
| **Schema creation** | [core/db.py](../packages/core/src/oceanbridge/core/db.py#L186) `Base.metadata.create_all(engine)` | Creates any missing tables on first connect. There is no migration tool: a changed column needs a reset (section 5) |
| **Sessions** | [core/db.py](../packages/core/src/oceanbridge/core/db.py#L198) `session_scope()` | Commits on success, rolls back on error, always closes |
| **Data access** | [core/repository.py](../packages/core/src/oceanbridge/core/repository.py) | Every read and write goes through these functions, which convert rows to the Pydantic models in `models.py` |
| **Demo data** | [seed.py](../packages/core/src/oceanbridge/seed.py#L57) `seed()` | 48 shipments, 9 carriers and one original PO per shipment. Uses fixed random seed 42, so the data is the same every time |

Two ways to use a different database file:

```powershell
# for this PowerShell window only
$env:OB_DB_PATH = "D:\data\oceanbridge.db"
.\.venv\Scripts\python.exe -m oceanbridge.seed

# or permanently, in .env at the project root
OB_DB_PATH=D:\data\oceanbridge.db
```

---

## 3. Who uses the file

```
Streamlit UI ──HTTP──► FastAPI (api/main.py) ──SQLAlchemy──►┐
                                                           ├──► data/oceanbridge.db  (WAL)
CrewAI agents ──stdio──► MCP server subprocess ──SQLAlchemy─┘
```

| Process | Access | How it finds the file |
|---|---|---|
| **REST API** (uvicorn) | Reads and writes | `OB_DB_PATH` or `settings.yaml` |
| **MCP server** (`python -m oceanbridge.mcp_servers.supply_chain_core`) | Reads and writes | A separate process. When the CrewAI backend starts it, it passes `OB_DB_PATH` explicitly ([agents/common/mcp_tools.py](../packages/agents/common/src/oceanbridge/agents/common/mcp_tools.py)) so both use the same file |
| **Streamlit UI** | None | Only calls the REST API |
| **Seed script** | Deletes all rows, then writes | `OB_DB_PATH` or `settings.yaml` |
| **Tests** | Their own temporary file per test | [conftest.py](../conftest.py) sets `OB_DB_PATH` to a temp directory, so tests never change `data/oceanbridge.db` |

---

## 4. Tables

All 14 tables are defined as SQLAlchemy models in [core/db.py](../packages/core/src/oceanbridge/core/db.py). Columns marked JSON hold nested objects such as route legs and SLA terms.

| Table | Model (line) | Key columns | Written by |
|---|---|---|---|
| `shipments` | `ShipmentRow` ([L29](../packages/core/src/oceanbridge/core/db.py#L29)) | `id` (SHP-1001…), `origin`, `destination`, `mode`, `carrier_id`, `legs` (JSON), `weight_kg`, `volume_cbm`, `cargo_value_usd`, `current_cost_usd`, `departure_date`, `eta`, `required_delivery_date`, `status`, `po_id` | `seed.py`; rerouted by `core/service.py` `issue_purchase_order`; status changed by `hitl/approvals.py` |
| `carriers` | `CarrierRow` ([L20](../packages/core/src/oceanbridge/core/db.py#L20)) | `id` (SEA-OCL…), `name`, `mode`, `capacity_remaining`, `capacity_unit` (TEU or kg) | `seed.py`; capacity reduced by `issue_purchase_order` |
| `purchase_orders` | `PurchaseOrderRow` ([L65](../packages/core/src/oceanbridge/core/db.py#L65)) | `po_id`, `shipment_id`, `carrier_id`, `amount_usd`, `route_label`, `legs` (JSON), `sla` (JSON), `approval_id`, `supersedes_po_id`, `status` (issued / superseded), `auto_executed`, `created_at` | `seed.py` (the `-ORIG` POs); `issue_purchase_order` |
| `approvals` | `ApprovalRow` ([L81](../packages/core/src/oceanbridge/core/db.py#L81)) | `id` (APR-…), `run_id`, `shipment_id`, `payload` (JSON: option, alternatives, HITL decision, rationale), `status` (pending / approved / rejected / executed), `approver`, `comment`, `created_at`, `decided_at` | `hitl/approvals.py`; set to executed by `hitl/guard.py` |
| `disruptions` | `DisruptionRow` ([L50](../packages/core/src/oceanbridge/core/db.py#L50)) | `id` (DSR-type-location), `type`, `location`, `severity`, `expected_delay_days`, `start_date`, `end_date`, `description`, `source`, `active` | `flow/reroute_flow.py` `detect_disruptions` on every run |
| `route_proposals` | `ProposalRow` ([L141](../packages/core/src/oceanbridge/core/db.py#L141)) | `shipment_id`, `options` (JSON list of priced options), `created_at` | `core/service.py` `list_route_alternatives`. Read by `submit_sla_proposal` and `issue_purchase_order` |
| `negotiations` | `NegotiationRow` ([L106](../packages/core/src/oceanbridge/core/db.py#L106)) | `id`, `shipment_id`, `carrier_id`, `round`, `offer` (JSON), `response` (JSON), `created_at` | `core/service.py` `submit_sla_proposal`. Also enforces the 3-round limit |
| `audit_log` | `AuditRow` ([L117](../packages/core/src/oceanbridge/core/db.py#L117)) | `id`, `ts`, `actor`, `action` (po_issued, po_refused, approval_requested, approval_granted, approval_rejected), `subject`, `detail` (JSON) | `core/service.py`, `hitl/guard.py`, `hitl/approvals.py` |
| `runs` | `RunRow` ([L94](../packages/core/src/oceanbridge/core/db.py#L94)) | `id` (RUN-…), `status`, `backend`, `started_at`, `finished_at`, `summary` (JSON), `trace` (JSON list of agent steps), `error` | `flow/reroute_flow.py` `run_pipeline`; `api/main.py` `POST /pipeline/run` |
| `cache_entries` | `CacheRow` ([L127](../packages/core/src/oceanbridge/core/db.py#L127)) | `key`, `namespace` (freight / route_decisions), `text`, `embedding` (bytes), `payload` (JSON), `guard` (JSON), `touchpoints` (JSON), `created_at`, `expires_at`, `hits` | `cache/semantic_cache.py`. This is the **L3 persistent** cache tier |
| `llm_usage` | `LlmUsageRow` ([L155](../packages/core/src/oceanbridge/core/db.py#L155)) | `id`, `ts`, `run_id`, `task_type`, `model`, `reason`, `prompt_tokens`, `completion_tokens`, `cost_usd`, `cache_hit` | `agents/common/context.py` `RunContext.usage`, once per agent call or cache hit |
| `chat_sessions` | `ChatSessionRow` | `id` (CHAT-…), `user`, `title`, `status` (active / ended / expired), `summary`, `state` (JSON working memory), `message_count`, token counts, `created_at`, `updated_at`, `ended_at`, `end_reason` | `chat/sessions.py`, `chat/service.py`. See [chatbot.md](chatbot.md) |
| `chat_messages` | `ChatMessageRow` | `id`, `session_id`, `role`, `content`, `tool_calls` (JSON), `model`, `created_at` | `chat/service.py` |
| `active_scenarios` | `ScenarioRow` ([L149](../packages/core/src/oceanbridge/core/db.py#L149)) | `name`, `activated_at` | `feeds/scenarios.py` `activate` / `deactivate` |

---

## 5. Common tasks

| Task | How |
|---|---|
| **Create and seed** | `.\.venv\Scripts\python.exe -m oceanbridge.seed` |
| **Reset to demo data** | UI sidebar **Reset demo data** or `POST /admin/seed`: deletes all rows in every table (including scenarios and the L3 cache), reseeds, and also clears the running API's in-memory cache tiers. Running the seed command from a terminal does the same to the file, but a running API keeps its in-memory cache until it is restarted |
| **Start from an empty file** | Stop the API and any MCP server, delete `data\oceanbridge.db*`, then run the seed command. Use this after changing a table definition |
| **Back up** | Stop the API first, then copy all three `oceanbridge.db*` files together. Copying only the `.db` file while the app runs can miss recent writes that are still in the `-wal` file |
| **Inspect** | Open the file in *DB Browser for SQLite* or the VS Code *SQLite Viewer* extension, or run the Python snippet below |

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/oceanbridge.db'); print(c.execute('select id, status, eta from shipments limit 5').fetchall())"
```

```text
Shipments by status:   select status, count(*) from shipments group by status;
Pending approvals:     select id, shipment_id, created_at from approvals where status = 'pending';
Reroute POs:           select po_id, shipment_id, amount_usd, auto_executed from purchase_orders where po_id not like '%-ORIG';
Refused POs:           select ts, subject, detail from audit_log where action = 'po_refused';
Model usage:           select model, count(*), sum(cost_usd) from llm_usage group by model;
```

---

## 6. Limitations of the current setup

| Limitation | Effect |
|---|---|
| Single file on one machine | Every process must run on the same computer and see the same path |
| One writer at a time | WAL lets readers work during a write, but writes queue up. The 30 s busy timeout covers a demo, not heavy load |
| No migrations | A changed table definition needs a reset; existing data is lost |
| No authentication or encryption | Anyone who can read the file can read the data |
| In-process cache tiers | L1 and L2 cache tiers are per process; only L3 (`cache_entries`) is shared through the file |

This is fine for the capstone demo and the tests. It is not suitable for production.

---

## 7. Moving to a hosted database

The usual next step is **PostgreSQL**, for example managed on Azure, AWS or Supabase. Because all access goes through SQLAlchemy and `core/repository.py`, only the connection layer changes:

1. **Configurable URL.** Add a `database_url` setting (e.g. `OB_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/oceanbridge`) and use it in `get_engine()` in `core/db.py` instead of building `sqlite:///<path>`.
2. **Drop SQLite-only options.** `check_same_thread`, the `timeout` argument and the two `PRAGMA` lines apply only to SQLite.
3. **Migrations.** Replace `create_all()` with Alembic migrations so schema changes keep existing data.
4. **Secrets.** Keep the database password in a secret store or the hosting platform's environment settings, not in `.env` or in git.
5. **Driver.** Add `psycopg[binary]` to `pyproject.toml`.

None of these changes are implemented yet.
