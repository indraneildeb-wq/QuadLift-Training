"""Structured output of the Vendor Negotiation & PO Agent."""

from __future__ import annotations

from pydantic import BaseModel, Field


class NegotiationSummary(BaseModel):
    po_id: str | None = Field(description="PO id returned by issue_purchase_order, null if it failed")
    final_amount_usd: float
    rounds: int
    carrier_message: str = Field(description="Message sent to the carrier confirming the agreed terms")
