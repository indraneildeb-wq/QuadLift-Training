"""Test Harness -- pure REST client. Prompts for weather / port status / vessel
tracking / news feed input (interactive, or replayed from a --script scenario
file), wraps each entry as a RawEvent per the declared_category mapping, POSTs
to the Agent's /ingest, and prints the returned current_snapshot. No embedded
server here -- the Downstream Listener process is where published reports
actually show up.
"""
import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent

SLOT_TO_DECLARED_CATEGORY = {
    "weather": "weather",
    "port_status": "port_status",
    "vessel_tracking": "vessel_tracking",
    "news_feed": "news",
}

SLOT_PROMPTS = {
    "weather": "Weather update (blank to skip): ",
    "port_status": "Port status update (blank to skip): ",
    "vessel_tracking": "Vessel tracking update (blank to skip): ",
    "news_feed": "News feed item (blank to skip): ",
}


def resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (ROOT_DIR / p)


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_raw_event(slot: str, text: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "raw_event_id": str(uuid.uuid4()),
        "source_type": "manual_test_input",
        "source_name": f"harness_{slot}",
        "received_at": now,
        "payload": {
            "declared_category": SLOT_TO_DECLARED_CATEGORY[slot],
            "raw_text": text,
            "entered_at": now,
        },
    }


def print_snapshot(snapshot: dict, previous, show_diff: bool, fmt: str) -> None:
    if fmt == "raw_json":
        print(json.dumps(snapshot, indent=2))
        return

    print(f"\n--- report {snapshot['report_id']}  v{snapshot['version']}  status={snapshot['status']} ---")
    print(f"  disruption_type: {snapshot['disruption_type']}   severity: {snapshot['severity']}")
    print(f"  confidence_score: {snapshot['confidence_score']:.3f}   recommended_attention: {snapshot['recommended_attention']}")
    if snapshot.get("trigger_reason"):
        print(f"  >>> TRIGGERED -- trigger_reason={snapshot['trigger_reason']}")
    print(f"  affected_ports: {snapshot['geography']['affected_ports']}")
    print(f"  affected_shipments: {snapshot['impact_assessment']['affected_shipment_ids']}  "
          f"cargo_value_usd: {snapshot['impact_assessment']['estimated_cargo_value_usd']}")
    print(f"  narrative: {snapshot['narrative_summary']}")

    if show_diff and previous and previous.get("report_id") == snapshot.get("report_id"):
        delta = snapshot["confidence_score"] - previous["confidence_score"]
        print(f"  confidence change since last turn: {delta:+.3f}")
    print("-" * 64)


def send_event(client: httpx.Client, ingest_url: str, raw_event: dict, session_log):
    resp = client.post(ingest_url, json=raw_event)
    if resp.status_code >= 400:
        print(f"  ! Agent rejected input: {resp.status_code} {resp.text}")
        return None
    body = resp.json()
    if session_log:
        session_log.write(json.dumps({"raw_event": raw_event, "response": body}) + "\n")
        session_log.flush()
    return body.get("current_snapshot")


def run_script(client, ingest_url, script_path, config, session_log):
    with open(script_path, "r", encoding="utf-8") as f:
        entries = yaml.safe_load(f)
    previous = None
    for entry in entries:
        slot, text = entry["slot"], entry["text"]
        print(f"\n[{slot}] {text}")
        raw_event = build_raw_event(slot, text)
        snapshot = send_event(client, ingest_url, raw_event, session_log)
        if snapshot:
            print_snapshot(snapshot, previous, config["output"]["show_diff"], config["output"]["format"])
            previous = snapshot


def run_interactive(client, ingest_url, config, session_log):
    previous = None
    order = config["prompts"]["order"]
    print("Disruption Monitoring Agent -- Test Harness")
    print("Enter natural-language input for each category. Blank input skips that category.")
    print("Ctrl+C to exit.\n")
    while True:
        any_input = False
        for slot in order:
            try:
                text = input(SLOT_PROMPTS[slot]).strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                return
            if not text:
                continue
            any_input = True
            raw_event = build_raw_event(slot, text)
            snapshot = send_event(client, ingest_url, raw_event, session_log)
            if snapshot:
                print_snapshot(snapshot, previous, config["output"]["show_diff"], config["output"]["format"])
                previous = snapshot
        if not any_input:
            print("No input entered this round -- Ctrl+C to exit, or provide input to continue.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Harness -- interactive client for the Disruption Monitoring Agent")
    parser.add_argument("--agent-url", default=None)
    parser.add_argument("--config", default=str(ROOT_DIR / "config" / "harness_config.yaml"))
    parser.add_argument("--session-log", default=None)
    parser.add_argument("--script", default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    config = load_config(resolve(args.config))
    base_url = args.agent_url or config["agent"]["base_url"]
    ingest_url = base_url.rstrip("/") + config["agent"]["ingest_path"]

    session_log_path = args.session_log or config["session"]["log_path"]
    session_log = None
    if session_log_path:
        p = resolve(session_log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        session_log = open(p, "a", encoding="utf-8")

    with httpx.Client(timeout=config["agent"]["request_timeout_seconds"]) as client:
        if args.script:
            run_script(client, ingest_url, resolve(args.script), config, session_log)
        else:
            run_interactive(client, ingest_url, config, session_log)

    if session_log:
        session_log.close()


if __name__ == "__main__":
    main()
