"""Publishes a validated DisruptionIntelligenceReport to the configured
downstream REST endpoint. Every attempt is audited. A 422 (schema rejection by
the downstream side) is never retried -- retrying a malformed payload can't fix
it; only transient failures (timeouts, 5xx) get the backoff/retry treatment.
"""
import logging
import time as time_module

import httpx


class Publisher:
    def __init__(self, url: str, timeout_seconds: int, retry_config: dict, audit, metrics):
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.max_attempts = retry_config["max_attempts"]
        self.backoff_seconds = retry_config["backoff_seconds"]
        self.audit = audit
        self.metrics = metrics

    def publish(self, report: dict) -> bool:
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = httpx.post(self.url, json=report, timeout=self.timeout_seconds)
                outcome = "success" if resp.status_code < 300 else "failure"
                self.audit.log(
                    event_type="downstream_call", correlation_id=report["report_id"], component="publisher",
                    detail={"endpoint": self.url, "http_status": resp.status_code, "retry_count": attempt - 1},
                    outcome=outcome,
                )
                self.metrics.inc("downstream_calls_total", {"endpoint": self.url, "status": str(resp.status_code)})

                if resp.status_code == 422:
                    logging.error("Downstream rejected report %s as schema-invalid; not retrying", report["report_id"])
                    return False
                if resp.status_code < 300:
                    return True
            except httpx.HTTPError as exc:
                self.audit.log(
                    event_type="downstream_call", correlation_id=report["report_id"], component="publisher",
                    detail={"endpoint": self.url, "error": str(exc), "retry_count": attempt - 1},
                    outcome="failure",
                )
                logging.warning("Publish attempt %s failed: %s", attempt, exc)

            if attempt < self.max_attempts:
                delay = self.backoff_seconds[min(attempt - 1, len(self.backoff_seconds) - 1)]
                time_module.sleep(delay)

        logging.error("Exhausted retries publishing report %s", report["report_id"])
        return False
