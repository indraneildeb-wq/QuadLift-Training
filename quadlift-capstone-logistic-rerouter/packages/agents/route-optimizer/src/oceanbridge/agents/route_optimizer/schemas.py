"""Structured output of the Route & Capacity Optimization Agent."""

from __future__ import annotations

from pydantic import BaseModel, Field


class OptimizationChoice(BaseModel):
    selected_option_id: str = Field(description="option_id of the recommended route, or STAY")
    ranked_option_ids: list[str] = Field(description="All option_ids, best first")
    rationale: str = Field(description="Why this option wins on landed cost, lead time, risk and capacity")
