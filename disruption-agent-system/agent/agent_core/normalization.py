"""Normalization Layer: RawEvent -> DisruptionSignal. Wraps the LLM call with
the semantic cache, model-routing escalation, and the observability/audit
instrumentation the design calls for (every LLM call and cache request is
logged, regardless of hit/miss/outcome).
"""
import uuid
from datetime import datetime, timezone


class NormalizationLayer:
    def __init__(self, llm_client, cache, pricing, audit, metrics, model_routing: dict):
        self.llm_client = llm_client
        self.cache = cache
        self.pricing = pricing
        self.audit = audit
        self.metrics = metrics
        self.model_routing = model_routing

    def build_signal(self, raw_event: dict) -> tuple:
        payload = raw_event["payload"]
        text = payload["raw_text"]
        declared_category = payload.get("declared_category")
        correlation_id = raw_event["raw_event_id"]

        usage = {"llm_calls": 0, "cache_hits": 0, "total_tokens": 0, "cost_usd": 0.0}

        cached, hit = self.cache.get(text)
        self.metrics.inc("cache_lookups_total", {"cache_tier": "combined"})
        self.audit.log(
            event_type="cache_request", correlation_id=correlation_id, component="normalization",
            detail={"cache_tier": "exact_or_semantic", "hit": hit}, outcome="success",
        )

        if hit:
            self.metrics.inc("cache_hits_total", {"cache_tier": "combined"})
            usage["cache_hits"] += 1
            extraction = cached
        else:
            model = self.model_routing["extraction_model"]
            extraction = self._call_llm(text, declared_category, model, correlation_id)
            self._accrue_usage(usage, extraction)

            if extraction["confidence_of_extraction"] < self.model_routing["escalation_confidence_threshold"]:
                escalated_model = self.model_routing["complex_reasoning_model"]
                extraction = self._call_llm(text, declared_category, escalated_model, correlation_id)
                self._accrue_usage(usage, extraction)

            self.cache.set(text, extraction)

        now = datetime.now(timezone.utc)
        signal = {
            "signal_id": str(uuid.uuid4()),
            "raw_event_id": raw_event["raw_event_id"],
            "signal_type": extraction["signal_type"],
            "severity": extraction["severity"],
            "confidence_of_extraction": extraction["confidence_of_extraction"],
            "geography": extraction["geography"],
            "entities": extraction["entities"],
            "summary": extraction["summary"],
            "source_excerpt": text[:1000],
            "event_time": raw_event["received_at"],
            "ingested_at": now.isoformat(),
        }
        return signal, usage

    def _accrue_usage(self, usage: dict, extraction: dict) -> None:
        usage["llm_calls"] += 1
        usage["total_tokens"] += extraction["usage"]["prompt_tokens"] + extraction["usage"]["completion_tokens"]
        usage["cost_usd"] += self.pricing.cost_usd(
            extraction["model"], extraction["usage"]["prompt_tokens"], extraction["usage"]["completion_tokens"]
        )

    def _call_llm(self, text: str, declared_category, model: str, correlation_id) -> dict:
        start = datetime.now(timezone.utc)
        try:
            result = self.llm_client.classify_and_extract(text, declared_category, model)
        except Exception as exc:
            self.metrics.inc("llm_calls_total", {"model": model, "outcome": "failure"})
            self.audit.log(
                event_type="llm_call", correlation_id=correlation_id, component="normalization",
                detail={"model": model, "purpose": "classification_and_extraction", "error": str(exc)},
                outcome="failure",
            )
            raise

        latency_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        self.metrics.inc("llm_calls_total", {"model": model, "outcome": "success"})
        self.metrics.inc("llm_tokens_total", {"model": model, "token_type": "prompt"}, result["usage"]["prompt_tokens"])
        self.metrics.inc("llm_tokens_total", {"model": model, "token_type": "completion"}, result["usage"]["completion_tokens"])
        cost = self.pricing.cost_usd(result["model"], result["usage"]["prompt_tokens"], result["usage"]["completion_tokens"])
        self.metrics.inc("llm_cost_usd_total", {"model": model}, cost)
        self.audit.log(
            event_type="llm_call", correlation_id=correlation_id, component="normalization",
            detail={
                "model": model, "purpose": "classification_and_extraction",
                "prompt_tokens": result["usage"]["prompt_tokens"],
                "completion_tokens": result["usage"]["completion_tokens"],
                "cost_usd": cost,
            },
            outcome="success", latency_ms=latency_ms,
        )
        return result
