import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


class AuditLogger:
    """Append-only JSONL audit trail. Every llm_call, cache_request,
    downstream_call, and schema_violation is written here unconditionally --
    this is separate from the application logger and is never sampled."""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, event_type: str, correlation_id, component: str, detail: dict,
            outcome: str, latency_ms: float = 0.0) -> str:
        audit_id = str(uuid.uuid4())
        entry = {
            "audit_id": audit_id,
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "correlation_id": correlation_id,
            "component": component,
            "detail": detail,
            "outcome": outcome,
            "latency_ms": latency_ms,
        }
        with self._lock:
            with open(self.file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        return audit_id
