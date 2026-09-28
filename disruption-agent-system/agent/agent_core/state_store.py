import json
from typing import Optional

import redis


class StateStore:
    """Redis-backed store for open/published reports and the correlation index
    used to fold a new signal into an existing report. Debounce/max-hold deadlines
    are deliberately NOT stored separately -- they're always derivable from a
    report's created_at/updated_at, so the TriggerController's reconciliation
    loop can recompute them on every tick without a second timer mechanism."""

    def __init__(self, host: str, port: int, db: int, key_prefix: str, connection_timeout_seconds: int = 5):
        self._redis = redis.Redis(host=host, port=port, db=db,
                                   socket_timeout=connection_timeout_seconds, decode_responses=True)
        self._prefix = key_prefix

    def _report_key(self, report_id: str) -> str:
        return f"{self._prefix}report:{report_id}"

    def _correlation_key(self, correlation_key: str) -> str:
        return f"{self._prefix}correlation:{correlation_key}"

    def get_report(self, report_id: str) -> Optional[dict]:
        raw = self._redis.get(self._report_key(report_id))
        return json.loads(raw) if raw else None

    def save_report(self, report: dict) -> None:
        self._redis.set(self._report_key(report["report_id"]), json.dumps(report))

    def find_open_report_id(self, correlation_key: str) -> Optional[str]:
        return self._redis.get(self._correlation_key(correlation_key))

    def link_correlation(self, correlation_key: str, report_id: str) -> None:
        self._redis.set(self._correlation_key(correlation_key), report_id)

    def all_report_ids(self) -> list:
        prefix = self._report_key("")
        return [k[len(prefix):] for k in self._redis.scan_iter(match=f"{prefix}*")]

    def ping(self) -> bool:
        return self._redis.ping()
