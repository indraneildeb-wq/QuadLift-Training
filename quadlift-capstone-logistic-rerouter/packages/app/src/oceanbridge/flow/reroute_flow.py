"""Multi-agent rerouting pipeline as a CrewAI Flow.

    detect_disruptions -> assess_shipments -> optimize_routes -> hitl_gate (router)
                                                                   |-> "dispatch": execute_auto_reroutes
                                                                   |               queue_for_approval
                                                                   '-> "idle"

Sign-offs: oceanbridge.runtime.execute_approval runs the negotiation + PO under the approval id (through
oceanbridge.hitl.approvals); reject_approval restores the shipment."""

from __future__ import annotations

import os
import uuid
from datetime import date

from crewai.flow.flow import Flow, listen, router, start
from pydantic import BaseModel, Field

from oceanbridge.agents import disruption_monitor
from oceanbridge.agents.common.base import AgentBackend, fallback_reason
from oceanbridge.agents.common.context import RunContext, Timer
from oceanbridge.agents.route_optimizer import ROUTE_CACHE_NS, RouteOptimizer
from oceanbridge.agents.vendor_negotiator import negotiate_reroute
from oceanbridge.cache.semantic_cache import all_caches, get_cache
from oceanbridge.config import get_settings
from oceanbridge.core import repository as repo
from oceanbridge.core import risk, service
from oceanbridge.feeds import scenarios
from oceanbridge.hitl import approvals, gate
from oceanbridge.llm.router import ComplexitySignals, TaskType
from oceanbridge.runtime import make_backend
from oceanbridge.models import (
    ApprovalStatus,
    Disruption,
    HitlDecision,
    RiskAssessment,
    RouteOption,
    ShipmentStatus,
)

os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")

MONITORED = [ShipmentStatus.BOOKED, ShipmentStatus.IN_TRANSIT, ShipmentStatus.REROUTED]


