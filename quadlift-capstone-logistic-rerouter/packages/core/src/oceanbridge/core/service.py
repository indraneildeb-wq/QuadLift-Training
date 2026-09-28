"""Supply Chain Core API. The MCP server exposes these functions as tools; the offline agents
call them directly. All state changes (POs, reroutes, capacity) go through here."""

from __future__ import annotations

import uuid

from oceanbridge.cache.semantic_cache import CacheQuery, get_cache
from oceanbridge.core import repository as repo
from oceanbridge.core.db import ProposalRow, session_scope
from oceanbridge.core.freight import FreightQuote, capacity_required, teu_units
from oceanbridge.core.freight import calculate_freight_cost as _calc
from oceanbridge.core.reference import CARRIERS_BY_ID
from oceanbridge.core.risk import assess
from oceanbridge.core.routing import RouteEngine
from oceanbridge.feeds import maritime
from oceanbridge.hitl import guard
from oceanbridge.hitl.errors import PolicyViolation
from oceanbridge.models import (
    Mode,
    PurchaseOrder,
    RouteOption,
    Shipment,
    SLAProposalResponse,
    SLATerms,
)

MAX_NEGOTIATION_ROUNDS = 3


# ------------------------------------------------------------------ status
def get_shipment_status(shipment_id: str) -> dict:
    s = repo.get_shipment(shipment_id)
    if s is None:
        raise KeyError(f"Unknown shipment {shipment_id}")
    risk = assess(s, repo.active_disruptions())
    pending = repo.pending_approval_for(shipment_id)
    return {
        "shipment": s.model_dump(mode="json"),
        "tracking": maritime.track_vessel(s),
        "risk": risk.model_dump(mode="json"),
        "purchase_orders": [p.model_dump(mode="json") for p in repo.list_pos(shipment_id)],
        "pending_approval_id": pending.id if pending else None,
    }


# ------------------------------------------------------------------ freight (cached)
def _freight_query(origin, destination, mode, weight_kg, volume_cbm, carrier_id, via) -> CacheQuery:
    mode = Mode(mode)
    units = capacity_required(mode, weight_kg, volume_cbm)
    params = {"o": origin, "d": destination, "m": mode.value, "c": carrier_id, "via": via,
              "w": round(weight_kg, 1), "v": round(volume_cbm, 2)}
    text = (f"{mode.value} freight rate from {origin} to {destination} via {'/'.join(via or []) or 'default'} "
            f"carrier {carrier_id or 'any'} for {weight_kg:.0f} kg {volume_cbm:.1f} cbm")
    # A cached quote is only reusable for the same lane, carrier and chargeable units.
    guard = {"o": origin, "d": destination, "m": mode.value, "c": carrier_id, "via": via,
             "units": units if mode == Mode.AIR else teu_units(weight_kg, volume_cbm)}
    return CacheQuery(params=params, text=text, guard=guard,
                      touchpoints=[origin, destination, *(via or [])])


def calculate_freight_cost(origin: str, destination: str, mode: str, weight_kg: float, volume_cbm: float,
                           carrier_id: str | None = None, via: list[str] | None = None) -> dict:
    """Public quote (what the MCP tool returns): internal cache bookkeeping fields are stripped."""
    d = _freight_lookup(origin, destination, mode, weight_kg, volume_cbm, carrier_id, via)
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _freight_lookup(origin, destination, mode, weight_kg, volume_cbm, carrier_id=None, via=None) -> dict:
    cache = get_cache("freight")
    q = _freight_query(origin, destination, mode, weight_kg, volume_cbm, carrier_id, via)
    hit = cache.get(q)
    if hit:
        return {**hit.payload, "cache_tier": hit.tier}
    quote = _calc(origin, destination, mode, weight_kg, volume_cbm, carrier_id=carrier_id, via=via)
    payload = quote.to_dict()
    # Quote weight-dependent fields (CO2) scale with the actual weight; keep the base weight for rescaling.
    payload["_weight_kg"] = weight_kg
    cache.put(q, payload)
    return {**payload, "cache_tier": "miss"}


def _cached_quote(origin, destination, mode, weight_kg, volume_cbm, carrier_id=None, via=None) -> FreightQuote:
    d = _freight_lookup(origin, destination, Mode(mode).value, weight_kg, volume_cbm, carrier_id, via)
    base_w = d.get("_weight_kg") or weight_kg
    co2 = d["co2_kg"] * (weight_kg / base_w if base_w else 1.0)
    return FreightQuote(origin=d["origin"], destination=d["destination"], mode=d["mode"], carrier_id=d["carrier_id"],
                        via=d["via"], distance_km=d["distance_km"], chargeable_units=d["chargeable_units"],
                        unit=d["unit"], cost_usd=d["cost_usd"], transit_days=d["transit_days"], co2_kg=round(co2, 1))


# ------------------------------------------------------------------ alternatives
def alternatives_for(shipment: Shipment, max_options: int = 8) -> list[RouteOption]:
    """Price alternatives for an arbitrary (possibly hypothetical) shipment without persisting them."""
    engine = RouteEngine(repo.all_capacity(), quote_fn=_cached_quote)
    return engine.alternatives(shipment, repo.active_disruptions(), max_options=max_options)


def list_route_alternatives(shipment_id: str, max_options: int = 8) -> list[RouteOption]:
    s = repo.get_shipment(shipment_id)
    if s is None:
        raise KeyError(f"Unknown shipment {shipment_id}")
    options = alternatives_for(s, max_options)
    with session_scope() as db:
        db.merge(ProposalRow(shipment_id=shipment_id, options=[o.model_dump(mode="json") for o in options],
                             created_at=repo.utcnow()))
    return options


