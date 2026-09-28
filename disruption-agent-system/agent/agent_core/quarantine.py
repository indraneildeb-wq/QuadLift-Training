import json
from pathlib import Path


class QuarantineStore:
    """Rejected payloads from any schema-validation boundary land here, keyed by
    audit_id, instead of being silently dropped -- generalizes the event-streaming
    dead-letter-queue pattern to every boundary in the system."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def store(self, audit_id: str, payload) -> str:
        path = self.directory / f"{audit_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        return str(path)
