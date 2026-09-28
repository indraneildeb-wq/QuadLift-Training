"""Route & capacity engine: generates and scores air/sea/rail/multimodal alternatives.

All numbers the optimisation agent sees come from here, so an LLM can rank and justify
options but never invent prices."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Callable

from oceanbridge.config import get_settings
from oceanbridge.core import geo
from oceanbridge.core.freight import FreightQuote, capacity_required
from oceanbridge.core.freight import calculate_freight_cost as _raw_calc
from oceanbridge.core.reference import CARRIERS_BY_ID, carriers_for
from oceanbridge.core.risk import applicable_disruptions, combine_risk, relevant_disruptions
from oceanbridge.models import Disruption, DisruptionType, Leg, Mode, RouteOption, Shipment, ShipmentStatus

TRANSFER_DAYS = 1.5
# Surcharge on freight for each disrupted touchpoint (fraction of leg cost x severity).
SURCHARGE = {
    DisruptionType.GEOPOLITICAL: 0.15,   # war-risk premium
    DisruptionType.PORT_CONGESTION: 0.08,
    DisruptionType.PORT_CLOSURE: 0.05,
    DisruptionType.WEATHER: 0.03,
    DisruptionType.LABOR: 0.04,
    DisruptionType.CANAL_RESTRICTION: 0.10,
}
# CO2 per km for trucking is the heaviest factor; keep the emission estimate on the quote.

QuoteFn = Callable[..., FreightQuote]


def _leg(q: FreightQuote) -> Leg:
    return Leg(mode=Mode(q.mode), origin=q.origin, destination=q.destination, carrier_id=q.carrier_id,
               via=q.via, distance_km=q.distance_km, transit_days=q.transit_days, cost_usd=q.cost_usd)


def _ranked_carriers(mode: Mode, need: float, capacity: dict[str, float]) -> list[str]:
    """Carriers for a mode ordered by reliability-adjusted price; those with enough capacity first."""
    ranked = sorted(carriers_for(mode), key=lambda c: c.rate_multiplier * (1 + (1 - c.reliability)))
    ok = [c.id for c in ranked if capacity.get(c.id, c.capacity) >= need]
    return ok or [ranked[0].id]


class RouteEngine:
    def __init__(self, capacity: dict[str, float], quote_fn: QuoteFn | None = None, today: date | None = None):
        self.capacity = capacity
        self.quote = quote_fn or _raw_calc
        self.today = today or date.today()
        self.econ = get_settings().economics

    # ------------------------------------------------------------------ public
    def alternatives(self, shipment: Shipment, disruptions: list[Disruption], max_options: int = 8) -> list[RouteOption]:
        builders: list[tuple[str, list[list[tuple]]]] = []
        o, d = shipment.origin, shipment.destination
        in_transit = shipment.status == ShipmentStatus.IN_TRANSIT
        disrupted = {x.location for x in disruptions}

        if not in_transit:
            # 1. Alternative sea paths (e.g. Cape of Good Hope instead of Suez)
            for path in geo.sea_paths(o, d):
                builders.append((f"Sea {o}->{d} via {'/'.join(path) or 'direct'}", [[("sea", o, d, path)]]))
            # 2. Alternative origin port (truck to a nearby port, then sea)
            if o in disrupted:
                for alt, _km in geo.nearby_ports(o)[:2]:
                    path = self._best_sea_path(alt, d, disrupted)
                    builders.append((f"Truck {o}->{alt}, sea {alt}->{d}", [[("road", o, alt, None), ("sea", alt, d, path)]]))
            # 3. Rail corridor (China-Europe rail, US land bridge)
            if geo.rail_available(o, d):
                builders.append((f"Rail {o}->{d}", [[("rail", o, d, None)]]))
            # 4. Air freight
            builders.append((f"Air {o}->{d}", [[("air", o, d, None)]]))
            # 5. Sea-air via Dubai for Asia -> Europe
            if {geo.region(o), geo.region(d)} == {"EAST_ASIA", "EUROPE"} and geo.region(o) == "EAST_ASIA":
                builders.append((f"Sea-air {o}->AEJEA->{d}", [[("sea", o, "AEJEA", ["MALACCA", "HORMUZ"]), ("air", "AEJEA", d, None)]]))
            # 6. US land bridge: Asia -> US East Coast via West Coast + rail
            if geo.region(o) == "EAST_ASIA" and geo.region(d) == "NA_EAST":
                for wc in ("USSEA", "USLAX"):
                    builders.append((f"Sea {o}->{wc}, rail {wc}->{d}", [[("sea", o, wc, []), ("rail", wc, d, None)]]))

        # 7. Alternative destination port (sea to a nearby port, truck the last mile)
        if d in disrupted or in_transit:
            for alt, _km in geo.nearby_ports(d)[:3]:
                path = [] if in_transit else self._best_sea_path(o, alt, disrupted)
                if in_transit:
                    path = next((leg.via for leg in shipment.legs if leg.mode == Mode.SEA), [])
                builders.append((f"Sea {o}->{alt}, truck {alt}->{d}", [[("sea", o, alt, path), ("road", alt, d, None)]]))

        stay = self._stay(shipment, disruptions)
        options: list[RouteOption] = [stay]
        seen: set[str] = set()
        for label, variants in builders:
            for spec in variants:
                opt = self._price(shipment, label, spec, disruptions)
                if opt is None:
                    continue
                # A reroute must mitigate: land earlier than absorbing the delay, or cut residual risk
                # materially without landing later.
                earlier = opt.projected_eta < stay.projected_eta
                safer = opt.projected_eta <= stay.projected_eta and opt.residual_risk <= stay.residual_risk - 0.1
                if not (earlier or safer):
                    continue
                # Same physical path as today (only the carrier differs) does not avoid the disruption.
                sig = self._signature(opt.legs)
                if sig in seen or sig == self._signature(shipment.legs):
                    continue
                seen.add(sig)
                options.append(opt)
        options.sort(key=lambda x: x.landed_cost_score)
        for i, opt in enumerate(options):
            if opt.option_id != "STAY":
                opt.option_id = f"OPT-{i + 1}"
        return options[:max_options]

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _signature(legs: list[Leg]) -> str:
        return "|".join(f"{leg.mode.value}:{leg.origin}>{leg.destination}:{','.join(leg.via)}" for leg in legs)

    def _best_sea_path(self, o: str, d: str, disrupted: set[str]) -> list[str]:
        paths = geo.sea_paths(o, d)
        return min(paths, key=lambda p: (len(set(p) & disrupted), paths.index(p)))

    def _start_date(self, shipment: Shipment) -> date:
        return max(self.today, shipment.departure_date)

    def _score(self, shipment: Shipment, cost: float, transit_days: float, eta: date, risk: float) -> float:
        late = max(0, (eta - shipment.required_delivery_date).days)
        v = shipment.cargo_value_usd
        return round(cost + v * self.econ.holding_cost_per_day * transit_days
                     + v * self.econ.lateness_penalty_per_day * late + v * 0.02 * risk, 2)

    def _stay(self, shipment: Shipment, disruptions: list[Disruption]) -> RouteOption:
        rel = relevant_disruptions(shipment, disruptions)
        risk, delay = combine_risk(rel)
        eta = shipment.eta + timedelta(days=math.ceil(delay))
        transit = (eta - self._start_date(shipment)).days
        co2 = sum(leg.distance_km for leg in shipment.legs) * shipment.weight_kg / 1e6 * 10
        return RouteOption(
            option_id="STAY", shipment_id=shipment.id, label="Keep current routing (absorb delay)",
            mode=shipment.mode, legs=shipment.legs, total_cost_usd=shipment.current_cost_usd,
            transit_days=float(transit), co2_kg=round(co2, 1), cost_delta_usd=0.0, cost_increase_pct=0.0,
            projected_eta=eta, lead_time_delta_days=float((eta - shipment.eta).days), residual_risk=risk,
            landed_cost_score=self._score(shipment, shipment.current_cost_usd, transit, eta, risk),
            capacity_ok=True, primary_carrier_id=shipment.carrier_id,
        )

    def _price(self, shipment: Shipment, label: str, spec: list[tuple], disruptions: list[Disruption]) -> RouteOption | None:
        legs: list[Leg] = []
        co2 = 0.0
        capacity_ok = True
        try:
            for mode_s, o, d, via in spec:
                mode = Mode(mode_s)
                need = capacity_required(mode, shipment.weight_kg, shipment.volume_cbm)
                carrier = _ranked_carriers(mode, need, self.capacity)[0]
                if self.capacity.get(carrier, CARRIERS_BY_ID[carrier].capacity) < need:
                    capacity_ok = False
                q = self.quote(o, d, mode, shipment.weight_kg, shipment.volume_cbm, carrier_id=carrier,
                               via=via if mode == Mode.SEA else None)
                legs.append(_leg(q))
                co2 += q.co2_kg
        except ValueError:
            return None

        # Exposure is per leg mode: trucking out of a closed seaport or flying over a strike is unaffected.
        skip = {shipment.origin} if shipment.status == ShipmentStatus.IN_TRANSIT else set()
        rel = applicable_disruptions(legs, disruptions, skip)

        freight = sum(leg.cost_usd for leg in legs)
        surcharge = sum(SURCHARGE[x.type] * x.severity * freight for x in rel)
        total = round(freight + surcharge, 2)
        risk, delay = combine_risk(rel)
        transit = sum(leg.transit_days for leg in legs) + TRANSFER_DAYS * (len(legs) - 1) + delay
        if shipment.status == ShipmentStatus.IN_TRANSIT:
            # Only the tail of the voyage changes; the ETA moves by the change in the final leg(s).
            base_transit = sum(leg.transit_days for leg in shipment.legs)
            eta = shipment.eta + timedelta(days=math.ceil(transit - base_transit))
        else:
            eta = self._start_date(shipment) + timedelta(days=math.ceil(transit))
        mode = legs[0].mode if len({leg.mode for leg in legs}) == 1 else Mode.MULTIMODAL
        primary = max(legs, key=lambda leg: leg.cost_usd).carrier_id
        label = f"{label} [{CARRIERS_BY_ID[primary].name}]"
        delta = total - shipment.current_cost_usd
        return RouteOption(
            option_id="tmp", shipment_id=shipment.id, label=label, mode=mode, legs=legs,
            total_cost_usd=total, transit_days=round(transit, 1), co2_kg=round(co2, 1),
            cost_delta_usd=round(delta, 2), cost_increase_pct=round(delta / shipment.current_cost_usd * 100, 2),
            projected_eta=eta, lead_time_delta_days=float((eta - shipment.eta).days), residual_risk=risk,
            landed_cost_score=self._score(shipment, total, transit, eta, risk), capacity_ok=capacity_ok,
            primary_carrier_id=primary,
        )
