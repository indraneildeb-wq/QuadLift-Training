"""In-tool enforcement: the checks `issue_purchase_order` runs before any money is committed.

The pipeline already applies the gate before negotiating; this guard applies it again at the point
of commit, so an agent or MCP client calling the tool directly cannot skip human sign-off."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from oceanbridge.core import repository as repo
from oceanbridge.hitl import gate
from oceanbridge.hitl.errors import PolicyViolation
from oceanbridge.models import ApprovalRequest, ApprovalStatus, HitlDecision, RouteOption, Shipment


@dataclass
class Authorization:
    option: RouteOption
    decision: HitlDecision
    approval: ApprovalRequest | None


def authorize_purchase_order(shipment: Shipment, option_id: str, amount_usd: float, approval_id: str | None,
                             proposed_option: Callable[[str, str], RouteOption | None],
                             actor: str) -> Authorization:
    """Return the option to commit, or raise PolicyViolation / KeyError.

    Order of checks: approval exists and is unused -> option matches -> not STAY -> amount within the
    quote -> HITL gate (auto-execute, or an APPROVED sign-off for this shipment)."""
    approval = repo.get_approval(approval_id) if approval_id else None
    if approval_id and approval is None:
        raise PolicyViolation(f"Unknown approval {approval_id}")
    if approval and approval.status == ApprovalStatus.EXECUTED:
        raise PolicyViolation(f"Approval {approval_id} has already been executed")

    option = approval.option if approval else proposed_option(shipment.id, option_id)
    if option is None or option.option_id != option_id:
        raise KeyError(f"Option {option_id} not found for {shipment.id}")
    if option.option_id == "STAY":
        raise PolicyViolation("STAY keeps the current booking; no purchase order is needed")
    if amount_usd > option.total_cost_usd + 0.01:
        raise PolicyViolation(f"PO amount ${amount_usd:,.2f} exceeds quoted route cost ${option.total_cost_usd:,.2f}")

    decision = gate.evaluate(gate.cost_increase_pct(amount_usd, shipment.current_cost_usd), shipment.cargo_value_usd)
    if not decision.auto_execute:
        if approval is None:
            repo.audit(actor, "po_refused", shipment.id, {"reasons": decision.reasons, "option_id": option_id})
            raise PolicyViolation("HITL sign-off required: " + "; ".join(decision.reasons))
        if approval.shipment_id != shipment.id or approval.status != ApprovalStatus.APPROVED:
            raise PolicyViolation(f"Approval {approval_id} is not an APPROVED sign-off for {shipment.id}")
    return Authorization(option=option, decision=decision, approval=approval)


def mark_executed(authorization: Authorization) -> None:
    """Approvals are single-use: once a PO is issued under one it can never be used again."""
    if authorization.approval:
        repo.decide_approval(authorization.approval.id, ApprovalStatus.EXECUTED)
