"""Human-in-the-loop approval gate.

Rule (OceanBridge policy):
  * Auto-execute when the reroute increases total freight cost by LESS THAN max_cost_increase_pct
    AND the cargo value is LESS THAN max_cargo_value_usd.
  * Anything else (cost increase >= threshold OR cargo value >= threshold) requires
    Logistics Operations Manager sign-off.

This is deliberately plain code: the decision is never delegated to an LLM, and the same
function is re-checked inside the issue_purchase_order tool as a second line of defence.
"""

from __future__ import annotations

from oceanbridge.config import HitlSettings, get_settings
from oceanbridge.models import HitlDecision


def evaluate(cost_increase_pct: float, cargo_value_usd: float, policy: HitlSettings | None = None) -> HitlDecision:
    p = policy or get_settings().hitl
    reasons: list[str] = []
    if cost_increase_pct >= p.max_cost_increase_pct:
        reasons.append(f"Freight cost increase {cost_increase_pct:.2f}% >= {p.max_cost_increase_pct:.2f}% limit")
    if cargo_value_usd >= p.max_cargo_value_usd:
        reasons.append(f"Cargo value ${cargo_value_usd:,.0f} >= ${p.max_cargo_value_usd:,.0f} limit")
    auto = not reasons
    if auto:
        reasons.append(
            f"Cost increase {cost_increase_pct:.2f}% < {p.max_cost_increase_pct:.2f}% and "
            f"cargo value ${cargo_value_usd:,.0f} < ${p.max_cargo_value_usd:,.0f}: auto-execute"
        )
    return HitlDecision(
        auto_execute=auto,
        cost_increase_pct=round(cost_increase_pct, 2),
        cargo_value_usd=cargo_value_usd,
        reasons=reasons,
        policy=f"auto if cost_increase < {p.max_cost_increase_pct}% AND value < ${p.max_cargo_value_usd:,.0f}; "
               f"else {p.approver_role} sign-off",
    )


def cost_increase_pct(new_cost: float, baseline_cost: float) -> float:
    return (new_cost - baseline_cost) / baseline_cost * 100.0
