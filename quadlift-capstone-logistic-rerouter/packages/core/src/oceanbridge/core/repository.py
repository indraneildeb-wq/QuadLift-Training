"""Data access helpers converting between ORM rows and domain models."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from oceanbridge.core.db import (
    ApprovalRow,
    AuditRow,
    CarrierRow,
    DisruptionRow,
    LlmUsageRow,
    NegotiationRow,
    PurchaseOrderRow,
    RunRow,
    ShipmentRow,
    session_scope,
)
from oceanbridge.models import (
    ApprovalRequest,
    ApprovalStatus,
    Disruption,
    HitlDecision,
    Leg,
    PurchaseOrder,
    RouteOption,
    Shipment,
    ShipmentStatus,
    SLATerms,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- shipments
def _shipment(row: ShipmentRow) -> Shipment:
    return Shipment(
        id=row.id,
        customer=row.customer,
        commodity=row.commodity,
        origin=row.origin,
        destination=row.destination,
        mode=row.mode,
        carrier_id=row.carrier_id,
        legs=[Leg(**leg) for leg in row.legs],
        weight_kg=row.weight_kg,
        volume_cbm=row.volume_cbm,
        cargo_value_usd=row.cargo_value_usd,
        current_cost_usd=row.current_cost_usd,
        departure_date=row.departure_date,
        eta=row.eta,
        required_delivery_date=row.required_delivery_date,
        status=row.status,
        po_id=row.po_id,
    )


def get_shipment(shipment_id: str) -> Shipment | None:
    with session_scope() as s:
        row = s.get(ShipmentRow, shipment_id)
        return _shipment(row) if row else None


def list_shipments(statuses: list[ShipmentStatus] | None = None) -> list[Shipment]:
    with session_scope() as s:
        q = select(ShipmentRow).order_by(ShipmentRow.id)
        if statuses:
            q = q.where(ShipmentRow.status.in_([st.value for st in statuses]))
        return [_shipment(r) for r in s.scalars(q)]


def set_shipment_status(shipment_id: str, status: ShipmentStatus) -> None:
    with session_scope() as s:
        row = s.get(ShipmentRow, shipment_id)
        if row:
            row.status = status.value


def apply_reroute(shipment_id: str, option: RouteOption, po_id: str, amount_usd: float) -> None:
    with session_scope() as s:
        row = s.get(ShipmentRow, shipment_id)
        row.legs = [leg.model_dump(mode="json") for leg in option.legs]
        row.mode = option.mode.value
        row.carrier_id = option.primary_carrier_id
        row.current_cost_usd = round(amount_usd, 2)
        row.eta = option.projected_eta
        row.status = ShipmentStatus.REROUTED.value
        row.po_id = po_id


# ---------------------------------------------------------------- carriers
def carrier_capacity(carrier_id: str) -> float:
    with session_scope() as s:
        row = s.get(CarrierRow, carrier_id)
        return row.capacity_remaining if row else 0.0


def all_capacity() -> dict[str, float]:
    with session_scope() as s:
        return {r.id: r.capacity_remaining for r in s.scalars(select(CarrierRow))}


def consume_capacity(carrier_id: str, amount: float) -> None:
    with session_scope() as s:
        row = s.get(CarrierRow, carrier_id)
        if row is None or row.capacity_remaining < amount:
            raise ValueError(f"Insufficient capacity on {carrier_id}")
        row.capacity_remaining -= amount


# ---------------------------------------------------------------- disruptions
def _disruption(row: DisruptionRow) -> Disruption:
    return Disruption(
        id=row.id, type=row.type, location=row.location, severity=row.severity,
        expected_delay_days=row.expected_delay_days, start_date=row.start_date, end_date=row.end_date,
        description=row.description, source=row.source, active=row.active,
    )


def active_disruptions() -> list[Disruption]:
    with session_scope() as s:
        return [_disruption(r) for r in s.scalars(select(DisruptionRow).where(DisruptionRow.active.is_(True)))]


def upsert_disruption(d: Disruption, scenario: str | None = None) -> bool:
    """Insert or update; returns True if this disruption is new."""
    with session_scope() as s:
        row = s.get(DisruptionRow, d.id)
        is_new = row is None
        if is_new:
            row = DisruptionRow(id=d.id)
            s.add(row)
        row.type, row.location, row.severity = d.type.value, d.location, d.severity
        row.expected_delay_days, row.start_date, row.end_date = d.expected_delay_days, d.start_date, d.end_date
        row.description, row.source, row.active, row.scenario = d.description, d.source, d.active, scenario
        return is_new


def clear_disruptions() -> None:
    with session_scope() as s:
        for row in s.scalars(select(DisruptionRow)):
            row.active = False


# ---------------------------------------------------------------- purchase orders
def _po(row: PurchaseOrderRow) -> PurchaseOrder:
    return PurchaseOrder(
        po_id=row.po_id, shipment_id=row.shipment_id, carrier_id=row.carrier_id, amount_usd=row.amount_usd,
        route_label=row.route_label, legs=[Leg(**leg) for leg in row.legs], sla=SLATerms(**row.sla),
        approval_id=row.approval_id, supersedes_po_id=row.supersedes_po_id, status=row.status,
        auto_executed=row.auto_executed, created_at=row.created_at,
    )


def insert_po(po: PurchaseOrder) -> None:
    with session_scope() as s:
        if po.supersedes_po_id:
            old = s.get(PurchaseOrderRow, po.supersedes_po_id)
            if old:
                old.status = "superseded"
        s.add(PurchaseOrderRow(
            po_id=po.po_id, shipment_id=po.shipment_id, carrier_id=po.carrier_id, amount_usd=po.amount_usd,
            route_label=po.route_label, legs=[leg.model_dump(mode="json") for leg in po.legs],
            sla=po.sla.model_dump(mode="json"), approval_id=po.approval_id, supersedes_po_id=po.supersedes_po_id,
            status=po.status, auto_executed=po.auto_executed, created_at=po.created_at,
        ))


def get_po(po_id: str) -> PurchaseOrder | None:
    with session_scope() as s:
        row = s.get(PurchaseOrderRow, po_id)
        return _po(row) if row else None


def list_pos(shipment_id: str | None = None) -> list[PurchaseOrder]:
    with session_scope() as s:
        q = select(PurchaseOrderRow).order_by(PurchaseOrderRow.created_at.desc())
        if shipment_id:
            q = q.where(PurchaseOrderRow.shipment_id == shipment_id)
        return [_po(r) for r in s.scalars(q)]


def count_pos() -> int:
    with session_scope() as s:
        return len(list(s.scalars(select(PurchaseOrderRow.po_id))))


# ---------------------------------------------------------------- approvals
def _approval(row: ApprovalRow) -> ApprovalRequest:
    p = row.payload
    return ApprovalRequest(
        id=row.id, run_id=row.run_id, shipment_id=row.shipment_id,
        option=RouteOption(**p["option"]), alternatives=[RouteOption(**a) for a in p.get("alternatives", [])],
        decision=HitlDecision(**p["decision"]), rationale=p.get("rationale", ""),
        status=row.status, approver=row.approver, comment=row.comment,
        created_at=row.created_at, decided_at=row.decided_at,
    )


def insert_approval(a: ApprovalRequest) -> None:
    with session_scope() as s:
        s.add(ApprovalRow(
            id=a.id, run_id=a.run_id, shipment_id=a.shipment_id,
            payload={
                "option": a.option.model_dump(mode="json"),
                "alternatives": [x.model_dump(mode="json") for x in a.alternatives],
                "decision": a.decision.model_dump(mode="json"),
                "rationale": a.rationale,
            },
            status=a.status.value, created_at=a.created_at,
        ))


def get_approval(approval_id: str) -> ApprovalRequest | None:
    with session_scope() as s:
        row = s.get(ApprovalRow, approval_id)
        return _approval(row) if row else None


def list_approvals(status: ApprovalStatus | None = None) -> list[ApprovalRequest]:
    with session_scope() as s:
        q = select(ApprovalRow).order_by(ApprovalRow.created_at.desc())
        if status:
            q = q.where(ApprovalRow.status == status.value)
        return [_approval(r) for r in s.scalars(q)]


def pending_approval_for(shipment_id: str) -> ApprovalRequest | None:
    with session_scope() as s:
        row = s.scalars(select(ApprovalRow).where(
            ApprovalRow.shipment_id == shipment_id, ApprovalRow.status == ApprovalStatus.PENDING.value
        )).first()
        return _approval(row) if row else None


def decide_approval(approval_id: str, status: ApprovalStatus, approver: str | None = None, comment: str | None = None) -> ApprovalRequest:
    with session_scope() as s:
        row = s.get(ApprovalRow, approval_id)
        if row is None:
            raise KeyError(approval_id)
        row.status = status.value
        if approver is not None:
            row.approver = approver
        if comment is not None:
            row.comment = comment
        row.decided_at = utcnow()
        return _approval(row)


# ---------------------------------------------------------------- runs
def create_run(run_id: str, backend: str) -> None:
    with session_scope() as s:
        s.add(RunRow(id=run_id, status="running", backend=backend, started_at=utcnow(), summary={}, trace=[]))


def append_trace(run_id: str, event: dict) -> None:
    with session_scope() as s:
        row = s.get(RunRow, run_id)
        if row:
            row.trace = [*row.trace, event]


def finish_run(run_id: str, status: str, summary: dict, error: str | None = None) -> None:
    with session_scope() as s:
        row = s.get(RunRow, run_id)
        row.status, row.summary, row.error, row.finished_at = status, summary, error, utcnow()


def get_run(run_id: str) -> dict | None:
    with session_scope() as s:
        row = s.get(RunRow, run_id)
        if row is None:
            return None
        return {
            "id": row.id, "status": row.status, "backend": row.backend, "started_at": row.started_at,
            "finished_at": row.finished_at, "summary": row.summary, "trace": row.trace, "error": row.error,
        }


def list_runs(limit: int = 20) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(RunRow).order_by(RunRow.started_at.desc()).limit(limit))
        return [{"id": r.id, "status": r.status, "backend": r.backend, "started_at": r.started_at,
                 "finished_at": r.finished_at, "summary": r.summary, "error": r.error} for r in rows]


# ---------------------------------------------------------------- negotiation / audit / usage
def record_negotiation_round(shipment_id: str, carrier_id: str, rnd: int, offer: dict, response: dict) -> None:
    with session_scope() as s:
        s.add(NegotiationRow(shipment_id=shipment_id, carrier_id=carrier_id, round=rnd, offer=offer,
                             response=response, created_at=utcnow()))


def negotiation_rounds(shipment_id: str, carrier_id: str) -> int:
    with session_scope() as s:
        return len(list(s.scalars(select(NegotiationRow.id).where(
            NegotiationRow.shipment_id == shipment_id, NegotiationRow.carrier_id == carrier_id))))


def audit(actor: str, action: str, subject: str, detail: dict) -> None:
    with session_scope() as s:
        s.add(AuditRow(ts=utcnow(), actor=actor, action=action, subject=subject, detail=detail))


def list_audit(limit: int = 100) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(AuditRow).order_by(AuditRow.id.desc()).limit(limit))
        return [{"ts": r.ts, "actor": r.actor, "action": r.action, "subject": r.subject, "detail": r.detail} for r in rows]


def record_llm_usage(run_id: str | None, task_type: str, model: str, reason: str, prompt_tokens: int = 0,
                     completion_tokens: int = 0, cost_usd: float = 0.0, cache_hit: bool = False) -> None:
    with session_scope() as s:
        s.add(LlmUsageRow(ts=utcnow(), run_id=run_id, task_type=task_type, model=model, reason=reason,
                          prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cost_usd=cost_usd,
                          cache_hit=cache_hit))


def llm_usage_rows(limit: int = 500) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(LlmUsageRow).order_by(LlmUsageRow.id.desc()).limit(limit))
        return [{"ts": r.ts, "run_id": r.run_id, "task_type": r.task_type, "model": r.model, "reason": r.reason,
                 "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
                 "cost_usd": r.cost_usd, "cache_hit": r.cache_hit} for r in rows]
