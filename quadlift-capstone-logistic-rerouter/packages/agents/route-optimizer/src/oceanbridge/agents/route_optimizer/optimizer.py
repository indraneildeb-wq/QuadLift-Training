"""Route optimisation pipeline around the agent: semantic cache -> routed LLM -> escalation -> offline fallback."""

from __future__ import annotations

from dataclasses import dataclass, field

from oceanbridge.agents.common.base import AgentBackend, fallback_reason
from oceanbridge.agents.common.context import RunContext
from oceanbridge.agents.route_optimizer.agent import explain_option, run_offline
from oceanbridge.agents.route_optimizer.schemas import OptimizationChoice
from oceanbridge.cache.semantic_cache import CacheQuery, SemanticCache, get_cache
from oceanbridge.config import get_settings
from oceanbridge.core.geo import PORTS
from oceanbridge.core.routing import RouteEngine
from oceanbridge.llm.router import ComplexitySignals, TaskType
from oceanbridge.models import Mode, RiskAssessment, RouteOption, Shipment, ShipmentStatus

ROUTE_CACHE_NS = "route_decisions"


def _value_band(v: float) -> str:
    return "sign-off value band" if v >= get_settings().hitl.max_cargo_value_usd else "standard value band"


def _slack_band(shipment: Shipment) -> str:
    slack = (shipment.required_delivery_date - shipment.eta).days
    return "tight" if slack <= 2 else "moderate" if slack <= 6 else "loose"


def route_decision_query(shipment: Shipment, assessment: RiskAssessment, disruptions_fp: list[str]) -> CacheQuery:
    """Cache key/text/guard for an optimisation query.

    Exact key: every shipment-specific parameter. Semantic tier: shipments on the same lane and route, in
    the same disruption situation, value band and delivery-slack band, with weight/volume within tolerance,
    reuse the same routing *strategy* (which is then re-priced for the actual shipment)."""
    guard = {
        "origin": shipment.origin, "destination": shipment.destination, "mode": shipment.mode.value,
        "status": shipment.status.value, "route": RouteEngine._signature(shipment.legs),
        "disruptions": disruptions_fp, "value_band": _value_band(shipment.cargo_value_usd),
        "slack": _slack_band(shipment), "weight_kg": shipment.weight_kg, "volume_cbm": shipment.volume_cbm,
    }
    params = {**guard, "value": round(shipment.cargo_value_usd, -3), "rdd": str(shipment.required_delivery_date),
              "eta": str(shipment.eta), "delay": assessment.expected_delay_days}
    o, d = PORTS[shipment.origin].name, PORTS[shipment.destination].name
    text = (f"Reroute a {shipment.status.value.replace('_', ' ')} {shipment.mode.value} shipment from {o} "
            f"({shipment.origin}) to {d} ({shipment.destination}), about {round(shipment.weight_kg / 1000)} tonnes, "
            f"{_value_band(shipment.cargo_value_usd)}, {_slack_band(shipment)} delivery window. "
            f"Disruptions: {', '.join(disruptions_fp) or 'none'}.")
    return CacheQuery(params=params, text=text, guard=guard, touchpoints=sorted(shipment.touchpoints))


def disruption_fingerprint(assessment: RiskAssessment, disruptions: dict[str, dict]) -> list[str]:
    return sorted(f"{d}:{disruptions[d]['severity']:.1f}" for d in assessment.disruption_ids if d in disruptions)


def _sig(o: RouteOption) -> str:
    return "STAY" if o.option_id == "STAY" else RouteEngine._signature(o.legs)


@dataclass
class OptimizationOutcome:
    choice: OptimizationChoice
    option: RouteOption
    cache_tier: str
    model: str
    similarity: float | None = None
    errors: list[str] = field(default_factory=list)