class RunState(BaseModel):
    run_id: str = ""
    situation_report: str = ""
    disruptions: list[dict] = Field(default_factory=list)
    new_disruptions: list[str] = Field(default_factory=list)
    at_risk: list[dict] = Field(default_factory=list)
    decisions: list[dict] = Field(default_factory=list)
    auto_queue: list[dict] = Field(default_factory=list)
    approval_queue: list[dict] = Field(default_factory=list)
    executed: list[dict] = Field(default_factory=list)
    queued: list[dict] = Field(default_factory=list)
    kept: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class RerouteFlow(Flow[RunState]):
    # `backend` (AgentBackend) and `ctx` (RunContext) are attached by run_pipeline before kickoff.

    # 1. Disruption Monitoring Agent: sensor fusion, then LLM validation / explanation
    @start()
    def detect_disruptions(self):
        self.state.run_id = self.ctx.run_id
        with Timer() as t:
            detected = risk.detect_all()
        before = {d.id for d in repo.active_disruptions()}
        repo.clear_disruptions()
        new_locs: set[str] = set()
        for d in detected:
            repo.upsert_disruption(d)
            if d.id not in before:
                new_locs.add(d.location)
        self.state.disruptions = [d.model_dump(mode="json") for d in detected]
        self.state.new_disruptions = sorted({d.id for d in detected} - before)
        evicted = get_cache(ROUTE_CACHE_NS).invalidate_locations(new_locs) if new_locs else 0
        self.ctx.trace("detect", f"Sensor fusion over weather/AIS/congestion/geopolitical feeds: {len(detected)} "
                                 f"active disruptions ({len(self.state.new_disruptions)} new); "
                                 f"{evicted} cached route decisions invalidated",
                       duration_ms=t.ms, agent="Disruption Monitoring Agent")

    @listen(detect_disruptions)
    def assess_shipments(self):
        disruptions = [Disruption(**d) for d in self.state.disruptions]
        shipments = [s for s in repo.list_shipments(MONITORED) if not repo.pending_approval_for(s.id)]
        prescreen = [risk.assess(s, disruptions) for s in shipments]
        merged = {p.shipment_id: p for p in prescreen}
        report_summary = ""
        if any(p.disruption_ids for p in prescreen):
            decision = self.ctx.router.select(TaskType.DISRUPTION_MONITORING,
                                              ComplexitySignals(n_disruptions=len(disruptions)))
            with Timer() as t:
                try:
                    call = self.backend.monitor(self.ctx, disruptions, prescreen, decision)
                except Exception as e:  # an LLM failure must not stop monitoring
                    self.state.errors.append(fallback_reason(e))
                    call = disruption_monitor.run_offline(self.ctx, decision, disruptions, prescreen)
            self.ctx.usage(decision, call.prompt_tokens, call.completion_tokens, executed_model=call.model)
            report_summary = call.output.summary
            for view in call.output.shipments:
                base = merged.get(view.shipment_id)
                if base is None:
                    continue  # the agent may not introduce shipments outside the pre-screen
                adj = min(max(view.risk_score, base.risk_score - 0.15), base.risk_score + 0.15)
                merged[view.shipment_id] = base.model_copy(update={
                    "risk_score": round(min(1.0, max(0.0, adj)), 3),
                    "at_risk": base.at_risk or view.at_risk,
                    "drivers": base.drivers + ([f"analyst: {view.explanation}"] if view.explanation else []),
                })
            self.ctx.trace("monitor", report_summary, model=call.model, routing=decision.reason, duration_ms=t.ms,
                           agent="Disruption Monitoring Agent")
        if not report_summary:
            waiting = len(repo.list_approvals(ApprovalStatus.PENDING))
            report_summary = (f"{len(disruptions)} active disruptions, but no monitored shipment is newly at risk"
                              + (f" ({waiting} already awaiting approval)." if waiting else ".")
                              if disruptions else "No active disruptions affecting monitored shipments.")
        self.state.situation_report = report_summary
        self.state.at_risk = [a.model_dump(mode="json") for a in merged.values() if a.at_risk]

    # 2. Route & Capacity Optimization Agent (semantic cache + dynamic model routing)
    @listen(assess_shipments)
    def optimize_routes(self):
        optimizer = RouteOptimizer(self.backend, self.ctx)
        dmap = {d["id"]: d for d in self.state.disruptions}
        for a_dict in self.state.at_risk:
            assessment = RiskAssessment(**a_dict)
            shipment = repo.get_shipment(assessment.shipment_id)
            try:
                with Timer() as t:
                    options = service.list_route_alternatives(shipment.id)
                    out = optimizer.optimize(shipment, assessment, options, dmap)
                self.state.errors.extend(out.errors)
                self.state.decisions.append({
                    "shipment_id": shipment.id, "option": out.option.model_dump(mode="json"),
                    "alternatives": [o.model_dump(mode="json") for o in options
                                     if o.option_id != out.option.option_id][:4],
                    "rationale": out.choice.rationale, "cache_tier": out.cache_tier, "model": out.model,
                })
                self.ctx.trace("optimize", f"{shipment.id}: {out.option.label} ({out.option.cost_increase_pct:+.1f}%, "
                                           f"ETA {out.option.projected_eta})", shipment_id=shipment.id,
                               model=out.model, cache_tier=out.cache_tier, duration_ms=t.ms,
                               agent="Route & Capacity Optimization Agent")
            except Exception as e:
                self.state.errors.append(f"optimize {shipment.id}: {type(e).__name__}: {e}")

    # 3. HITL gate: deterministic policy, never delegated to an LLM
    @router(optimize_routes)
    def hitl_gate(self):
        for d in self.state.decisions:
            option = RouteOption(**d["option"])
            if option.option_id == "STAY":
                self.state.kept.append(d["shipment_id"])
                continue
            shipment = repo.get_shipment(d["shipment_id"])
            decision = gate.evaluate(option.cost_increase_pct, shipment.cargo_value_usd)
            d["hitl"] = decision.model_dump(mode="json")
            (self.state.auto_queue if decision.auto_execute else self.state.approval_queue).append(d)
            self.ctx.trace("hitl", f"{shipment.id}: {'AUTO-EXECUTE' if decision.auto_execute else 'SIGN-OFF REQUIRED'}"
                                   f" - {'; '.join(decision.reasons)}", shipment_id=shipment.id, agent="HITL Gate")
        return "dispatch" if (self.state.auto_queue or self.state.approval_queue) else "idle"

    # 4a. Vendor Negotiation & PO Agent: auto-executed reroutes
    @listen("dispatch")
    def execute_auto_reroutes(self):
        for d in self.state.auto_queue:
            shipment = repo.get_shipment(d["shipment_id"])
            try:
                self.state.executed.append(negotiate_reroute(self.backend, self.ctx, shipment,
                                                             RouteOption(**d["option"]), approval_id=None))
            except Exception as e:
                self.state.errors.append(f"negotiate {shipment.id}: {type(e).__name__}: {e}")

    # 4b. Queue for Logistics Operations Manager sign-off
    @listen("dispatch")
    def queue_for_approval(self):
        for d in self.state.approval_queue:
            option = RouteOption(**d["option"])
            approval = approvals.request_approval(
                self.ctx.run_id, d["shipment_id"], option, [RouteOption(**a) for a in d["alternatives"]],
                HitlDecision(**d["hitl"]), d["rationale"])
            self.state.queued.append({"approval_id": approval.id, "shipment_id": d["shipment_id"],
                                      "option": option.label, "cost_increase_pct": option.cost_increase_pct})
            self.ctx.trace("approval", f"{d['shipment_id']}: queued {approval.id} for "
                                       f"{get_settings().hitl.approver_role}", shipment_id=d["shipment_id"])


