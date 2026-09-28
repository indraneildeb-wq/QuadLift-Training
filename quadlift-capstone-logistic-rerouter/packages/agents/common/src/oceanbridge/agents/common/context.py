"""Per-run context shared by the flow and agent backends: tracing and LLM usage accounting."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from oceanbridge.core import repository as repo
from oceanbridge.core.repository import utcnow
from oceanbridge.llm.router import ModelRouter, RoutingDecision


@dataclass
class RunContext:
    run_id: str
    backend: str
    router: ModelRouter = field(default_factory=ModelRouter)
    events: list[dict] = field(default_factory=list)

    def trace(self, step: str, message: str, **extra) -> None:
        event = {"ts": utcnow().isoformat(timespec="seconds"), "step": step, "message": message,
                 **{k: v for k, v in extra.items() if v is not None}}
        self.events.append(event)
        repo.append_trace(self.run_id, event)

    def usage(self, decision: RoutingDecision, prompt_tokens: int = 0, completion_tokens: int = 0,
              cache_hit: bool = False, executed_model: str | None = None) -> float:
        cost = 0.0 if cache_hit else self.router.estimate_cost(decision.model, prompt_tokens, completion_tokens)
        repo.record_llm_usage(self.run_id, decision.task_type.value, executed_model or decision.model,
                              decision.reason, prompt_tokens, completion_tokens, cost, cache_hit)
        return cost


class Timer:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = round((time.perf_counter() - self.t0) * 1000, 1)