class RouteOptimizer:
    def __init__(self, backend: AgentBackend, ctx: RunContext, cache: SemanticCache | None = None):
        self.backend = backend
        self.ctx = ctx
        self.cache = cache or get_cache(ROUTE_CACHE_NS)

    def optimize(self, shipment: Shipment, assessment: RiskAssessment, options: list[RouteOption],
                 disruptions: dict[str, dict], use_cache: bool = True, write_cache: bool = True) -> OptimizationOutcome:
        by_sig = {_sig(o): o for o in options}
        q = route_decision_query(shipment, assessment, disruption_fingerprint(assessment, disruptions))
        signals = ComplexitySignals(
            n_candidates=len(options), multimodal_candidates=sum(o.mode == Mode.MULTIMODAL for o in options),
            cargo_value_usd=shipment.cargo_value_usd, n_disruptions=len(assessment.disruption_ids),
            in_transit=shipment.status == ShipmentStatus.IN_TRANSIT,
        )
        decision = self.ctx.router.select(TaskType.ROUTE_OPTIMIZATION, signals)

        # ---- tiers L1/L2/L3
        hit = self.cache.get(q) if use_cache else None
        if hit and hit.payload.get("selected_sig") in by_sig:
            chosen = by_sig[hit.payload["selected_sig"]]
            ranked = [by_sig[s].option_id for s in hit.payload.get("ranked_sigs", []) if s in by_sig]
            ranked += [o.option_id for o in options if o.option_id not in ranked]
            rationale = (f"[{hit.tier} cache hit, similarity {hit.similarity}] Routing strategy reused from an "
                         f"equivalent query and re-priced for this shipment. "
                         + explain_option(shipment, chosen, by_sig.get("STAY")))
            self.ctx.usage(decision, cache_hit=True, executed_model=f"cache:{hit.tier}")
            choice = OptimizationChoice(selected_option_id=chosen.option_id, ranked_option_ids=ranked, rationale=rationale)
            return OptimizationOutcome(choice, chosen, hit.tier, f"cache (originally {hit.payload.get('model')})",
                                       hit.similarity)

        # ---- routed model, escalate once on unusable output, then deterministic fallback
        errors: list[str] = []
        valid = {o.option_id for o in options}

        def attempt(dec):
            c = self.backend.optimize(self.ctx, shipment, assessment, options, dec)
            if c.output is None or c.output.selected_option_id not in valid:
                self.ctx.usage(dec, c.prompt_tokens, c.completion_tokens, executed_model=c.model)
                raise ValueError(f"selected_option_id {getattr(c.output, 'selected_option_id', None)!r} "
                                 f"is not one of the candidates")
            return c

        call, last_err = None, None
        try:
            call = attempt(decision)
        except Exception as e:
            last_err = e
            if decision.tier == "light":
                decision = self.ctx.router.select(TaskType.ROUTE_OPTIMIZATION, signals, escalate=True)
                self.ctx.trace("route-model", f"{shipment.id}: escalating to {decision.model} ({e})",
                               shipment_id=shipment.id)
                try:
                    call = attempt(decision)
                except Exception as e2:
                    last_err = e2
        if call is None:
            errors.append(f"{shipment.id}: {fallback_reason(last_err)}")
            call = run_offline(self.ctx, decision, shipment, assessment, options)

        self.ctx.usage(decision, call.prompt_tokens, call.completion_tokens, executed_model=call.model)
        choice: OptimizationChoice = call.output
        option = next(o for o in options if o.option_id == choice.selected_option_id)
        if write_cache:
            id_to_sig = {o.option_id: _sig(o) for o in options}
            self.cache.put(q, {"selected_sig": id_to_sig[choice.selected_option_id],
                               "ranked_sigs": [id_to_sig[i] for i in choice.ranked_option_ids if i in id_to_sig],
                               "rationale": choice.rationale, "model": call.model,
                               "_tokens": call.prompt_tokens + call.completion_tokens},
                           disrupted=bool(assessment.disruption_ids))
        return OptimizationOutcome(choice, option, "miss", call.model, None, errors)
