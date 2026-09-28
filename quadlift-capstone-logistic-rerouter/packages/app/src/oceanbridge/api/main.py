"""FastAPI backend for the OceanBridge rerouting system.

Run:  uvicorn oceanbridge.api.main:app --reload
"""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from oceanbridge.agents.common.context import RunContext
from oceanbridge.agents.route_optimizer import ROUTE_CACHE_NS
from oceanbridge.api.chat_routes import router as chat_router
from oceanbridge.cache.semantic_cache import all_caches, get_cache
from oceanbridge.chat.service import reset_chat_service
from oceanbridge.config import get_settings
from oceanbridge.core import repository as repo
from oceanbridge.core import service
from oceanbridge.core.geo import ALL_LOCATIONS
from oceanbridge.core.risk import assess
from oceanbridge.feeds import scenarios
from oceanbridge.flow.reroute_flow import optimize_what_if, run_pipeline
from oceanbridge.hitl import PolicyViolation
from oceanbridge.runtime import execute_approval, make_backend, reject_approval
from oceanbridge.llm.router import TaskType
from oceanbridge.models import ApprovalStatus
from oceanbridge.seed import seed

@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    reset_chat_service()  # stops the chat assistant's MCP server subprocess


app = FastAPI(title="OceanBridge Logistics - Disruption & Autonomous Rerouting API", version="0.1.0",
              lifespan=lifespan)
app.include_router(chat_router)
_run_lock = threading.Lock()


class RunRequest(BaseModel):
    scenario: str | None = Field(default=None, description="Optionally activate a scenario before running")


class DecisionRequest(BaseModel):
    approver: str = Field(description="Name of the Logistics Operations Manager")
    comment: str | None = None


class WhatIfRequest(BaseModel):
    shipment_id: str
    weight_kg: float | None = None
    volume_cbm: float | None = None
    cargo_value_usd: float | None = None
    required_delivery_date: date | None = None


def _404(what: str):
    raise HTTPException(status_code=404, detail=f"{what} not found")


# ------------------------------------------------------------------ system
@app.get("/health")
def health():
    s = get_settings()
    return {"status": "ok", "backend": "crewai" if s.use_crewai else "offline", "db": str(s.db_file)}


@app.get("/config")
def config():
    s = get_settings()
    return {"hitl": s.hitl.model_dump(), "models": s.models.model_dump(), "cache": s.cache.model_dump(),
            "backend": "crewai" if s.use_crewai else "offline"}


@app.post("/admin/seed")
def reseed():
    scenarios.clear_all()
    for c in all_caches().values():
        c.clear()
    return seed(reset=True)


# ------------------------------------------------------------------ scenarios & disruptions
@app.get("/scenarios")
def list_scenarios():
    active = {s.name for s in scenarios.active()}
    return [{"name": s.name, "title": s.title, "description": s.description, "active": s.name in active}
            for s in scenarios.SCENARIOS.values()]


@app.post("/scenarios/{name}/activate")
def activate_scenario(name: str):
    try:
        sc = scenarios.activate(name)
    except KeyError:
        _404("Scenario")
    return {"activated": sc.name}


@app.post("/scenarios/{name}/deactivate")
def deactivate_scenario(name: str):
    scenarios.deactivate(name)
    return {"deactivated": name}


@app.post("/scenarios/clear")
def clear_scenarios():
    scenarios.clear_all()
    return {"cleared": True}


@app.get("/disruptions")
def disruptions():
    out = []
    for d in repo.active_disruptions():
        loc = ALL_LOCATIONS.get(d.location)
        out.append({**d.model_dump(mode="json"), "lat": loc.lat if loc else None, "lon": loc.lon if loc else None,
                    "location_name": loc.name if loc else d.location})
    return out


# ------------------------------------------------------------------ pipeline
def _run_in_background(run_id: str, scenario: str | None):
    with _run_lock:
        try:
            run_pipeline(scenario=scenario, run_id=run_id)
        except Exception:
            pass  # failure is recorded on the run row


@app.post("/pipeline/run")
def trigger_run(req: RunRequest, background: BackgroundTasks):
    if req.scenario and req.scenario not in scenarios.SCENARIOS:
        _404("Scenario")
    run_id = f"RUN-{date.today():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
    repo.create_run(run_id, "crewai" if get_settings().use_crewai else "offline")
    background.add_task(_run_in_background, run_id, req.scenario)
    return {"run_id": run_id, "status": "running"}


@app.post("/pipeline/run-sync")
def run_sync(req: RunRequest):
    """Run the pipeline and wait for the result (handy for demos and scripts)."""
    if req.scenario and req.scenario not in scenarios.SCENARIOS:
        _404("Scenario")
    with _run_lock:
        return run_pipeline(scenario=req.scenario)


