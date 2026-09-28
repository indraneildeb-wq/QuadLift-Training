"""Pydantic domain schemas shared by the core, agents, MCP server and API."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Mode(str, Enum):
    SEA = "sea"
    AIR = "air"
    RAIL = "rail"
    ROAD = "road"
    MULTIMODAL = "multimodal"


class ShipmentStatus(str, Enum):
    BOOKED = "booked"
    IN_TRANSIT = "in_transit"
    REROUTED = "rerouted"
    PENDING_APPROVAL = "pending_approval"
    DELIVERED = "delivered"


class DisruptionType(str, Enum):
    WEATHER = "weather"
    PORT_CONGESTION = "port_congestion"
    PORT_CLOSURE = "port_closure"
    GEOPOLITICAL = "geopolitical"
    LABOR = "labor"
    CANAL_RESTRICTION = "canal_restriction"


class Leg(BaseModel):
    mode: Mode
    origin: str
    destination: str
    carrier_id: str
    via: list[str] = Field(default_factory=list, description="Chokepoints/waypoints traversed")
    distance_km: float = 0.0
    transit_days: float = 0.0
    cost_usd: float = 0.0


class Shipment(BaseModel):
    id: str
    customer: str
    commodity: str
    origin: str
    destination: str
    mode: Mode
    carrier_id: str
    legs: list[Leg]
    weight_kg: float
    volume_cbm: float
    cargo_value_usd: float
    current_cost_usd: float
    departure_date: date
    eta: date
    required_delivery_date: date
    status: ShipmentStatus
    po_id: str | None = None

    @property
    def touchpoints(self) -> set[str]:
        pts: set[str] = set()
        for leg in self.legs:
            pts.update([leg.origin, leg.destination, *leg.via])
        return pts


class Disruption(BaseModel):
    id: str
    type: DisruptionType
    location: str = Field(description="Port code or chokepoint id")
    severity: float = Field(ge=0, le=1)
    expected_delay_days: float
    start_date: date
    end_date: date
    description: str
    source: str
    active: bool = True


class RiskAssessment(BaseModel):
    shipment_id: str
    risk_score: float = Field(ge=0, le=1)
    expected_delay_days: float
    disruption_ids: list[str]
    drivers: list[str]
    at_risk: bool
    projected_eta: date | None = None
    misses_required_date: bool = False


class RouteOption(BaseModel):
    option_id: str
    shipment_id: str
    label: str
    mode: Mode
    legs: list[Leg]
    total_cost_usd: float
    transit_days: float
    co2_kg: float
    cost_delta_usd: float
    cost_increase_pct: float
    projected_eta: date
    lead_time_delta_days: float
    residual_risk: float
    landed_cost_score: float = Field(description="Freight + holding + lateness + risk exposure; lower is better")
    capacity_ok: bool = True
    primary_carrier_id: str

    @property
    def touchpoints(self) -> set[str]:
        pts: set[str] = set()
        for leg in self.legs:
            pts.update([leg.origin, leg.destination, *leg.via])
        return pts


class OptimizationDecision(BaseModel):
    shipment_id: str
    selected_option_id: str
    ranked_option_ids: list[str]
    rationale: str
    model_used: str | None = None
    cache_tier: str | None = None


class HitlDecision(BaseModel):
    auto_execute: bool
    cost_increase_pct: float
    cargo_value_usd: float
    reasons: list[str]
    policy: str


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"


class ApprovalRequest(BaseModel):
    id: str
    run_id: str
    shipment_id: str
    option: RouteOption
    alternatives: list[RouteOption] = Field(default_factory=list)
    decision: HitlDecision
    rationale: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    approver: str | None = None
    comment: str | None = None
    created_at: datetime
    decided_at: datetime | None = None


class SLATerms(BaseModel):
    guaranteed_transit_days: float
    late_penalty_pct_per_day: float = Field(description="% of freight charge credited per day late")
    max_penalty_pct: float
    reserved_capacity: str
    free_demurrage_days: int = 3
    notes: str = ""


class SLAProposalResponse(BaseModel):
    carrier_id: str
    accepted: bool
    round: int
    counter_amount_usd: float
    counter_terms: SLATerms
    message: str


class PurchaseOrder(BaseModel):
    po_id: str
    shipment_id: str
    carrier_id: str
    amount_usd: float
    route_label: str
    legs: list[Leg]
    sla: SLATerms
    approval_id: str | None = None
    supersedes_po_id: str | None = None
    status: str = "issued"
    auto_executed: bool
    created_at: datetime


class NegotiationResult(BaseModel):
    shipment_id: str
    po: PurchaseOrder
    rounds: int
    list_price_usd: float
    final_amount_usd: float
    savings_usd: float
    carrier_message: str
    model_used: str | None = None
