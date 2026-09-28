"""Talks to the real MCP server over stdio, exactly as the CrewAI agents do."""

import asyncio
import json

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from oceanbridge.mcp_servers import SERVERS, stdio_params


async def _session_calls(db_file):
    params = stdio_params("supply_chain_core", env={"OB_DB_PATH": str(db_file), "OB_LLM_MODE": "offline"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            status = await session.call_tool("get_shipment_status", {"shipment_id": "SHP-1017"})
            quote = await session.call_tool("calculate_freight_cost", {
                "origin": "CNSHA", "destination": "NLRTM", "mode": "sea", "weight_kg": 18000, "volume_cbm": 60})
            alts = await session.call_tool("list_route_alternatives", {"shipment_id": "SHP-1017"})
            alt_payload = json.loads(alts.content[0].text)
            opt = next(o for o in alt_payload["options"] if o["option_id"] != "STAY")
            po = await session.call_tool("issue_purchase_order", {
                "shipment_id": "SHP-1017", "option_id": opt["option_id"], "amount_usd": opt["total_cost_usd"],
                "sla_json": json.dumps({"guaranteed_transit_days": 30, "late_penalty_pct_per_day": 1,
                                        "max_penalty_pct": 10, "reserved_capacity": "x"})})
            return tools, [json.loads(r.content[0].text) for r in (status, quote, po)]


def test_mcp_tools_and_hitl_enforcement(disrupted):
    tools, (status, quote, po) = asyncio.run(_session_calls(disrupted))
    assert {"get_shipment_status", "calculate_freight_cost", "issue_purchase_order"} <= tools
    assert tools == set(SERVERS["supply_chain_core"].tools)  # registry matches what the server exposes
    assert status["ok"] and status["shipment"]["id"] == "SHP-1017"
    assert quote["ok"] and quote["cost_usd"] > 0
    # SHP-1017 carries $10M+ of cargo: the MCP tool itself must refuse without sign-off.
    assert po["ok"] is False and po["error"] == "PolicyViolation"
