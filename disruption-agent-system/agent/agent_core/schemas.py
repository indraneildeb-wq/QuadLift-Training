"""Schema-validation gates for every boundary the Agent touches. Reject + log is
enforced by the caller (agent_main._reject / normalization / mcp_client) -- this
module only loads the shared schema files once and exposes a single validate()
call per boundary, raising SchemaValidationError with structured details on
failure so callers can build the schema_violation audit entry without
re-deriving anything.
"""
import json

from jsonschema import Draft202012Validator

from .paths import resolve


class SchemaValidationError(Exception):
    def __init__(self, boundary: str, schema_id: str, errors: list):
        self.boundary = boundary
        self.schema_id = schema_id
        self.errors = errors
        super().__init__(f"schema validation failed for boundary={boundary}: {errors}")


def _load_schema(path_str: str) -> dict:
    with open(resolve(path_str), "r", encoding="utf-8") as f:
        return json.load(f)


_RAW_EVENT_SCHEMA = _load_schema("schemas/raw_event.schema.json")
_REPORT_SCHEMA = _load_schema("schemas/disruption_intelligence_report.schema.json")
_MCP_RESPONSE_SCHEMA = _load_schema("schemas/get_shipment_status_response.schema.json")

# DisruptionSignal is a $defs entry inside the report schema -- build a standalone
# validator for it that still has access to the shared $defs for its own $refs.
_SIGNAL_SCHEMA = {**_REPORT_SCHEMA["$defs"]["DisruptionSignal"], "$defs": _REPORT_SCHEMA["$defs"]}

_VALIDATORS = {
    "raw_event": (Draft202012Validator(_RAW_EVENT_SCHEMA), "schemas/raw_event.schema.json"),
    "disruption_signal": (Draft202012Validator(_SIGNAL_SCHEMA),
                           "schemas/disruption_intelligence_report.schema.json#/$defs/DisruptionSignal"),
    "llm_structured_output": (Draft202012Validator(_SIGNAL_SCHEMA),
                               "schemas/disruption_intelligence_report.schema.json#/$defs/DisruptionSignal"),
    "intelligence_report": (Draft202012Validator(_REPORT_SCHEMA),
                             "schemas/disruption_intelligence_report.schema.json"),
    "mcp_response": (Draft202012Validator(_MCP_RESPONSE_SCHEMA),
                      "schemas/get_shipment_status_response.schema.json"),
}


def validate(payload, boundary: str) -> None:
    validator, schema_id = _VALIDATORS[boundary]
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        raise SchemaValidationError(
            boundary=boundary,
            schema_id=schema_id,
            errors=[
                {"path": "$" + "".join(f"[{p!r}]" for p in e.path), "message": e.message}
                for e in errors
            ],
        )
