"""Runtime wiring: the one place where the separately packaged agents are connected to the rest.

    make_backend()        OfflineBackend or CrewAIBackend (composes the four pipeline agents)
    execute_approval()    manager sign-off -> Vendor Negotiation agent -> PO (injected into oceanbridge.hitl)
    reject_approval()     re-exported from oceanbridge.hitl.approvals
"""

from __future__ import annotations

from oceanbridge.config import get_settings
from oceanbridge.hitl.approvals import approve_and_execute, reject_approval


def make_backend():
    from oceanbridge.runtime.backends import CrewAIBackend, OfflineBackend

    return CrewAIBackend() if get_settings().use_crewai else OfflineBackend()


def execute_approval(approval_id: str, approver: str, comment: str | None = None, backend=None) -> dict:
    """Approve, then let the Vendor Negotiation agent negotiate and issue the PO under the approval id."""
    from oceanbridge.agents.common.context import RunContext
    from oceanbridge.agents.vendor_negotiator import negotiate_reroute
    from oceanbridge.core import repository as repo

    backend = backend or make_backend()

    def negotiate(approval) -> dict:
        ctx = RunContext(run_id=approval.run_id, backend=backend.name)
        shipment = repo.get_shipment(approval.shipment_id)
        return negotiate_reroute(backend, ctx, shipment, approval.option, approval_id=approval.id)

    try:
        return approve_and_execute(approval_id, approver, comment, negotiate)
    finally:
        backend.close()


__all__ = ["make_backend", "execute_approval", "reject_approval"]
