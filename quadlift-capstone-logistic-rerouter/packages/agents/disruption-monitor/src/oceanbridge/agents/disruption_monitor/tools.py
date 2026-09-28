"""Local CrewAI tools for the external feeds (the Supply Chain Core tools come from the MCP server)."""

from __future__ import annotations

import json

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from oceanbridge.core import repository as repo
from oceanbridge.feeds import geopolitics, maritime, port_congestion, weather


class _LocationArgs(BaseModel):
    location: str = Field(description="Port UN/LOCODE (e.g. CNSHA) or chokepoint id (SUEZ, PANAMA, MALACCA, "
                                      "BAB_EL_MANDEB, CAPE, GIBRALTAR, HORMUZ)")


class _ShipmentArgs(BaseModel):
    shipment_id: str


class WeatherFeedTool(BaseTool):
    name: str = "marine_weather_feed"
    description: str = "Marine weather at a port or chokepoint: wind (kts), waves (m), storm category, advisories."
    args_schema: type[BaseModel] = _LocationArgs

    def _run(self, location: str) -> str:
        return json.dumps(weather.get_conditions(location))


class PortCongestionTool(BaseTool):
    name: str = "port_congestion_index"
    description: str = "Port congestion index (0-100, >70 congested), vessels waiting, average wait days, yard utilisation."
    args_schema: type[BaseModel] = _LocationArgs

    def _run(self, location: str) -> str:
        return json.dumps(port_congestion.get_index(location))


class MaritimeStatusTool(BaseTool):
    name: str = "maritime_port_status"
    description: str = "AIS-derived port/canal operating status (open, closed, strike, restricted) and vessels holding."
    args_schema: type[BaseModel] = _LocationArgs

    def _run(self, location: str) -> str:
        return json.dumps(maritime.get_port_status(location))


class GeopoliticalRiskTool(BaseTool):
    name: str = "geopolitical_risk_feed"
    description: str = "Security/geopolitical threat level (1-5) and alerts for a location."
    args_schema: type[BaseModel] = _LocationArgs

    def _run(self, location: str) -> str:
        return json.dumps(geopolitics.get_alerts(location))


class VesselTrackingTool(BaseTool):
    name: str = "vessel_tracking"
    description: str = "Live AIS position, speed and voyage progress of the vessel carrying a shipment."
    args_schema: type[BaseModel] = _ShipmentArgs

    def _run(self, shipment_id: str) -> str:
        s = repo.get_shipment(shipment_id)
        return json.dumps(maritime.track_vessel(s) if s else {"error": "unknown shipment"})


def feed_tools() -> list[BaseTool]:
    return [WeatherFeedTool(), PortCongestionTool(), MaritimeStatusTool(), GeopoliticalRiskTool(), VesselTrackingTool()]
