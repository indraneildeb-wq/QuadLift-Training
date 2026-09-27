"""Bayesian log-odds confidence scoring, per the design:

    log_odds = logit(p0) + sum_i [ source_reliability_i * severity_weight_i
                                    * extraction_confidence_i * diversity_factor_i
                                    * time_decay_i ]
    confidence_score = sigmoid(log_odds)

Recomputed from the full contributing_signals list on every call (cheap given
report sizes in this domain) so time_decay always reflects "now", which is
what lets a quiet report's confidence fall even with no new signals.
"""
import math
from datetime import datetime, timezone


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class ConfidenceScorer:
    def __init__(self, config: dict):
        self.prior_p0 = config["prior_p0"]
        self.decay_lambda = config["decay_lambda"]
        self.severity_weight = config["severity_weight"]
        self.source_reliability_defaults = config["source_reliability_defaults"]

    def _prior_log_odds(self) -> float:
        p0 = self.prior_p0
        return math.log(p0 / (1 - p0))

    def score(self, contributing_signals: list, now: datetime = None) -> float:
        now = now or datetime.now(timezone.utc)
        log_odds = self._prior_log_odds()

        type_counts: dict = {}
        for signal in contributing_signals:
            signal_type = signal["signal_type"]
            type_counts[signal_type] = type_counts.get(signal_type, 0) + 1
            k = type_counts[signal_type]

            source_reliability = self.source_reliability_defaults.get(
                signal_type, self.source_reliability_defaults.get("unknown", 0.4)
            )
            severity_weight = self.severity_weight[signal["severity"]]
            extraction_confidence = signal["confidence_of_extraction"]
            diversity_factor = 1 / math.sqrt(k)

            event_time = _parse_iso(signal["event_time"])
            delta_seconds = max(0.0, (now - event_time).total_seconds())
            time_decay = math.exp(-self.decay_lambda * delta_seconds)

            log_odds += source_reliability * severity_weight * extraction_confidence * diversity_factor * time_decay

        return 1 / (1 + math.exp(-log_odds))
