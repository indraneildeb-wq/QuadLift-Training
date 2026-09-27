"""MCPClient adapter for get_shipment_status. MockMCPClient loads the fixture
file and filters in-memory; RealMCPClient is a thin placeholder REST client for
the real Supply Chain Core MCP gateway. The Fusion Engine calls
get_shipment_status(...) identically either way, and both paths run through
the same mcp_response schema validation -- fixture drift is caught the same
way a malformed real response would be.
"""
import json
import time
from abc import ABC, abstractmethod
from typing import Optional

from .paths import resolve
from .schemas import validate as validate_schema


class MCPError(Exception):
    def __init__(self, error_type: str, detail: str):
        self.error_type = error_type
        self.detail = detail
        super().__init__(f"{error_type}: {detail}")


class MCPClient(ABC):
    @abstractmethod
    def get_shipment_status(self, shipment_id: Optional[str] = None, filter: Optional[dict] = None) -> dict:
        raise NotImplementedError


class MockMCPClient(MCPClient):
    def __init__(self, fixture_file: str):
        with open(resolve(fixture_file), "r", encoding="utf-8") as f:
            data = json.load(f)
        self._shipments = data.get("shipments", [])
        self._simulated_errors = data.get("simulated_errors", [])

    def _matching_simulated_error(self, shipment_id, filter):
        for rule in self._simulated_errors:
            match = rule.get("match", {})
            if shipment_id is not None and match.get("shipment_id") == shipment_id:
                return rule
            if filter is not None and match.get("filter"):
                rule_filter = match["filter"]
                if rule_filter.items() <= filter.items():
                    return rule
        return None

    def get_shipment_status(self, shipment_id: Optional[str] = None, filter: Optional[dict] = None) -> dict:
        rule = self._matching_simulated_error(shipment_id, filter)
        if rule:
            if rule["error_type"] == "timeout":
                time.sleep(rule.get("delay_ms", 0) / 1000)
                raise MCPError("timeout", f"simulated timeout after {rule.get('delay_ms')}ms")
            raise MCPError(rule["error_type"], f"simulated {rule.get('http_status')} response")

        if shipment_id:
            matches = [s for s in self._shipments if s["shipment_id"] == shipment_id]
        elif filter:
            matches = [s for s in self._shipments if self._matches_filter(s, filter)]
        else:
            matches = []

        response = {"shipments": matches}
        validate_schema(response, "mcp_response")
        return response

    @staticmethod
    def _matches_filter(shipment: dict, filter: dict) -> bool:
        route = shipment["route"]
        if filter.get("port_unlocode"):
            if filter["port_unlocode"] not in (
                route.get("origin_port_unlocode"),
                route.get("destination_port_unlocode"),
                route.get("current_leg_port_unlocode"),
            ):
                return False
        if filter.get("vessel_imo") and filter["vessel_imo"] != route.get("vessel_imo"):
            return False
        if filter.get("lane") and filter["lane"] != route.get("lane"):
            return False
        return True


class RealMCPClient(MCPClient):
    """Placeholder REST client for the real Supply Chain Core MCP gateway.
    Not exercised while --mock-mcp is in effect; the interface is what matters
    here so the Fusion Engine never needs to change when this is wired up."""

    def __init__(self, endpoint: str, auth_ref: str, timeout_seconds: int = 5):
        import httpx

        self._client = httpx.Client(base_url=endpoint, timeout=timeout_seconds)
        self._auth_ref = auth_ref  # resolved to a real credential outside this class

    def get_shipment_status(self, shipment_id: Optional[str] = None, filter: Optional[dict] = None) -> dict:
        import httpx

        body = {"shipment_id": shipment_id} if shipment_id else {"filter": filter}
        try:
            resp = self._client.post("/tools/get_shipment_status", json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise MCPError("http_error", str(exc))
        response = resp.json()
        validate_schema(response, "mcp_response")
        return response
