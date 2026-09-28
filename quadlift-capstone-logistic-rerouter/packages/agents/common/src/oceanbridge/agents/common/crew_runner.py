"""Runs one CrewAI agent + task from an agent folder's agent.yaml, with the LLM chosen by the router."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from crewai import LLM, Agent, Crew, Process, Task

from oceanbridge.agents.common.base import AgentCall
from oceanbridge.agents.common.context import RunContext
from oceanbridge.llm.router import RoutingDecision

os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")


def load_config(agent_dir: Path) -> dict:
    """agent.yaml holds `agent` (role, goal, backstory) and `task` (description, expected_output)."""
    return yaml.safe_load((agent_dir / "agent.yaml").read_text(encoding="utf-8"))


def build_llm(decision: RoutingDecision, temperature: float = 0.1) -> LLM:
    """CrewAI LLM for the model the router chose."""
    return LLM(model=decision.model, temperature=temperature)


def run_crew(ctx: RunContext, config: dict, fields: dict, output_model, decision: RoutingDecision,
             tools: list) -> AgentCall:
    a, t = config["agent"], config["task"]
    agent = Agent(role=a["role"], goal=a["goal"], backstory=a["backstory"], llm=build_llm(decision),
                  tools=tools, allow_delegation=False, verbose=False, max_iter=8)
    # Fields are formatted in here (not via kickoff inputs) so JSON braces in the data are left alone.
    task = Task(description=t["description"].format(**fields), expected_output=t["expected_output"],
                agent=agent, output_pydantic=output_model)
    out = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False).kickoff()
    usage = getattr(out, "token_usage", None)
    result = out.pydantic
    if result is None and out.raw:
        result = output_model.model_validate_json(out.raw)
    return AgentCall(result, decision.model, int(getattr(usage, "prompt_tokens", 0) or 0),
                     int(getattr(usage, "completion_tokens", 0) or 0))
