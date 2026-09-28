"""Backend interface shared by all agents. The flow is backend-agnostic: CrewAI (LLM) or offline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from oceanbridge.agents.common.context import RunContext
from oceanbridge.llm.router import RoutingDecision
from oceanbridge.models import Disruption, RiskAssessment, RouteOption, Shipment


@dataclass
class AgentCall:
    """Result wrapper carrying token usage and the model that actually produced the output."""
    output: object
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class AgentBackend(Protocol):
    name: str

    def monitor(self, ctx: RunContext, disruptions: list[Disruption], prescreen: list[RiskAssessment],
                decision: RoutingDecision) -> AgentCall: ...  # output: MonitoringReport

    def optimize(self, ctx: RunContext, shipment: Shipment, assessment: RiskAssessment,
                 options: list[RouteOption], decision: RoutingDecision) -> AgentCall: ...  # output: OptimizationChoice

    def negotiate(self, ctx: RunContext, shipment: Shipment, option: RouteOption, approval_id: str | None,
                  decision: RoutingDecision) -> AgentCall: ...  # output: NegotiationResult

    def briefing(self, ctx: RunContext, shipment_id: str, decision: RoutingDecision) -> AgentCall: ...  # StatusBriefing

    def close(self) -> None: ...


def offline_label(decision: RoutingDecision) -> str:
    """Model label recorded for deterministic agents: shows which model the router would have used."""
    return f"offline-heuristic (routed:{decision.model})"


def fallback_reason(err: Exception) -> str:
    return f"LLM agent output unusable ({type(err).__name__}: {err}); deterministic fallback applied"