# ---------------------------------------------------------------------- entry points used by the API
def _summary(state: RunState) -> dict:
    return {
        "situation_report": state.situation_report,
        "disruptions": len(state.disruptions),
        "new_disruptions": state.new_disruptions,
        "at_risk": len(state.at_risk),
        "decisions": [{k: d[k] for k in ("shipment_id", "cache_tier", "model", "rationale")} |
                      {"option": d["option"]["label"], "cost_increase_pct": d["option"]["cost_increase_pct"]}
                      for d in state.decisions],
        "auto_executed": state.executed,
        "queued_for_approval": state.queued,
        "kept_current_routing": state.kept,
        "errors": state.errors,
        "cache": {ns: c.snapshot() for ns, c in all_caches().items()},
    }


def run_pipeline(scenario: str | None = None, run_id: str | None = None, backend: AgentBackend | None = None) -> dict:
    """One monitoring -> optimisation -> HITL -> execution cycle. Returns the run summary."""
    if scenario:
        scenarios.activate(scenario)
    backend = backend or make_backend()
    run_id = run_id or f"RUN-{date.today():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
    if repo.get_run(run_id) is None:
        repo.create_run(run_id, backend.name)
    ctx = RunContext(run_id=run_id, backend=backend.name)
    flow = RerouteFlow()
    flow.backend, flow.ctx = backend, ctx
    try:
        flow.kickoff()
        summary = _summary(flow.state)
        repo.finish_run(run_id, "completed", summary)
        return {"run_id": run_id, "backend": backend.name, **summary}
    except Exception as e:
        repo.finish_run(run_id, "failed", _summary(flow.state), error=f"{type(e).__name__}: {e}")
        raise
    finally:
        backend.close()


def optimize_what_if(shipment_id: str, overrides: dict | None = None, backend: AgentBackend | None = None) -> dict:
    """Dry-run optimisation for a shipment, optionally with hypothetical attributes (weight, value, ...).
    Uses the semantic cache and model routing but never issues POs or approvals."""
    shipment = repo.get_shipment(shipment_id)
    if shipment is None:
        raise KeyError(shipment_id)
    if overrides:
        shipment = shipment.model_copy(update={k: v for k, v in overrides.items() if v is not None})
    backend = backend or make_backend()
    ctx = RunContext(run_id=f"WHATIF-{uuid.uuid4().hex[:6].upper()}", backend=backend.name)
    try:
        disruptions = repo.active_disruptions()
        assessment = risk.assess(shipment, disruptions)
        options = service.alternatives_for(shipment)
        out = RouteOptimizer(backend, ctx).optimize(shipment, assessment, options,
                                                    {d.id: d.model_dump(mode="json") for d in disruptions})
    finally:
        backend.close()
    hitl = gate.evaluate(out.option.cost_increase_pct, shipment.cargo_value_usd)
    return {
        "shipment_id": shipment_id, "overrides": overrides or {}, "assessment": assessment.model_dump(mode="json"),
        "options": [o.model_dump(mode="json") for o in options], "selected_option_id": out.option.option_id,
        "rationale": out.choice.rationale, "cache_tier": out.cache_tier, "similarity": out.similarity,
        "model": out.model, "hitl": hitl.model_dump(mode="json"), "errors": out.errors,
    }
