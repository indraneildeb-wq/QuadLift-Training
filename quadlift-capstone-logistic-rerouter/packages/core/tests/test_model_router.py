from oceanbridge.llm.router import ComplexitySignals, ModelRouter, TaskType


def test_status_and_monitoring_use_light_model():
    r = ModelRouter()
    assert r.select(TaskType.STATUS_CHECK).model == "gpt-4o-mini"
    assert r.select(TaskType.DISRUPTION_MONITORING, ComplexitySignals(n_disruptions=5)).model == "gpt-4o-mini"


def test_negotiation_uses_heavy_model():
    assert ModelRouter().select(TaskType.NEGOTIATION).model == "gpt-4o"


def test_route_optimization_depends_on_complexity():
    r = ModelRouter()
    simple = r.select(TaskType.ROUTE_OPTIMIZATION, ComplexitySignals(n_candidates=2, cargo_value_usd=50_000))
    complex_ = r.select(TaskType.ROUTE_OPTIMIZATION, ComplexitySignals(
        n_candidates=6, multimodal_candidates=2, cargo_value_usd=2_000_000, n_disruptions=2))
    assert simple.model == "gpt-4o-mini" and simple.tier == "light"
    assert complex_.model == "gpt-4o" and complex_.score >= 3


def test_escalation_forces_heavy():
    d = ModelRouter().select(TaskType.ROUTE_OPTIMIZATION, ComplexitySignals(), escalate=True)
    assert d.model == "gpt-4o" and "escalated" in d.reason


def test_cost_estimate():
    assert ModelRouter().estimate_cost("gpt-4o", 1_000_000, 0) == 2.5
