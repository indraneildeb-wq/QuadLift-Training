"""Disruption Monitoring Agent -- entrypoint. Wires ingestion validation,
normalization (LLM + cache), fusion (+ MCP impact assessment), confidence
scoring, and the trigger/publish controller behind a small FastAPI surface:
POST /ingest, GET /reports/{id}, POST /config/downstream, GET /health,
GET /metrics.
"""
import argparse
import logging

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from agent_core.audit import AuditLogger
from agent_core.cache import SemanticCache
from agent_core.fusion import FusionEngine
from agent_core.llm_client import MockLLMClient, RealLLMClient
from agent_core.mcp_client import MockMCPClient, RealMCPClient
from agent_core.metrics import MetricsRegistry
from agent_core.normalization import NormalizationLayer
from agent_core.paths import ROOT_DIR, resolve
from agent_core.pricing import PricingTable
from agent_core.publisher import Publisher
from agent_core.quarantine import QuarantineStore
from agent_core.schemas import SchemaValidationError
from agent_core.schemas import validate as validate_schema
from agent_core.scoring import ConfidenceScorer
from agent_core.settings import load_agent_config
from agent_core.state_store import StateStore
from agent_core.trigger import TriggerController


def build_app(config: dict) -> FastAPI:
    audit = AuditLogger(resolve(config["audit"]["file_path"]))
    quarantine = QuarantineStore(resolve(config["quarantine"]["directory"]))
    metrics = MetricsRegistry()
    pricing = PricingTable(config["observability"]["cost_pricing_table_file"])

    ss_cfg = config["state_store"]["redis"]
    state_store = StateStore(
        ss_cfg["host"], ss_cfg["port"], ss_cfg["db"], ss_cfg["key_prefix"], ss_cfg["connection_timeout_seconds"]
    )

    cache_cfg = config["semantic_cache"]
    exact_ttl = next(t["ttl_seconds"] for t in cache_cfg["tiers"] if t["name"] == "exact_match")
    semantic_tier = next(t for t in cache_cfg["tiers"] if t["name"] == "semantic")
    cache = SemanticCache(
        cache_cfg["redis"]["host"], cache_cfg["redis"]["port"], cache_cfg["redis"]["db"], cache_cfg["redis"]["key_prefix"],
        exact_ttl, semantic_tier["ttl_seconds"], semantic_tier["similarity_threshold"], enabled=cache_cfg["enabled"],
    )

    if config["llm"]["mode"] == "mock":
        llm_client = MockLLMClient()
    else:
        llm_client = RealLLMClient(config["llm"]["real"]["api_key_env_var"])

    if config["mcp"]["mode"] == "mock":
        mcp_client = MockMCPClient(config["mcp"]["mock"]["fixture_file"])
    else:
        mcp_client = RealMCPClient(config["mcp"]["real"]["endpoint"], config["mcp"]["real"]["auth_ref"])

    normalization = NormalizationLayer(llm_client, cache, pricing, audit, metrics, config["model_routing"])
    scorer = ConfidenceScorer(config["confidence_scoring"])
    fusion = FusionEngine(state_store, mcp_client, scorer, config["trigger"]["confidence_threshold"], audit, metrics)
    publisher = Publisher(
        config["downstream"]["url"], config["downstream"]["timeout_seconds"], config["downstream"]["retry"], audit, metrics
    )
    trigger = TriggerController(
        state_store, scorer, publisher,
        config["trigger"]["confidence_threshold"], config["trigger"]["debounce_seconds"],
        config["trigger"]["max_hold_seconds"], config["trigger"]["reconciliation_interval_seconds"], audit,
    )
    trigger.start()

    app = FastAPI(title="Disruption Monitoring Agent")

    def _reject(boundary: str, source_name, correlation_id, payload, exc: SchemaValidationError):
        audit_id = audit.log(
            event_type="schema_violation", correlation_id=correlation_id, component="ingestion",
            detail={
                "boundary": boundary, "schema_id": exc.schema_id,
                "validation_errors": exc.errors, "source_name": source_name,
            },
            outcome="rejected",
        )
        metrics.inc("schema_violations_total", {"boundary": boundary, "source_name": source_name or "unknown"})
        payload_ref = quarantine.store(audit_id, payload)
        logging.warning("Schema violation rejected (boundary=%s): %s", boundary, exc.errors)
        status_code = 400 if boundary == "raw_event" else 422
        return JSONResponse(status_code=status_code, content={
            "error": "schema_validation_failed", "boundary": boundary,
            "details": exc.errors, "quarantined_at": payload_ref,
        })

    @app.post("/ingest")
    async def ingest(request: Request):
        raw_event = await request.json()

        try:
            validate_schema(raw_event, "raw_event")
        except SchemaValidationError as exc:
            source_name = raw_event.get("source_name") if isinstance(raw_event, dict) else None
            raw_event_id = raw_event.get("raw_event_id") if isinstance(raw_event, dict) else None
            return _reject("raw_event", source_name, raw_event_id, raw_event, exc)

        try:
            signal, usage = normalization.build_signal(raw_event)
        except SchemaValidationError as exc:
            return _reject(exc.boundary, raw_event.get("source_name"), raw_event["raw_event_id"], raw_event, exc)

        try:
            validate_schema(signal, "disruption_signal")
        except SchemaValidationError as exc:
            return _reject("disruption_signal", raw_event.get("source_name"), raw_event["raw_event_id"], signal, exc)

        report = fusion.ingest_signal(signal, usage)
        report = trigger.evaluate_after_ingest(report)

        try:
            validate_schema(report, "intelligence_report")
        except SchemaValidationError as exc:
            return _reject("intelligence_report", "agent", report.get("report_id"), report, exc)

        return JSONResponse(status_code=202, content={
            "accepted": True, "report_id": report["report_id"], "current_snapshot": report,
        })

    @app.get("/reports/{report_id}")
    async def get_report(report_id: str):
        report = state_store.get_report(report_id)
        if not report:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        return report

    @app.post("/config/downstream")
    async def set_downstream(request: Request):
        body = await request.json()
        url = body.get("url")
        if not url:
            return JSONResponse(status_code=400, content={"error": "missing url"})
        publisher.url = url
        return {"ok": True, "url": url}

    @app.get("/health")
    async def health():
        try:
            state_store.ping()
            redis_ok = True
        except Exception:
            redis_ok = False
        return {"status": "ok" if redis_ok else "degraded", "redis": redis_ok}

    @app.get("/metrics")
    async def metrics_endpoint():
        return PlainTextResponse(metrics.render_prometheus())

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Disruption Monitoring Agent")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--downstream-url", default=None)
    parser.add_argument("--mock-mcp", default=None, help="Path to a shipment fixture file; forces MockMCPClient")
    parser.add_argument("--mock-llm", action="store_true", help="Force MockLLMClient regardless of config")
    parser.add_argument("--config", default=str(ROOT_DIR / "config" / "agent_config.yaml"))
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args()

    config = load_agent_config(args.config)

    if args.downstream_url:
        config["downstream"]["url"] = args.downstream_url
    if args.mock_mcp:
        config["mcp"]["mode"] = "mock"
        config["mcp"]["mock"]["fixture_file"] = args.mock_mcp
    if args.mock_llm:
        config["llm"]["mode"] = "mock"

    host = args.host or config["server"]["host"]
    port = args.port or config["server"]["port"]
    log_level = (args.log_level or config["logging"]["level"]).upper()
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    app = build_app(config)
    logging.info(
        "Disruption Agent starting on %s:%s (mcp.mode=%s, llm.mode=%s, downstream=%s)",
        host, port, config["mcp"]["mode"], config["llm"]["mode"], config["downstream"]["url"],
    )
    uvicorn.run(app, host=host, port=port, log_level=log_level.lower())


if __name__ == "__main__":
    main()
