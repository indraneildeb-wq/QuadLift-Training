"""Chat Assistant agent: agentic tool calling over the MCP server and local tools, for the chat feature.

    agent.yaml   role and system prompt
    tools.py     tool registry (MCP-backed and local), with read / write / confirm effects
    agent.py     run_llm (OpenAI function-calling loop) and run_offline (planner + templates)
    intents.py   offline planner that maps messages to tool calls using session memory
    memory.py    history window, running summary, working memory (entities, pending action)
"""

from oceanbridge.agents.chat_assistant.agent import TurnOutput, is_analytical, run_llm, run_offline
from oceanbridge.agents.chat_assistant.tools import TOOLS, ToolCallRecord, ToolContext, catalog

__all__ = ["run_llm", "run_offline", "is_analytical", "TurnOutput", "TOOLS", "ToolCallRecord", "ToolContext",
           "catalog"]
