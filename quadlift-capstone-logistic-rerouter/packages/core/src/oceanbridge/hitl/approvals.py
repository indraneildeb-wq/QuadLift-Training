"""Approval workflow for the Logistics Operations Manager: request, approve & execute, reject.

Executing an approval needs the Vendor Negotiation agent, which lives in another package. It is passed in
as `negotiate` (the app wires it in oceanbridge.runtime.execute_approval), so this package never imports
the agents."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Callable

from oceanbridge.core import repository as repo
from oceanbridge.models import ApprovalRequest, ApprovalStatus, HitlDecision, RouteOption, ShipmentStatus

Negotiator = Callable[[ApprovalRequest], dict]


def request_approval(run_id: str, shipment_id: str, option: RouteOption, alternatives: list[RouteOption],
                     decision: HitlDecision, rationale: str) -> ApprovalRequest:
    """Queue a reroute that exceeded the HITL limits and park the shipment until someone decides."""
    approval = ApprovalRequest(
        id=f"APR-{uuid.uuid4().hex[:8].upper()}", run_id=run_id, shipment_id=shipment_id, option=option,
        alternatives=alternatives, decision=decision, rationale=rationale, created_at=repo.utcnow(),
    )
    repo.insert_approval(approval)
    repo.set_shipment_status(shipment_id, ShipmentStatus.PENDING_APPROVAL)
    repo.audit("hitl-gate", "approval_requested", shipment_id,
               {"approval_id": approval.id, "option": option.label, "reasons": decision.reasons})
    return approval


def _pending(approval_id: str) -> ApprovalRequest:
    approval = repo.get_approval(approval_id)
    if approval is None:
        raise KeyError(approval_id)
    if approval.status != ApprovalStatus.PENDING:
        raise ValueError(f"Approval {approval_id} is {approval.status.value}, not pending")
    return approval


def approve_and_execute(approval_id: str, approver: str, comment: str | None, negotiate: Negotiator) -> dict:
    """Record the manager's sign-off, then run `negotiate(approval)`, which issues the PO under that approval id."""
    approval = _pending(approval_id)
    repo.decide_approval(approval_id, ApprovalStatus.APPROVED, approver, comment)
    repo.audit(approver, "approval_granted", approval.shipment_id, {"approval_id": approval_id, "comment": comment})
    return negotiate(repo.get_approval(approval_id))


def reject_approval(approval_id: str, approver: str, comment: str | None = None) -> dict:
    """Reject: nothing is executed and the shipment returns to its booked / in-transit state."""
    approval = _pending(approval_id)
    repo.decide_approval(approval_id, ApprovalStatus.REJECTED, approver, comment)
    shipment = repo.get_shipment(approval.shipment_id)
    restored = ShipmentStatus.IN_TRANSIT if shipment.departure_date <= date.today() else ShipmentStatus.BOOKED
    repo.set_shipment_status(shipment.id, restored)
    repo.audit(approver, "approval_rejected", shipment.id, {"approval_id": approval_id, "comment": comment})
    return {"approval_id": approval_id, "status": "rejected", "shipment_status": restored.value}
