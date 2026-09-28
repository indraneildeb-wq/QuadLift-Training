"""MCPClient adapter for get_shipment_status. MockMCPClient loads the fixture
file and filters in-memory; RealMCPClient talks to an already-running MCP
server over the real protocol (SSE transport + ClientSession handshake +
call_tool). Both share the exact same get_shipment_status(shipment_id, filter)
-> {"shipments": [...]} contract and the same mcp_response schema validation --
the Fusion Engine never knows or cares which one it's using, and fixture drift
is caught the same way a malformed real response would be.
"""
import asyncio
import concurrent.futures
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
    """Real MCP protocol client for an already-running server, over the SSE
    transport: open the transport, wrap it in a ClientSession, initialize()
    (the handshake), then call_tool("get_shipment_status", arguments). The
    tool's declared input/output are identical to the mock's -- {shipment_id}
    or {filter: {port_unlocode, vessel_imo, lane}} in, {"shipments": [...]}
    out -- so the response is validated against the same mcp_response schema
    used for MockMCPClient.

    Each call opens its own SSE connection and re-does the handshake via
    asyncio.run(), rather than keeping a persistent session across calls --
    this is what lets the rest of the codebase (FusionEngine, the /ingest
    handler) stay fully synchronous with no changes. It costs a repeated
    handshake per call; revisit with a long-lived session + background event
    loop if call volume ever makes that overhead matter.

    get_shipment_status() is a plain synchronous method, but it's called from
    inside FastAPI's already-running event loop (the /ingest handler is
    `async def`) -- asyncio.run() refuses to start a second loop on a thread
    that's already running one. So the coroutine is handed to a dedicated
    worker thread (with no event loop of its own) via a ThreadPoolExecutor,
    where asyncio.run() is safe to call; .result() then blocks the calling
    (event-loop) thread until it's done, same as any other synchronous call
    Fusion makes today.
    """

    def __init__(self, endpoint: str, api_key: str, auth_header_name: str = "Authorization",
                 timeout_seconds: float = 10.0):
        self._endpoint = endpoint
        self._headers = {auth_header_name: api_key}
        self._timeout_seconds = timeout_seconds
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="mcp-client")

    def get_shipment_status(self, shipment_id: Optional[str] = None, filter: Optional[dict] = None) -> dict:
        try:
            future = self._executor.submit(asyncio.run, self._call_tool(shipment_id, filter))
            response = future.result(timeout=self._timeout_seconds + 5)
        except MCPError:
            raise
        except Exception as exc:
            raise MCPError("mcp_protocol_error", str(exc))
        validate_schema(response, "mcp_response")
        return response

    async def _call_tool(self, shipment_id: Optional[str], filter_: Optional[dict]) -> dict:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        arguments = {"shipment_id": shipment_id} if shipment_id else {"filter": filter_}

        async with sse_client(self._endpoint, headers=self._headers, timeout=self._timeout_seconds) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_shipment_status", arguments)

        if result.is_error:
            detail = result.content[0].text if result.content else "unknown MCP tool error"
            raise MCPError("tool_error", detail)

        if result.structured_content is not None:
            return result.structured_content

        if result.content and hasattr(result.content[0], "text"):
            return json.loads(result.content[0].text)

        raise MCPError("empty_response", "MCP tool returned no content")
