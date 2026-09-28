import pytest

from oceanbridge.core import repository as repo
from oceanbridge.core import service
from oceanbridge.hitl import PolicyViolation, gate
from oceanbridge.models import ApprovalRequest, ApprovalStatus


def _pick(shipment_id: str, auto: bool):
    s = repo.get_shipment(shipment_id)
    for o in service.list_route_alternatives(shipment_id):
        if o.option_id != "STAY" and gate.evaluate(o.cost_increase_pct, s.cargo_value_usd).auto_execute is auto:
            return s, o
    pytest.skip(f"no {'auto' if auto else 'sign-off'} option for {shipment_id}")


def test_auto_po_issued_and_supersedes_original(disrupted):
    s, o = _pick("SHP-1004", auto=True)
    po = service.issue_purchase_order(s.id, o.option_id, o.total_cost_usd, service.requested_sla(o))
    assert po.auto_executed and po.supersedes_po_id == f"PO-{s.id}-ORIG"
    assert repo.get_po(f"PO-{s.id}-ORIG").status == "superseded"
    after = repo.get_shipment(s.id)
    assert after.status.value == "rerouted" and after.po_id == po.po_id


def test_po_refused_without_signoff(disrupted):
    s, o = _pick("SHP-1017", auto=False)  # $10M cargo
    with pytest.raises(PolicyViolation, match="HITL"):
        service.issue_purchase_order(s.id, o.option_id, o.total_cost_usd, service.requested_sla(o))
    assert repo.get_shipment(s.id).po_id == f"PO-{s.id}-ORIG"


def test_po_requires_approved_status_and_single_use(disrupted):
    s, o = _pick("SHP-1017", auto=False)
    appr = ApprovalRequest(id="APR-TEST", run_id="R", shipment_id=s.id, option=o,
                           decision=gate.evaluate(o.cost_increase_pct, s.cargo_value_usd), rationale="",
                           created_at=repo.utcnow())
    repo.insert_approval(appr)
    with pytest.raises(PolicyViolation):  # still pending
        service.issue_purchase_order(s.id, o.option_id, o.total_cost_usd, service.requested_sla(o), "APR-TEST")
    repo.decide_approval("APR-TEST", ApprovalStatus.APPROVED, "LOM")
    po = service.issue_purchase_order(s.id, o.option_id, o.total_cost_usd, service.requested_sla(o), "APR-TEST")
    assert po.approval_id == "APR-TEST" and not po.auto_executed
    with pytest.raises(PolicyViolation, match="already been executed"):
        service.issue_purchase_order(s.id, o.option_id, o.total_cost_usd, service.requested_sla(o), "APR-TEST")


def test_po_amount_cannot_exceed_quote(disrupted):
    s, o = _pick("SHP-1004", auto=True)
    with pytest.raises(PolicyViolation, match="exceeds"):
        service.issue_purchase_order(s.id, o.option_id, o.total_cost_usd * 1.2, service.requested_sla(o))


def test_negotiation_converges_within_three_rounds(disrupted):
    s, o = _pick("SHP-1004", auto=True)
    terms = service.requested_sla(o)
    offer = o.total_cost_usd * 0.8
    for rnd in range(1, 4):
        r = service.submit_sla_proposal(s.id, o.option_id, offer, terms)
        assert r.round == rnd and r.counter_amount_usd <= o.total_cost_usd
        offer, terms = r.counter_amount_usd, r.counter_terms
    with pytest.raises(PolicyViolation, match="Maximum"):
        service.submit_sla_proposal(s.id, o.option_id, offer, terms)