@app.get("/runs")
def runs(limit: int = 20):
    return repo.list_runs(limit)


@app.get("/runs/{run_id}")
def run(run_id: str):
    r = repo.get_run(run_id)
    return r if r else _404("Run")


# ------------------------------------------------------------------ shipments
@app.get("/shipments")
def shipments():
    active = repo.active_disruptions()
    out = []
    for s in repo.list_shipments():
        a = assess(s, active)
        out.append({**s.model_dump(mode="json"), "risk_score": a.risk_score, "at_risk": a.at_risk,
                    "expected_delay_days": a.expected_delay_days})
    return out


@app.get("/shipments/{shipment_id}")
def shipment_status(shipment_id: str):
    try:
        return service.get_shipment_status(shipment_id)
    except KeyError:
        _404("Shipment")


@app.get("/shipments/{shipment_id}/briefing")
def shipment_briefing(shipment_id: str):
    """Status check agent - routed to the light model (gpt-4o-mini)."""
    if repo.get_shipment(shipment_id) is None:
        _404("Shipment")
    backend = make_backend()
    ctx = RunContext(run_id=f"STATUS-{uuid.uuid4().hex[:6].upper()}", backend=backend.name)
    decision = ctx.router.select(TaskType.STATUS_CHECK)
    try:
        call = backend.briefing(ctx, shipment_id, decision)
    finally:
        backend.close()
    ctx.usage(decision, call.prompt_tokens, call.completion_tokens, executed_model=call.model)
    return {**call.output.model_dump(), "model": call.model, "routing": decision.reason}


@app.get("/shipments/{shipment_id}/alternatives")
def shipment_alternatives(shipment_id: str):
    try:
        return [o.model_dump(mode="json") for o in service.list_route_alternatives(shipment_id)]
    except KeyError:
        _404("Shipment")


@app.post("/optimize/what-if")
def what_if(req: WhatIfRequest):
    overrides = req.model_dump(exclude={"shipment_id"}, exclude_none=True)
    try:
        return optimize_what_if(req.shipment_id, overrides or None)
    except KeyError:
        _404("Shipment")


# ------------------------------------------------------------------ approvals (HITL)
@app.get("/approvals")
def approvals(status: ApprovalStatus | None = None):
    return [a.model_dump(mode="json") for a in repo.list_approvals(status)]


@app.get("/approvals/{approval_id}")
def approval(approval_id: str):
    a = repo.get_approval(approval_id)
    return a.model_dump(mode="json") if a else _404("Approval")


@app.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, req: DecisionRequest):
    try:
        return execute_approval(approval_id, req.approver, req.comment)
    except KeyError:
        _404("Approval")
    except (ValueError, PolicyViolation) as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/approvals/{approval_id}/reject")
def reject(approval_id: str, req: DecisionRequest):
    try:
        return reject_approval(approval_id, req.approver, req.comment)
    except KeyError:
        _404("Approval")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


# ------------------------------------------------------------------ POs, audit, metrics
@app.get("/purchase-orders")
def purchase_orders(shipment_id: str | None = None):
    return [p.model_dump(mode="json") for p in repo.list_pos(shipment_id)]


@app.get("/audit")
def audit(limit: int = 100):
    return repo.list_audit(limit)


@app.get("/metrics")
def metrics():
    usage = repo.llm_usage_rows(2000)
    by_model: dict[str, dict] = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                                     "cost_usd": 0.0})
    by_task: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for u in usage:
        m = by_model[u["model"]]
        m["calls"] += 1
        m["prompt_tokens"] += u["prompt_tokens"]
        m["completion_tokens"] += u["completion_tokens"]
        m["cost_usd"] = round(m["cost_usd"] + u["cost_usd"], 6)
        by_task[u["task_type"]][u["model"]] += 1
    get_cache(ROUTE_CACHE_NS)  # make sure the main cache shows up even before the first run
    approvals_all = repo.list_approvals()
    pos = repo.list_pos()
    return {
        "cache": {ns: c.snapshot() for ns, c in all_caches().items()},
        "llm_by_model": by_model,
        "llm_by_task": {k: dict(v) for k, v in by_task.items()},
        "recent_llm_calls": usage[:50],
        "approvals": {s.value: sum(a.status == s for a in approvals_all) for s in ApprovalStatus},
        "purchase_orders": {"total": len(pos), "reroute_pos": sum(not p.po_id.endswith("-ORIG") for p in pos),
                            "auto_executed": sum(p.auto_executed and not p.po_id.endswith("-ORIG") for p in pos)},
    }