def get_proposed_option(shipment_id: str, option_id: str) -> RouteOption | None:
    with session_scope() as db:
        row = db.get(ProposalRow, shipment_id)
        if row is None:
            return None
        for o in row.options:
            if o["option_id"] == option_id:
                return RouteOption(**o)
    return None


# ------------------------------------------------------------------ negotiation (mock carrier API)
def requested_sla(option: RouteOption) -> SLATerms:
    """OceanBridge's opening SLA ask for a route."""
    carrier = CARRIERS_BY_ID[option.primary_carrier_id]
    unit = carrier.capacity_unit
    return SLATerms(
        guaranteed_transit_days=round(option.transit_days * 1.05, 1),
        late_penalty_pct_per_day=2.0,
        max_penalty_pct=20.0,
        reserved_capacity=f"Priority loading, capacity reserved in {unit}",
        free_demurrage_days=5,
        notes="Disruption reroute - OceanBridge requests guaranteed space and penalty-backed transit",
    )


def submit_sla_proposal(shipment_id: str, option_id: str, offered_amount_usd: float, terms: SLATerms) -> SLAProposalResponse:
    """Simulated carrier negotiation endpoint. The carrier concedes more each round, bounded by its floor."""
    option = get_proposed_option(shipment_id, option_id)
    if option is None:
        raise KeyError(f"No proposed option {option_id} for {shipment_id}; call list_route_alternatives first")
    carrier = CARRIERS_BY_ID[option.primary_carrier_id]
    rnd = repo.negotiation_rounds(shipment_id, carrier.id) + 1
    if rnd > MAX_NEGOTIATION_ROUNDS:
        raise PolicyViolation(f"Maximum of {MAX_NEGOTIATION_ROUNDS} negotiation rounds reached with {carrier.id}")

    primary_cost = sum(leg.cost_usd for leg in option.legs if leg.carrier_id == carrier.id)
    other = option.total_cost_usd - primary_cost
    list_price = option.total_cost_usd
    floor = other + primary_cost * carrier.min_margin
    concession = min(1.0, carrier.flexibility * rnd / 2)
    ask = round(list_price - (list_price - floor) * concession, 2)

    # Carrier's acceptable SLA envelope depends on its reliability.
    max_pen_day = round(0.5 + 1.5 * carrier.reliability, 2)
    max_pen_cap = round(5 + 10 * carrier.reliability, 1)
    min_transit = round(option.transit_days * (1 + (1 - carrier.reliability) * 0.5), 1)
    counter_terms = SLATerms(
        guaranteed_transit_days=max(terms.guaranteed_transit_days, min_transit),
        late_penalty_pct_per_day=min(terms.late_penalty_pct_per_day, max_pen_day),
        max_penalty_pct=min(terms.max_penalty_pct, max_pen_cap),
        reserved_capacity=terms.reserved_capacity,
        free_demurrage_days=min(terms.free_demurrage_days, 4),
        notes=terms.notes,
    )
    terms_ok = counter_terms == terms
    accepted = offered_amount_usd >= ask and terms_ok
    counter_amount = round(max(offered_amount_usd, ask), 2) if not accepted else round(offered_amount_usd, 2)
    msg = (f"{carrier.name}: accepted ${counter_amount:,.2f} with requested SLA." if accepted else
           f"{carrier.name}: round {rnd} counter at ${counter_amount:,.2f}; "
           f"transit guarantee {counter_terms.guaranteed_transit_days}d, penalty {counter_terms.late_penalty_pct_per_day}%/day "
           f"capped at {counter_terms.max_penalty_pct}%.")
    resp = SLAProposalResponse(carrier_id=carrier.id, accepted=accepted, round=rnd, counter_amount_usd=counter_amount,
                               counter_terms=counter_terms, message=msg)
    repo.record_negotiation_round(shipment_id, carrier.id, rnd,
                                  {"amount": offered_amount_usd, "terms": terms.model_dump()},
                                  resp.model_dump(mode="json"))
    return resp


# ------------------------------------------------------------------ purchase orders (HITL-enforced by hitl.guard)
def issue_purchase_order(shipment_id: str, option_id: str, amount_usd: float, sla: SLATerms,
                         approval_id: str | None = None, actor: str = "vendor-negotiation-agent") -> PurchaseOrder:
    shipment = repo.get_shipment(shipment_id)
    if shipment is None:
        raise KeyError(f"Unknown shipment {shipment_id}")
    auth = guard.authorize_purchase_order(shipment, option_id, amount_usd, approval_id, get_proposed_option, actor)
    option, decision = auth.option, auth.decision

    need = capacity_required(CARRIERS_BY_ID[option.primary_carrier_id].mode, shipment.weight_kg, shipment.volume_cbm)
    repo.consume_capacity(option.primary_carrier_id, need)
    po = PurchaseOrder(
        po_id=f"PO-{shipment_id}-{uuid.uuid4().hex[:6].upper()}",
        shipment_id=shipment_id, carrier_id=option.primary_carrier_id, amount_usd=round(amount_usd, 2),
        route_label=option.label, legs=option.legs, sla=sla, approval_id=approval_id,
        supersedes_po_id=shipment.po_id, status="issued", auto_executed=decision.auto_execute,
        created_at=repo.utcnow(),
    )
    repo.insert_po(po)
    repo.apply_reroute(shipment_id, option, po.po_id, amount_usd)
    guard.mark_executed(auth)
    repo.audit(actor, "po_issued", shipment_id, {
        "po_id": po.po_id, "amount_usd": po.amount_usd, "option": option.label,
        "auto_executed": decision.auto_execute, "approval_id": approval_id, "hitl": decision.reasons,
    })
    return po
