# MCP Tool Interface: `get_shipment_status`

**Context**: Called by the Disruption Monitoring Agent to determine which
shipments are affected by a detected disruption (port closure, weather event,
vessel delay, etc.) and their cargo exposure.

## Transport & connection
- Protocol: standard MCP (JSON-RPC), over the **SSE transport** — expose an
  SSE endpoint (conventionally `/sse`) that supports the standard MCP
  handshake (`initialize`) followed by `list_tools` / `call_tool`.
- Auth: a single header carrying an API key. Header name is configurable on
  our side (default assumed: `Authorization`), and **the value is the raw key
  string** — no `Bearer ` prefix is added by our client, so please don't
  require one server-side unless you tell us and we'll adjust.

## Input (tool arguments)

The tool accepts **either** of two argument shapes — not both required
together:

**Mode 1 — direct lookup by ID:**
```json
{ "shipment_id": "SHP-2026-004821" }
```

**Mode 2 — filter-based lookup** (used far more often — the Agent typically
knows a disrupted port/vessel/lane, not a specific shipment ID):
```json
{
  "filter": {
    "port_unlocode": "KRPUS",
    "vessel_imo": "IMO9321483",
    "lane": "Asia-WestCoast"
  }
}
```
All three filter fields are optional and **OR'd together** — any shipment
matching *any* provided field should be returned. Specifically:
- `port_unlocode` matches if it equals the shipment's `origin_port_unlocode`,
  `destination_port_unlocode`, **or** `current_leg_port_unlocode` (i.e. "is
  this port anywhere on the route, including where the shipment currently
  is")
- `vessel_imo` matches the shipment's assigned vessel
- `lane` matches the shipment's trade lane

## Output

Always a single object with a `shipments` array (zero, one, or many matches
— never a bare error for "no matches," just an empty array):

```json
{
  "shipments": [
    {
      "shipment_id": "SHP-2026-004821",
      "po_id": "PO-88213",
      "customer_ref": "OceanBridge-Client-114",
      "cargo": {
        "description": "Electronics components",
        "value_usd": 82000,
        "hs_code": "8471"
      },
      "route": {
        "mode": "sea",
        "carrier": "Maersk",
        "vessel_imo": "IMO9321483",
        "vessel_name": "MV Pacific Horizon",
        "origin_port_unlocode": "CNSHA",
        "destination_port_unlocode": "USLAX",
        "current_leg_port_unlocode": "KRPUS",
        "lane": "Asia-WestCoast"
      },
      "status": "in_transit",
      "eta": "2026-10-03T00:00:00Z",
      "last_updated": "2026-09-25T08:00:00Z"
    }
  ]
}
```

### Field reference

| Field | Type | Notes |
|---|---|---|
| `shipment_id` | string | required |
| `po_id` | string | required |
| `customer_ref` | string \| null | optional |
| `cargo.description` | string | required |
| `cargo.value_usd` | number ≥ 0 | required |
| `cargo.hs_code` | string \| null | optional |
| `route.mode` | `"sea" \| "air" \| "rail"` | required |
| `route.carrier` | string \| null | optional |
| `route.vessel_imo` | string \| null | null for air/rail legs |
| `route.vessel_name` | string \| null | null for air/rail legs |
| `route.origin_port_unlocode` | string | required, UN/LOCODE format (e.g. `CNSHA`) |
| `route.destination_port_unlocode` | string | required, UN/LOCODE format |
| `route.current_leg_port_unlocode` | string \| null | UN/LOCODE format when set |
| `route.lane` | string | required |
| `status` | `"in_transit" \| "at_port" \| "delayed" \| "delivered"` | required |
| `eta` | string | required, ISO 8601 date-time |
| `last_updated` | string | required, ISO 8601 date-time |

No other fields — the response is validated strictly
(`additionalProperties: false`); anything with extra or missing fields is
rejected, logged, and treated as a failed call rather than guessed at.

## Error handling
Report tool-level failures via the standard MCP mechanism (`isError: true` on
the tool result, with a human-readable message in the returned content)
rather than an HTTP error code — the client checks that flag and surfaces the
message, so a clear string there (e.g. `"shipment lookup service
unavailable"`) is all that's needed.

## Canonical schema
The exact same contract as a formal JSON Schema, used to validate every
response we receive: [`schemas/get_shipment_status_response.schema.json`](../schemas/get_shipment_status_response.schema.json).
