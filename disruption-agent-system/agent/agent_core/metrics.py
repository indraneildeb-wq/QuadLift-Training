import threading
from collections import defaultdict


class MetricsRegistry:
    """In-process counters exposed as Prometheus text format via GET /metrics.
    Covers llm_calls_total, llm_tokens_total, cache_lookups/hits_total,
    downstream_calls_total, schema_violations_total -- the observability
    surface called for in the design (cache hits, LLM calls, token count,
    usage cost is derived from llm_tokens_total x the pricing table)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters = defaultdict(float)

    def inc(self, name: str, labels: dict = None, value: float = 1) -> None:
        key = self._key(name, labels or {})
        with self._lock:
            self._counters[key] += value

    @staticmethod
    def _key(name: str, labels: dict) -> str:
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}" if label_str else name

    def render_prometheus(self) -> str:
        with self._lock:
            lines = [f"{key} {value}" for key, value in sorted(self._counters.items())]
        return "\n".join(lines) + "\n"
