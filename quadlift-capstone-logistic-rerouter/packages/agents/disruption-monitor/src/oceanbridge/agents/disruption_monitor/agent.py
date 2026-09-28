"""Disruption Monitoring Agent: validates the sensor-fusion pre-screen and writes the situation report."""

from __future__ import annotations

import json
from pathlib import Path

from oceanbridge.agents.common.base import AgentCall, offline_label
from oceanbridge.agents.common.context import RunContext
from oceanbridge.agents.common.crew_runner import load_config, run_crew
from oceanbridge.agents.disruption_monitor.schemas import MonitoringReport, ShipmentRiskView
from oceanbridge.agents.disruption_monitor.tools import feed_tools
from oceanbridge.llm.router import RoutingDecision
from oceanbridge.models import Disruption, RiskAssessment

CONFIG = load_config(Path(__file__).parent)


def run_llm(ctx: RunContext, decision: RoutingDecision, disruptions: list[Disruption],
            prescreen: list[RiskAssessment]) -> AgentCall:
    relevant = [p for p in prescreen if p.disruption_ids]
    fields = {
        "disruptions": json.dumps([d.model_dump(mode="json") for d in disruptions], indent=1),
        "prescreen": json.dumps([{"shipment_id": p.shipment_id, "risk_score": p.risk_score,
                                  "expected_delay_days": p.expected_delay_days, "at_risk": p.at_risk,
                                  "misses_required_date": p.misses_required_date, "drivers": p.drivers}
                                 for p in relevant], indent=1),
    }
    return run_crew(ctx, CONFIG, fields, MonitoringReport, decision, feed_tools())


def run_offline(ctx: RunContext, decision: RoutingDecision, disruptions: list[Disruption],
                prescreen: list[RiskAssessment]) -> AgentCall:
    at_risk = [a for a in prescreen if a.at_risk]
    worst = sorted(disruptions, key=lambda d: -d.severity)[:3]
    summary = (f"{len(disruptions)} active disruptions detected"
               + (f" (most severe: {', '.join(f'{d.type.value} at {d.location} sev {d.severity:.2f}' for d in worst)})"
                  if worst else "")
               + f". {len(at_risk)} of {len(prescreen)} monitored shipments are at risk of delay.")
    views = [ShipmentRiskView(shipment_id=a.shipment_id, risk_score=a.risk_score, at_risk=a.at_risk,
                              explanation="; ".join(a.drivers) + (f"; +{a.expected_delay_days}d expected delay"
                                                                  + ("; misses required date"
                                                                     if a.misses_required_date else "")))
             for a in prescreen if a.disruption_ids]
    return AgentCall(MonitoringReport(summary=summary, shipments=views), offline_label(decision))
