"""Downstream Listener -- standalone stand-in for the real Route & Capacity
Optimization Agent's ingress. Receives published DisruptionIntelligenceReport
payloads, validates them against the shared schema, and prints/logs them.
Runs as its own process/terminal so published reports never interleave with
whatever the Test Harness's interactive prompt is doing.
"""
import argparse
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator

ROOT_DIR = Path(__file__).resolve().parent.parent


def resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (ROOT_DIR / p)


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def write_audit(audit_path: Path, entry: dict) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def quarantine_payload(payload, audit_id: str) -> str:
    q_dir = ROOT_DIR / "quarantine"
    q_dir.mkdir(parents=True, exist_ok=True)
    q_path = q_dir / f"{audit_id}.json"
    with open(q_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return str(q_path.relative_to(ROOT_DIR))


def build_app(server_path: str, validator: Draft202012Validator, audit_path: Path, reject_on_invalid: bool) -> FastAPI:
    app = FastAPI(title="Downstream Listener")

    @app.post(server_path)
    async def receive_report(request: Request):
        raw_body = await request.body()
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            audit_id = str(uuid.uuid4())
            write_audit(audit_path, {
                "audit_id": audit_id,
                "event_type": "schema_violation",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "correlation_id": None,
                "component": "listener",
                "detail": {
                    "boundary": "intelligence_report",
                    "schema_id": "schemas/disruption_intelligence_report.schema.json",
                    "validation_errors": [{"path": "$", "message": f"invalid JSON body: {exc}"}],
                    "payload_ref": None,
                    "source_name": "agent",
                },
                "outcome": "rejected",
            })
            logging.warning("Rejected non-JSON body: %s", exc)
            return JSONResponse(status_code=422, content={"error": "invalid_json", "details": str(exc)})

        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
        if errors:
            audit_id = str(uuid.uuid4())
            payload_ref = quarantine_payload(payload, audit_id)
            error_list = [
                {"path": "$" + "".join(f"[{p!r}]" for p in e.path), "message": e.message}
                for e in errors
            ]
            write_audit(audit_path, {
                "audit_id": audit_id,
                "event_type": "schema_violation",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "correlation_id": payload.get("report_id") if isinstance(payload, dict) else None,
                "component": "listener",
                "detail": {
                    "boundary": "intelligence_report",
                    "schema_id": "schemas/disruption_intelligence_report.schema.json",
                    "validation_errors": error_list,
                    "payload_ref": payload_ref,
                    "source_name": "agent",
                },
                "outcome": "rejected",
            })
            logging.warning("REJECTED invalid report (report_id=%s): %s",
                             payload.get("report_id") if isinstance(payload, dict) else None, error_list)
            if reject_on_invalid:
                return JSONResponse(status_code=422, content={
                    "error": "schema_validation_failed", "details": error_list, "quarantined_at": payload_ref,
                })

        print("\n" + "=" * 72)
        print(f"PUBLISHED REPORT   report_id={payload.get('report_id')}   version={payload.get('version')}")
        print(f"  status={payload.get('status')}   trigger_reason={payload.get('trigger_reason')}")
        print(f"  disruption_type={payload.get('disruption_type')}   severity={payload.get('severity')}   "
              f"confidence={payload.get('confidence_score')}")
        print(f"  recommended_attention={payload.get('recommended_attention')}")
        print(f"  narrative_summary: {payload.get('narrative_summary')}")
        print(json.dumps(payload, indent=2))
        print("=" * 72 + "\n")

        write_audit(audit_path, {
            "audit_id": str(uuid.uuid4()),
            "event_type": "downstream_call",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "correlation_id": payload.get("report_id"),
            "component": "listener",
            "detail": {"endpoint": server_path, "http_status": 200, "retry_count": 0},
            "outcome": "success",
        })
        return JSONResponse(status_code=200, content={"received": True, "report_id": payload.get("report_id")})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Downstream Listener -- mock ingress for published Disruption Intelligence Reports")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--path", default=None)
    parser.add_argument("--config", default=str(ROOT_DIR / "config" / "listener_config.yaml"))
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args()

    config = load_config(resolve(args.config))
    host = args.host or config["server"]["host"]
    port = args.port or config["server"]["port"]
    server_path = args.path or config["server"]["path"]
    log_level = (args.log_level or config["logging"]["level"]).upper()

    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    schema_path = resolve(config["validation"]["schema_file"])
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)
    validator = Draft202012Validator(schema)

    audit_path = resolve(config["audit"]["file_path"])
    reject_on_invalid = config["validation"]["reject_on_invalid"]

    app = build_app(server_path, validator, audit_path, reject_on_invalid)

    logging.info("Downstream Listener starting on %s:%s%s", host, port, server_path)
    uvicorn.run(app, host=host, port=port, log_level=log_level.lower())


if __name__ == "__main__":
    main()
