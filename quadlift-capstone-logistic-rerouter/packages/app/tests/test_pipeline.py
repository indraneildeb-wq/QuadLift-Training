from oceanbridge.core import repository as repo
from oceanbridge.flow.reroute_flow import optimize_what_if, run_pipeline
from oceanbridge.runtime import execute_approval, reject_approval
from oceanbridge.models import ApprovalStatus, ShipmentStatus


def test_quiet_world_does_nothing(fresh_db):
    r = run_pipeline()
    assert r["disruptions"] == 0 and r["at_risk"] == 0
    assert not r["auto_executed"] and not r["queued_for_approval"]


def test_end_to_end_rotterdam_strike(fresh_db):
    r = run_pipeline("rotterdam_strike")
    assert r["disruptions"] >= 1 and r["at_risk"] > 0
    assert r["auto_executed"], "expected low-cost, low-value reroutes to auto-execute"
    assert r["queued_for_approval"], "expected high-value / high-cost reroutes to need sign-off"
    assert not r["errors"]

    # Every auto-executed PO satisfied the policy.
    for ex in r["auto_executed"]:
        po = repo.get_po(ex["po_id"])
        s = repo.get_shipment(ex["shipment_id"])
        assert po.auto_executed and s.status == ShipmentStatus.REROUTED

    # Every queued shipment is parked and has a pending approval.
    for q in r["queued_for_approval"]:
        assert repo.get_shipment(q["shipment_id"]).status == ShipmentStatus.PENDING_APPROVAL
        assert repo.get_approval(q["approval_id"]).status == ApprovalStatus.PENDING

    # Manager approves one and rejects another.
    a, b = r["queued_for_approval"][:2]
    done = execute_approval(a["approval_id"], "Test LOM", "approved in test")
    assert repo.get_po(done["po_id"]).approval_id == a["approval_id"]
    assert repo.get_approval(a["approval_id"]).status == ApprovalStatus.EXECUTED
    reject_approval(b["approval_id"], "Test LOM", "too expensive")
    assert repo.get_shipment(b["shipment_id"]).status in (ShipmentStatus.BOOKED, ShipmentStatus.IN_TRANSIT)

    # The rejected shipment is re-evaluated next cycle and the decision comes from the cache.
    r2 = run_pipeline()
    tiers = {d["shipment_id"]: d["cache_tier"] for d in r2["decisions"]}
    assert tiers.get(b["shipment_id"], "").startswith(("L1", "L3"))


def test_what_if_uses_semantic_tier(disrupted):
    first = optimize_what_if("SHP-1004")
    assert first["cache_tier"] == "miss"
    assert optimize_what_if("SHP-1004")["cache_tier"] == "L1-exact"
    w = repo.get_shipment("SHP-1004").weight_kg
    near = optimize_what_if("SHP-1004", {"weight_kg": w * 1.04})
    assert near["cache_tier"] == "L2-semantic"
    assert near["selected_option_id"] and near["hitl"]["policy"]
