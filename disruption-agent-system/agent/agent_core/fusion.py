"""Correlation & Fusion Engine: folds a new DisruptionSignal into an existing
open report (matched by correlation_key) or opens a new one, recomputes
aggregate severity/geography/impact/confidence, and persists via the
Redis-backed StateStore. Status/trigger_reason are intentionally left
untouched here -- TriggerController owns those transitions.
"""
import uuid
from datetime import datetime, timezone

from .mcp_client import MCPError

SEVERITY_ORDER = {"low": 0, "moderate": 1, "high": 2, "severe": 3}


class FusionEngine:
    def __init__(self, state_store, mcp_client, scorer, confidence_threshold: float, audit, metrics):
        self.state_store = state_store
        self.mcp_client = mcp_client
        self.scorer = scorer
        self.confidence_threshold = confidence_threshold
        self.audit = audit
        self.metrics = metrics

    @staticmethod
    def correlation_key(signal: dict) -> str:
        geo = signal["geography"]
        if geo.get("unlocode"):
            return f"port:{geo['unlocode']}"
        return f"region:{geo['region'].strip().lower()}"

    def _query_shipments(self, ports: list, vessels: list, correlation_id) -> list:
        results = {}
        for port in ports:
            self._safe_query(results, {"port_unlocode": port}, correlation_id)
        for vessel in vessels:
            self._safe_query(results, {"vessel_imo": vessel}, correlation_id)
        return list(results.values())

    def _safe_query(self, results: dict, filter_: dict, correlation_id) -> None:
        try:
            resp = self.mcp_client.get_shipment_status(filter=filter_)
            for s in resp["shipments"]:
                results[s["shipment_id"]] = s
            self.audit.log(
                event_type="downstream_call", correlation_id=correlation_id, component="fusion.mcp_client",
                detail={"endpoint": "get_shipment_status", "filter": filter_, "match_count": len(resp["shipments"])},
                outcome="success",
            )
        except MCPError as exc:
            self.audit.log(
                event_type="downstream_call", correlation_id=correlation_id, component="fusion.mcp_client",
                detail={"endpoint": "get_shipment_status", "filter": filter_, "error_type": exc.error_type, "error": exc.detail},
                outcome="failure",
            )
            self.metrics.inc("downstream_calls_total", {"endpoint": "get_shipment_status", "status": "error"})

    def _new_report(self, signal: dict, correlation_key: str, now: datetime) -> dict:
        return {
            "report_id": str(uuid.uuid4()),
            "version": 0,
            "status": "monitoring",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "disruption_type": signal["signal_type"],
            "severity": signal["severity"],
            "confidence_score": 0.0,
            "trigger_reason": None,
            "geography": {"region": signal["geography"]["region"], "affected_ports": [], "affected_lanes": []},
            "impact_assessment": {
                "affected_shipment_ids": [], "affected_po_ids": [],
                "estimated_cargo_value_usd": 0, "estimated_delay_days": 0,
            },
            "contributing_signals": [],
            "narrative_summary": "",
            "recommended_attention": "monitor",
            "metadata": {
                "correlation_key": correlation_key,
                "revision_history_ref": str(uuid.uuid4()),
                "llm_calls_count": 0,
                "cache_hits_count": 0,
                "total_tokens": 0,
                "processing_cost_usd": 0.0,
            },
        }

    def ingest_signal(self, signal: dict, usage: dict) -> dict:
        now = datetime.now(timezone.utc)
        key = self.correlation_key(signal)
        existing_id = self.state_store.find_open_report_id(key)
        report = self.state_store.get_report(existing_id) if existing_id else None
        if report is None:
            report = self._new_report(signal, key, now)

        report["contributing_signals"].append(signal)
        report["version"] += 1
        report["updated_at"] = now.isoformat()

        distinct_types = {s["signal_type"] for s in report["contributing_signals"]}
        report["disruption_type"] = next(iter(distinct_types)) if len(distinct_types) == 1 else "composite"
        report["severity"] = max(
            (s["severity"] for s in report["contributing_signals"]), key=lambda sev: SEVERITY_ORDER[sev]
        )

        ports = sorted({s["geography"]["unlocode"] for s in report["contributing_signals"] if s["geography"].get("unlocode")})
        lanes = sorted({lane for s in report["contributing_signals"] for lane in s["entities"].get("lanes", [])})
        vessels = sorted({v for s in report["contributing_signals"] for v in s["entities"].get("vessels", [])})
        report["geography"]["affected_ports"] = ports
        report["geography"]["affected_lanes"] = lanes

        shipments = self._query_shipments(ports, vessels, signal["raw_event_id"])
        report["impact_assessment"] = {
            "affected_shipment_ids": sorted({s["shipment_id"] for s in shipments}),
            "affected_po_ids": sorted({s["po_id"] for s in shipments}),
            "estimated_cargo_value_usd": sum(s["cargo"]["value_usd"] for s in shipments),
            "estimated_delay_days": 2 if shipments else 0,
        }

        report["confidence_score"] = self.scorer.score(report["contributing_signals"], now=now)
        report["recommended_attention"] = "escalate" if report["confidence_score"] >= self.confidence_threshold else "monitor"
        report["narrative_summary"] = " | ".join(s["summary"] for s in report["contributing_signals"][-3:])

        meta = report["metadata"]
        meta["llm_calls_count"] += usage.get("llm_calls", 0)
        meta["cache_hits_count"] += usage.get("cache_hits", 0)
        meta["total_tokens"] += usage.get("total_tokens", 0)
        meta["processing_cost_usd"] = round(meta["processing_cost_usd"] + usage.get("cost_usd", 0.0), 6)

        self.state_store.save_report(report)
        self.state_store.link_correlation(key, report["report_id"])

        return report
