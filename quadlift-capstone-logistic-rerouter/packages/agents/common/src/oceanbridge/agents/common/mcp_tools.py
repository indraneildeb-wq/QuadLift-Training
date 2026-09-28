"""Loads MCP server tools for the CrewAI agents (one stdio subprocess per server, started on first use)."""

from __future__ import annotations

from oceanbridge.config import get_settings
from oceanbridge.mcp_servers import SERVERS, stdio_params


class MCPToolbox:
    def __init__(self):
        self._adapters: dict[str, object] = {}
        self._tools: dict[str, object] = {}

    def _start(self, server: str) -> None:
        from crewai_tools import MCPServerAdapter

        params = stdio_params(server, env={"OB_DB_PATH": str(get_settings().db_file)})
        adapter = MCPServerAdapter(params, *SERVERS[server].tools)
        self._adapters[server] = adapter
        self._tools.update({t.name: t for t in adapter.tools})

    def get(self, *names: str) -> list:
        """CrewAI tools for the given MCP tool names, starting whichever servers provide them."""
        for server, spec in SERVERS.items():
            if server not in self._adapters and any(n in spec.tools for n in names):
                self._start(server)
        return [self._tools[n] for n in names if n in self._tools]

    def close(self) -> None:
        for adapter in self._adapters.values():
            adapter.stop()
        self._adapters.clear()
        self._tools.clear()
