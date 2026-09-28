"""The two agent backends (part of the app's runtime wiring). Each composes the four agents; the flow only sees the AgentBackend interface.

    OfflineBackend  deterministic agents: no LLM, used without an API key, in tests and as the fallback
    CrewAIBackend   CrewAI + OpenAI agents; Supply Chain Core tools come from the MCP servers
"""

from __future__ import annotations

from oceanbridge.agents import disruption_monitor, route_optimizer, status_analyst, vendor_negotiator
from oceanbridge.agents.common.base import AgentCall
from oceanbridge.agents.common.context import RunContext
from oceanbridge.agents.common.mcp_tools import MCPToolbox
from oceanbridge.llm.router import RoutingDecision
from oceanbridge.models import Disruption, RiskAssessment, RouteOption, Shipment


class OfflineBackend:
    name = "offline"

    def monitor(self, ctx: RunContext, disruptions: list[Disruption], prescreen: list[RiskAssessment],
                decision: RoutingDecision) -> AgentCall:
        return disruption_monitor.run_offline(ctx, decision, disruptions, prescreen)

    def optimize(self, ctx: RunContext, shipment: Shipment, assessment: RiskAssessment,
                 options: list[RouteOption], decision: RoutingDecision) -> AgentCall:
        return route_optimizer.run_offline(ctx, decision, shipment, assessment, options)

    def negotiate(self, ctx: RunContext, shipment: Shipment, option: RouteOption, approval_id: str | None,
                  decision: RoutingDecision) -> AgentCall:
        return vendor_negotiator.run_offline(ctx, decision, shipment, option, approval_id)

    def briefing(self, ctx: RunContext, shipment_id: str, decision: RoutingDecision) -> AgentCall:
        return status_analyst.run_offline(ctx, decision, shipment_id)

    def close(self) -> None:
        pass


class CrewAIBackend:
    name = "crewai"

    def __init__(self):
        self.toolbox = MCPToolbox()

    def monitor(self, ctx: RunContext, disruptions: list[Disruption], prescreen: list[RiskAssessment],
                decision: RoutingDecision) -> AgentCall:
        return disruption_monitor.run_llm(ctx, decision, disruptions, prescreen)

    def optimize(self, ctx: RunContext, shipment: Shipment, assessment: RiskAssessment,
                 options: list[RouteOption], decision: RoutingDecision) -> AgentCall:
        return route_optimizer.run_llm(ctx, decision, self.toolbox, shipment, assessment, options)

    def negotiate(self, ctx: RunContext, shipment: Shipment, option: RouteOption, approval_id: str | None,
                  decision: RoutingDecision) -> AgentCall:
        return vendor_negotiator.run_llm(ctx, decision, self.toolbox, shipment, option, approval_id)

    def briefing(self, ctx: RunContext, shipment_id: str, decision: RoutingDecision) -> AgentCall:
        return status_analyst.run_llm(ctx, decision, self.toolbox, shipment_id)

    def close(self) -> None:
        self.toolbox.close()
