"""Dynamic model routing: cheap model for status checks / monitoring, strong model for complex
route optimisation and negotiation, with escalation when the light model's output fails validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from oceanbridge.config import get_settings


class TaskType(str, Enum):
    STATUS_CHECK = "status_check"
    DISRUPTION_MONITORING = "disruption_monitoring"
    ROUTE_OPTIMIZATION = "route_optimization"
    NEGOTIATION = "negotiation"
    CHAT = "chat"


@dataclass
class ComplexitySignals:
    n_candidates: int = 0
    multimodal_candidates: int = 0
    cargo_value_usd: float = 0.0
    n_disruptions: int = 0
    in_transit: bool = False
    needs_approval: bool = False
    analytical: bool = False  # chat: the user asks for a comparison, recommendation or explanation


@dataclass
class RoutingDecision:
    task_type: TaskType
    model: str
    tier: str  # "light" | "heavy"
    score: int
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


# Base tier per task type; route optimisation is decided by complexity score.
BASE_TIER = {
    TaskType.STATUS_CHECK: "light",
    TaskType.DISRUPTION_MONITORING: "light",
    TaskType.NEGOTIATION: "heavy",
}


class ModelRouter:
    def __init__(self):
        self.cfg = get_settings().models

    def complexity(self, s: ComplexitySignals) -> tuple[int, list[str]]:
        score, why = 0, []
        if s.n_candidates >= 3:
            score += 1
            why.append(f"{s.n_candidates} candidate routes")
        if s.multimodal_candidates:
            score += 1
            why.append(f"{s.multimodal_candidates} multimodal options")
        if s.cargo_value_usd >= 250_000:
            score += 1
            why.append(f"high-value cargo ${s.cargo_value_usd:,.0f}")
        if s.n_disruptions >= 2:
            score += 1
            why.append(f"{s.n_disruptions} compounding disruptions")
        if s.in_transit:
            score += 1
            why.append("in-transit diversion")
        return score, why

    def select(self, task_type: TaskType, signals: ComplexitySignals | None = None, escalate: bool = False) -> RoutingDecision:
        signals = signals or ComplexitySignals()
        score, why = self.complexity(signals)
        if escalate:
            return RoutingDecision(task_type, self.cfg.heavy, "heavy", score, ["escalated after light-model validation failure", *why])
        if task_type == TaskType.ROUTE_OPTIMIZATION:
            tier = "heavy" if score >= self.cfg.escalation_threshold else "light"
            why = why or ["simple lane, few alternatives"]
            why.append(f"complexity {score} {'>=' if tier == 'heavy' else '<'} threshold {self.cfg.escalation_threshold}")
        elif task_type == TaskType.CHAT:
            tier = "heavy" if signals.analytical else "light"
            why = ["chat: analytical question (compare / recommend / explain)" if signals.analytical
                   else "chat: lookup or action request"]
        else:
            tier = BASE_TIER[task_type]
            why = [f"{task_type.value} routed to {tier} tier"]
        model = self.cfg.heavy if tier == "heavy" else self.cfg.light
        return RoutingDecision(task_type, model, tier, score, why)

    def estimate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        pin, pout = self.cfg.pricing.get(model, (0.0, 0.0))
        return round((prompt_tokens * pin + completion_tokens * pout) / 1_000_000, 6)
