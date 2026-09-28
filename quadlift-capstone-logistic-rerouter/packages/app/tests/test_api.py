from fastapi.testclient import TestClient

from oceanbridge.api.main import app


def test_api_flow(fresh_db):
    c = TestClient(app)
    assert c.get("/health").json()["backend"] == "offline"
    assert len(c.get("/shipments").json()) == 48
    assert c.post("/scenarios/rotterdam_strike/activate").status_code == 200
    run = c.post("/pipeline/run-sync", json={}).json()
    assert run["at_risk"] > 0

    pending = c.get("/approvals", params={"status": "pending"}).json()
    assert pending
    ok = c.post(f"/approvals/{pending[0]['id']}/approve", json={"approver": "LOM", "comment": "go"})
    assert ok.status_code == 200 and ok.json()["po_id"]
    again = c.post(f"/approvals/{pending[0]['id']}/approve", json={"approver": "LOM"})
    assert again.status_code == 409

    brief = c.get(f"/shipments/{pending[1]['shipment_id']}/briefing").json()
    assert "gpt-4o-mini" in brief["model"]  # status checks routed to the light model

    m = c.get("/metrics").json()
    assert "route_decisions" in m["cache"] and m["purchase_orders"]["reroute_pos"] >= 1
    assert c.get("/approvals/NOPE").status_code == 404
