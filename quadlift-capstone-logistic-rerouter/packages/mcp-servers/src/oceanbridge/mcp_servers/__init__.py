"""All MCP servers of the system, plus a registry clients use to launch them.

Each server lives in its own subpackage and runs with `python -m oceanbridge.mcp_servers.<name>`.
To add a server: create the subpackage (server.py with a FastMCP instance, __main__.py) and
register it in SERVERS below.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class ServerSpec:
    name: str
    module: str          # runnable with `python -m <module>`
    tools: tuple[str, ...]
    description: str


SERVERS: dict[str, ServerSpec] = {
    "supply_chain_core": ServerSpec(
        name="supply_chain_core",
        module="oceanbridge.mcp_servers.supply_chain_core",
        tools=("get_shipment_status", "calculate_freight_cost", "list_route_alternatives",
               "submit_sla_proposal", "issue_purchase_order"),
        description="Shipments, freight quotes, reroute options, carrier SLA negotiation and HITL-enforced POs",
    ),
}


def stdio_params(name: str, env: dict[str, str] | None = None):
    """StdioServerParameters that start the named server as a subprocess of the current interpreter."""
    from mcp import StdioServerParameters

    spec = SERVERS[name]
    return StdioServerParameters(command=sys.executable, args=["-m", spec.module],
                                 env={**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})})


__all__ = ["SERVERS", "ServerSpec", "stdio_params"]
