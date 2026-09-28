"""Structured output of the Shipment Status Analyst."""

from __future__ import annotations

from pydantic import BaseModel


class StatusBriefing(BaseModel):
    headline: str
    details: str
