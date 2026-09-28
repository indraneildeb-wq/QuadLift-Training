"""Seed the mock Supply Chain Core: carriers, ~48 shipments with original POs.

Run:  python -m oceanbridge.seed [--reset]
"""

from __future__ import annotations

import argparse
import random
from datetime import date, timedelta

from sqlalchemy import delete

from oceanbridge.config import get_settings
from oceanbridge.core import db as dbm
from oceanbridge.core.db import CarrierRow, ShipmentRow, session_scope
from oceanbridge.core.freight import calculate_freight_cost
from oceanbridge.core.reference import CARRIERS, carriers_for
from oceanbridge.core.repository import insert_po, utcnow
from oceanbridge.models import Leg, Mode, PurchaseOrder, SLATerms

# (origin, destination, mode, weight range kg)
LANES = [
    ("CNSHA", "NLRTM", Mode.SEA, (6_000, 40_000)),
    ("CNSHA", "DEHAM", Mode.SEA, (6_000, 40_000)),
    ("CNNGB", "BEANR", Mode.SEA, (6_000, 30_000)),
    ("CNSZX", "NLRTM", Mode.SEA, (6_000, 30_000)),
    ("CNSHA", "USLAX", Mode.SEA, (5_000, 35_000)),
    ("CNSZX", "USLGB", Mode.SEA, (5_000, 35_000)),
    ("KRPUS", "USLAX", Mode.SEA, (5_000, 25_000)),
    ("CNSHA", "USNYC", Mode.SEA, (6_000, 30_000)),
    ("JPTYO", "USSAV", Mode.SEA, (6_000, 25_000)),
    ("SGSIN", "GBFXT", Mode.SEA, (6_000, 25_000)),
    ("INNSA", "NLRTM", Mode.SEA, (6_000, 25_000)),
    ("AEJEA", "DEHAM", Mode.SEA, (6_000, 25_000)),
    ("NLRTM", "USNYC", Mode.SEA, (6_000, 25_000)),
    ("CNSHA", "DEHAM", Mode.RAIL, (4_000, 18_000)),
    ("CNSHA", "NLRTM", Mode.AIR, (300, 3_000)),
    ("USLAX", "USNYC", Mode.RAIL, (5_000, 20_000)),
]

# commodity -> (USD per kg range, kg per cbm)
COMMODITIES = {
    "Consumer electronics": ((55, 120), 180),
    "Apparel & footwear": ((7, 14), 160),
    "Automotive parts": ((10, 22), 350),
    "Pharmaceuticals": ((150, 380), 220),
    "Furniture": ((3, 6), 120),
    "Industrial machinery": ((18, 35), 400),
    "Home appliances": ((6, 12), 150),
    "Toys & games": ((5, 10), 140),
}
CUSTOMERS = ["Northwind Retail", "Contoso Electronics", "Fabrikam Motors", "Tailspin Pharma", "Litware Home",
             "Adventure Works", "Proseware Industrial", "Wide World Importers"]


def seed(reset: bool = True, n_shipments: int = 48, rng_seed: int = 42) -> dict:
    rng = random.Random(rng_seed)
    dbm.get_engine()
    today = date.today()
    with session_scope() as s:
        if reset:
            for table in reversed(dbm.Base.metadata.sorted_tables):
                s.execute(delete(table))
        for c in CARRIERS:
            s.merge(CarrierRow(id=c.id, name=c.name, mode=c.mode.value, capacity_remaining=c.capacity,
                               capacity_unit=c.capacity_unit))

    pos: list[PurchaseOrder] = []
    rows: list[ShipmentRow] = []
    for i in range(n_shipments):
        o, d, mode, (wmin, wmax) = LANES[i % len(LANES)]
        commodity = rng.choice(list(COMMODITIES))
        (vmin, vmax), density = COMMODITIES[commodity]
        weight = float(rng.randint(wmin // 100, wmax // 100) * 100)
        volume = round(weight / density * rng.uniform(0.9, 1.1), 1)
        value = round(weight * rng.uniform(vmin, vmax), -2)
        carrier = rng.choice(carriers_for(mode))
        q = calculate_freight_cost(o, d, mode, weight, volume, carrier_id=carrier.id)
        # Booked at contract rate plus a peak-season surcharge of 0-12%.
        booked_cost = round(q.cost_usd * rng.uniform(1.0, 1.12), 2)
        in_transit = mode == Mode.SEA and rng.random() < 0.2
        dep = today - timedelta(days=rng.randint(2, 8)) if in_transit else today + timedelta(days=rng.randint(1, 10))
        eta = dep + timedelta(days=round(q.transit_days))
        slack = rng.choice([1, 2, 3, 5, 7, 10])
        sid = f"SHP-{1001 + i}"
        leg = Leg(mode=mode, origin=o, destination=d, carrier_id=carrier.id, via=q.via, distance_km=q.distance_km,
                  transit_days=q.transit_days, cost_usd=booked_cost)
        po_id = f"PO-{sid}-ORIG"
        rows.append(ShipmentRow(
            id=sid, customer=rng.choice(CUSTOMERS), commodity=commodity, origin=o, destination=d, mode=mode.value,
            carrier_id=carrier.id, legs=[leg.model_dump(mode="json")], weight_kg=weight, volume_cbm=volume,
            cargo_value_usd=value, current_cost_usd=booked_cost, departure_date=dep, eta=eta,
            required_delivery_date=eta + timedelta(days=slack), status="in_transit" if in_transit else "booked",
            po_id=po_id,
        ))
        pos.append(PurchaseOrder(
            po_id=po_id, shipment_id=sid, carrier_id=carrier.id, amount_usd=booked_cost,
            route_label=f"{mode.value.title()} {o}->{d}", legs=[leg],
            sla=SLATerms(guaranteed_transit_days=q.transit_days + 3, late_penalty_pct_per_day=1.0,
                         max_penalty_pct=10.0, reserved_capacity="standard allocation"),
            status="issued", auto_executed=True, created_at=utcnow(),
        ))
    with session_scope() as s:
        s.add_all(rows)
    for po in pos:
        insert_po(po)
    return {"db": str(get_settings().db_file), "carriers": len(CARRIERS), "shipments": len(rows)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-reset", action="store_true", help="keep existing data")
    ap.add_argument("-n", type=int, default=48)
    args = ap.parse_args()
    print(seed(reset=not args.no_reset, n_shipments=args.n))


if __name__ == "__main__":
    main()
