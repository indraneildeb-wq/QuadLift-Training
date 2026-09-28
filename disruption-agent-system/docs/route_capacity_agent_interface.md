# Disruption Intelligence Report — Consumer Contract

**Context**: This is what the Disruption Monitoring Agent POSTs to your
configured REST endpoint whenever a disruption's confidence crosses
threshold, or its debounce timer expires. Your endpoint should accept `POST`
with this JSON body and return `200` on success.

## Delivery semantics
- **Idempotency**: every payload carries `report_id` + `version` (an
  incrementing int). The *same* `report_id` can arrive multiple times as new
  corroborating signals come in — treat a new delivery as an **update**, not
  a duplicate, and use `version` to detect if you've already processed a
  newer one.
- **Retries**: on transient failure (timeout, 5xx) we retry with backoff (a
  few attempts). If your endpoint returns `422` (schema-rejected), we do
  **not** retry — that's treated as a hard failure worth investigating, not a
  transient one, so please only return `422` for genuine payload problems.
- **`trigger_reason`**: `"confidence_threshold"` means the disruption is now
  confident enough to act on; `"timer_expiry"` means the debounce window
  closed without reaching that confidence — worth treating these differently
  downstream (e.g., `timer_expiry` reports may warrant a lighter-touch
  response since the signal is weaker).
- **`recommended_attention`**: `"escalate"` vs `"monitor"` — a simple
  pre-computed signal for whether this report crossed the actionable-
  confidence threshold at the time it was sent.
- **`disruption_type`** can be `"composite"` when multiple signal types
  (weather + port_status + vessel_tracking + geopolitical) corroborate the
  same disruption — don't assume it's always a single category.

## JSON Schema

The full, authoritative schema lives at
[`schemas/disruption_intelligence_report.schema.json`](../schemas/disruption_intelligence_report.schema.json)
in this repo. Key top-level fields:

| Field | Type | Notes |
|---|---|---|
| `report_id` | uuid | stable identity across versions of the same report |
| `version` | integer ≥ 1 | incremented on every update |
| `status` | `"monitoring" \| "published" \| "closed"` | will always be `"published"` on delivery to you |
| `created_at` / `updated_at` | ISO 8601 date-time | |
| `disruption_type` | `"weather" \| "port_status" \| "vessel_tracking" \| "geopolitical" \| "composite"` | |
| `severity` | `"low" \| "moderate" \| "high" \| "severe"` | |
| `confidence_score` | number 0–1 | |
| `trigger_reason` | `"confidence_threshold" \| "timer_expiry" \| null` | |
| `geography` | `{region, affected_ports[], affected_lanes[]}` | ports are UN/LOCODE |
| `impact_assessment` | `{affected_shipment_ids[], affected_po_ids[], estimated_cargo_value_usd, estimated_delay_days}` | |
| `contributing_signals` | array (≥1) | the full corroboration trail — see below |
| `narrative_summary` | string | human-readable synthesis (only the **last 3** contributing signals — see note below) |
| `recommended_attention` | `"monitor" \| "escalate"` | |
| `metadata` | `{correlation_key, revision_history_ref, llm_calls_count, cache_hits_count, total_tokens, processing_cost_usd}` | |

Each entry in `contributing_signals` carries: `signal_id`, `raw_event_id`,
`signal_type`, `severity`, `confidence_of_extraction`, `geography`,
`entities` (`{vessels[], ports[], lanes[]}`), `summary`, `source_excerpt`,
`event_time`, `ingested_at`.

> **Note**: `narrative_summary` only keeps the last 3 contributing signals'
> text (by design, to stay short). If you need the complete corroboration
> trail, read `contributing_signals` directly rather than the narrative
> string.

## Real sample

Captured live from the running system — a Busan typhoon scenario that
accumulated 6 corroborating signals and crossed the confidence threshold:

