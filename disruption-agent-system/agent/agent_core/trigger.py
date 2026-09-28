"""Trigger/Publish Controller. Two paths:

  A) evaluate_after_ingest(): called synchronously right after Fusion, on every
     /ingest. If confidence_score >= threshold, publish immediately
     (trigger_reason=confidence_threshold). If a report is already published
     and a later corroborating signal keeps it at/above threshold, this
     republishes the new version too -- the report stays in sync with
     downstream as long as it keeps corroborating.

  B) the background reconciliation loop: ticks every
     reconciliation_interval_seconds, and for every report still in
     "monitoring" status, recomputes confidence (so idle reports reflect
     time_decay) and checks whether the debounce or max-hold deadline has
     passed -- both derived from created_at/updated_at, no separate timer
     store needed. If so, publishes what's known (trigger_reason=timer_expiry
     if still under threshold, confidence_threshold if it crossed since the
     last check).
"""
import logging
import threading
from datetime import datetime, timezone, timedelta


class TriggerController:
    def __init__(self, state_store, scorer, publisher, confidence_threshold: float,
                 debounce_seconds: int, max_hold_seconds: int, reconciliation_interval_seconds: int, audit):
        self.state_store = state_store
        self.scorer = scorer
        self.publisher = publisher
        self.confidence_threshold = confidence_threshold
        self.debounce_seconds = debounce_seconds
        self.max_hold_seconds = max_hold_seconds
        self.reconciliation_interval_seconds = reconciliation_interval_seconds
        self.audit = audit
        self._stop = threading.Event()

    def evaluate_after_ingest(self, report: dict) -> dict:
        if report["confidence_score"] >= self.confidence_threshold:
            report["status"] = "published"
            report["trigger_reason"] = "confidence_threshold"
            self.state_store.save_report(report)
            self.publisher.publish(report)
        return report

    def _reconcile_once(self) -> None:
        now = datetime.now(timezone.utc)
        for report_id in self.state_store.all_report_ids():
            report = self.state_store.get_report(report_id)
            if not report or report["status"] != "monitoring":
                continue

            created_at = datetime.fromisoformat(report["created_at"].replace("Z", "+00:00"))
            updated_at = datetime.fromisoformat(report["updated_at"].replace("Z", "+00:00"))
            debounce_deadline = updated_at + timedelta(seconds=self.debounce_seconds)
            max_hold_deadline = created_at + timedelta(seconds=self.max_hold_seconds)

            report["confidence_score"] = self.scorer.score(report["contributing_signals"], now=now)

            if now >= debounce_deadline or now >= max_hold_deadline:
                report["status"] = "published"
                report["trigger_reason"] = (
                    "confidence_threshold" if report["confidence_score"] >= self.confidence_threshold else "timer_expiry"
                )
                self.state_store.save_report(report)
                self.publisher.publish(report)
            else:
                self.state_store.save_report(report)

    def start(self) -> None:
        def _loop():
            while not self._stop.is_set():
                try:
                    self._reconcile_once()
                except Exception:
                    logging.exception("Reconciliation loop error")
                self._stop.wait(self.reconciliation_interval_seconds)

        threading.Thread(target=_loop, daemon=True, name="trigger-reconciliation").start()

    def stop(self) -> None:
        self._stop.set()
