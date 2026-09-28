"""Human-in-the-loop (HITL) package.

    gate.py       the policy: auto-execute only if cost increase < limit AND cargo value < limit
    guard.py      in-tool enforcement run by issue_purchase_order before committing a PO
    approvals.py  the approval workflow: request, approve & execute, reject
    errors.py     PolicyViolation
"""

from oceanbridge.hitl.errors import PolicyViolation

__all__ = ["PolicyViolation"]