```json
{
  "report_id": "bbbd9bb5-56e0-4d09-8c7a-7e47b552cd1c",
  "version": 6,
  "status": "published",
  "created_at": "2026-09-27T14:31:15.297213+00:00",
  "updated_at": "2026-09-27T14:31:39.484060+00:00",
  "disruption_type": "composite",
  "severity": "severe",
  "confidence_score": 0.9164208370004462,
  "trigger_reason": "confidence_threshold",
  "geography": {
    "region": "Busan, South Korea",
    "affected_ports": ["KRPUS"],
    "affected_lanes": []
  },
  "impact_assessment": {
    "affected_shipment_ids": ["SHP-2026-004821", "SHP-2026-004977"],
    "affected_po_ids": ["PO-88213", "PO-88240"],
    "estimated_cargo_value_usd": 227000,
    "estimated_delay_days": 2
  },
  "contributing_signals": [
    {
      "signal_id": "9ecb3ed9-b39a-405a-9392-01d75c74926b",
      "raw_event_id": "d048b403-c02c-446a-9435-e250a2fbeee3",
      "signal_type": "weather",
      "severity": "high",
      "confidence_of_extraction": 0.85,
      "geography": { "region": "Busan, South Korea", "unlocode": "KRPUS", "coordinates": null },
      "entities": { "vessels": [], "ports": ["KRPUS"], "lanes": [] },
      "summary": "Typhoon approaching Busan, expected landfall in 36 hours, category 3",
      "source_excerpt": "Typhoon approaching Busan, expected landfall in 36 hours, category 3",
      "event_time": "2026-09-27T14:31:13.171812+00:00",
      "ingested_at": "2026-09-27T14:31:15.296662+00:00"
    },
    {
      "signal_id": "e78f1991-3ac8-4b00-86e0-6e00ef3bb935",
      "raw_event_id": "85364bb8-b38c-41bb-abbe-35315fa3a04d",
      "signal_type": "port_status",
      "severity": "moderate",
      "confidence_of_extraction": 0.85,
      "geography": { "region": "Busan, South Korea", "unlocode": "KRPUS", "coordinates": null },
      "entities": { "vessels": [], "ports": ["KRPUS"], "lanes": [] },
      "summary": "Port of Busan issuing advisory, reduced crane ops starting tomorrow",
      "source_excerpt": "Port of Busan issuing advisory, reduced crane ops starting tomorrow",
      "event_time": "2026-09-27T14:31:15.306092+00:00",
      "ingested_at": "2026-09-27T14:31:15.315441+00:00"
    },
    {
      "signal_id": "92401f05-3e83-4b94-b153-1356d2707499",
      "raw_event_id": "d20816ef-1b47-4e19-9318-5b0e5221e74e",
      "signal_type": "port_status",
      "severity": "high",
      "confidence_of_extraction": 0.65,
      "geography": { "region": "Busan, South Korea", "unlocode": "KRPUS", "coordinates": null },
      "entities": { "vessels": [], "ports": ["KRPUS"], "lanes": [] },
      "summary": "Reuters: Busan port authority warns of 48hr closure risk due to storm",
      "source_excerpt": "Reuters: Busan port authority warns of 48hr closure risk due to storm",
      "event_time": "2026-09-27T14:31:15.324330+00:00",
      "ingested_at": "2026-09-27T14:31:15.335802+00:00"
    },
    {
      "signal_id": "5501daff-2289-4439-898b-fc7a52317db8",
      "raw_event_id": "4134ccb4-42a9-4b1e-b38b-76d80b346872",
      "signal_type": "vessel_tracking",
      "severity": "moderate",
      "confidence_of_extraction": 0.85,
      "geography": { "region": "Busan, South Korea", "unlocode": "KRPUS", "coordinates": null },
      "entities": { "vessels": ["MV Evergreen Star showing reduced"], "ports": ["KRPUS"], "lanes": [] },
      "summary": "MV Evergreen Star showing reduced speed approaching Busan anchorage",
      "source_excerpt": "MV Evergreen Star showing reduced speed approaching Busan anchorage",
      "event_time": "2026-09-27T14:31:15.965907+00:00",
      "ingested_at": "2026-09-27T14:31:16.030205+00:00"
    },
    {
      "signal_id": "65dfe880-2f7e-46cc-b47b-ab185c011097",
      "raw_event_id": "6e08a682-4d81-4046-9a67-89539ce58a2c",
      "signal_type": "port_status",
      "severity": "high",
      "confidence_of_extraction": 0.85,
      "geography": { "region": "Busan, South Korea", "unlocode": "KRPUS", "coordinates": null },
      "entities": { "vessels": [], "ports": ["KRPUS"], "lanes": [] },
      "summary": "Busan port authority declares full closure of container terminal 4 effective immediately",
      "source_excerpt": "Busan port authority declares full closure of container terminal 4 effective immediately",
      "event_time": "2026-09-27T14:31:31.349275+00:00",
      "ingested_at": "2026-09-27T14:31:31.394826+00:00"
    },
    {
      "signal_id": "21cea1d5-a0b7-419b-ac75-9659ca0650f2",
      "raw_event_id": "8af51c13-e556-4c15-b49b-dbb711cfcdf6",
      "signal_type": "weather",
      "severity": "severe",
      "confidence_of_extraction": 0.85,
      "geography": { "region": "Busan, South Korea", "unlocode": "KRPUS", "coordinates": null },
      "entities": { "vessels": [], "ports": ["KRPUS"], "lanes": [] },
      "summary": "Typhoon upgraded to severe category 4, extreme conditions expected at Busan",
      "source_excerpt": "Typhoon upgraded to severe category 4, extreme conditions expected at Busan",
      "event_time": "2026-09-27T14:31:39.436417+00:00",
      "ingested_at": "2026-09-27T14:31:39.483406+00:00"
    }
  ],
  "narrative_summary": "MV Evergreen Star showing reduced speed approaching Busan anchorage | Busan port authority declares full closure of container terminal 4 effective immediately | Typhoon upgraded to severe category 4, extreme conditions expected at Busan",
  "recommended_attention": "escalate",
  "metadata": {
    "correlation_key": "port:KRPUS",
    "revision_history_ref": "651185fe-0bca-4b50-9f71-cb4c1b60b112",
    "llm_calls_count": 6,
    "cache_hits_count": 0,
    "total_tokens": 304,
    "processing_cost_usd": 0.0
  }
}
```
