"""Structured output of the Disruption Monitoring Agent (enforced via CrewAI output_pydantic)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ShipmentRiskView(BaseModel):
    shipment_id: str
    risk_score: float = Field(ge=0, le=1)
    at_risk: bool
    explanation: str


class MonitoringReport(BaseModel):
    summary: str = Field(description="2-4 sentence situation report for the logistics control tower")
    shipments: list[ShipmentRiskView]
