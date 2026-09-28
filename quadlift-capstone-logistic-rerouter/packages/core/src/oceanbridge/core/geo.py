"""Reference geography: ports, chokepoints and sea-lane routing."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Location:
    code: str
    name: str
    lat: float
    lon: float
    region: str  # EAST_ASIA, SOUTH_ASIA, MIDDLE_EAST, EUROPE, NA_WEST, NA_EAST, SOUTH_AMERICA, CHOKEPOINT
    country: str = ""


PORTS: dict[str, Location] = {
    p.code: p
    for p in [
        Location("CNSHA", "Shanghai", 31.23, 121.47, "EAST_ASIA", "CN"),
        Location("CNNGB", "Ningbo-Zhoushan", 29.87, 121.55, "EAST_ASIA", "CN"),
        Location("CNSZX", "Shenzhen (Yantian)", 22.57, 114.27, "EAST_ASIA", "CN"),
        Location("KRPUS", "Busan", 35.10, 129.04, "EAST_ASIA", "KR"),
        Location("JPTYO", "Tokyo", 35.62, 139.78, "EAST_ASIA", "JP"),
        Location("SGSIN", "Singapore", 1.26, 103.82, "EAST_ASIA", "SG"),
        Location("INNSA", "Nhava Sheva", 18.95, 72.95, "SOUTH_ASIA", "IN"),
        Location("AEJEA", "Jebel Ali (Dubai)", 25.01, 55.06, "MIDDLE_EAST", "AE"),
        Location("NLRTM", "Rotterdam", 51.95, 4.14, "EUROPE", "NL"),
        Location("BEANR", "Antwerp", 51.26, 4.40, "EUROPE", "BE"),
        Location("DEHAM", "Hamburg", 53.54, 9.98, "EUROPE", "DE"),
        Location("GBFXT", "Felixstowe", 51.96, 1.35, "EUROPE", "GB"),
        Location("USLAX", "Los Angeles", 33.73, -118.26, "NA_WEST", "US"),
        Location("USLGB", "Long Beach", 33.75, -118.19, "NA_WEST", "US"),
        Location("USSEA", "Seattle", 47.60, -122.34, "NA_WEST", "US"),
        Location("USNYC", "New York / New Jersey", 40.67, -74.04, "NA_EAST", "US"),
        Location("USSAV", "Savannah", 32.08, -81.09, "NA_EAST", "US"),
        Location("BRSSZ", "Santos", -23.96, -46.33, "SOUTH_AMERICA", "BR"),
    ]
}

CHOKEPOINTS: dict[str, Location] = {
    c.code: c
    for c in [
        Location("MALACCA", "Strait of Malacca", 2.50, 101.50, "CHOKEPOINT"),
        Location("BAB_EL_MANDEB", "Bab-el-Mandeb", 12.60, 43.30, "CHOKEPOINT"),
        Location("SUEZ", "Suez Canal", 30.50, 32.35, "CHOKEPOINT"),
        Location("GIBRALTAR", "Strait of Gibraltar", 35.95, -5.60, "CHOKEPOINT"),
        Location("CAPE", "Cape of Good Hope", -34.36, 18.47, "CHOKEPOINT"),
        Location("PANAMA", "Panama Canal", 9.08, -79.68, "CHOKEPOINT"),
        Location("HORMUZ", "Strait of Hormuz", 26.57, 56.25, "CHOKEPOINT"),
    ]
}

ALL_LOCATIONS: dict[str, Location] = {**PORTS, **CHOKEPOINTS}

# Regions connected by long-haul rail corridors (China-Europe rail, US land bridge).
RAIL_CORRIDORS: set[frozenset[str]] = {
    frozenset({"EAST_ASIA", "EUROPE"}),
    frozenset({"NA_WEST", "NA_EAST"}),
}
# Rail on the Asia-Europe corridor is only available from mainland China terminals.
RAIL_ELIGIBLE_PORTS: set[str] = {"CNSHA", "CNNGB", "CNSZX", "NLRTM", "BEANR", "DEHAM", "USLAX", "USLGB", "USSEA", "USNYC", "USSAV"}


def haversine_km(a: Location, b: Location) -> float:
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = p2 - p1
    dl = math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def path_distance_km(codes: list[str], detour_factor: float = 1.0) -> float:
    pts = [ALL_LOCATIONS[c] for c in codes]
    return sum(haversine_km(a, b) for a, b in zip(pts, pts[1:])) * detour_factor


def region(code: str) -> str:
    return ALL_LOCATIONS[code].region


def sea_paths(origin: str, destination: str) -> list[list[str]]:
    """Candidate sea paths (as lists of chokepoints) between two ports; first entry is the default."""
    o, d = region(origin), region(destination)
    pair = {o, d}
    east_of_malacca = ALL_LOCATIONS[origin].lon > 101.5 or ALL_LOCATIONS[destination].lon > 101.5
    malacca = ["MALACCA"] if east_of_malacca and ("EAST_ASIA" in pair) else []

    def orient(path: list[str]) -> list[str]:
        # Paths are written west-bound from Asia; reverse when travelling the other way.
        return path if o in {"EAST_ASIA", "SOUTH_ASIA", "MIDDLE_EAST"} else list(reversed(path))

    if pair <= {"EAST_ASIA"} or pair <= {"EUROPE"} or pair <= {"NA_WEST"} or pair <= {"NA_EAST"}:
        return [[]]
    if pair == {"EAST_ASIA", "EUROPE"}:
        return [orient(malacca + ["BAB_EL_MANDEB", "SUEZ", "GIBRALTAR"]), orient(malacca + ["CAPE"])]
    if pair in ({"SOUTH_ASIA", "EUROPE"}, {"MIDDLE_EAST", "EUROPE"}):
        pre = ["HORMUZ"] if "MIDDLE_EAST" in pair else []
        return [orient(pre + ["BAB_EL_MANDEB", "SUEZ", "GIBRALTAR"]), orient(pre + ["CAPE"])]
    if pair == {"EAST_ASIA", "NA_WEST"}:
        return [[]]
    if pair == {"EAST_ASIA", "NA_EAST"}:
        return [orient(["PANAMA"]), orient(malacca + ["BAB_EL_MANDEB", "SUEZ", "GIBRALTAR"])]
    if pair == {"EUROPE", "NA_EAST"}:
        return [[]]
    if pair == {"EUROPE", "NA_WEST"}:
        return [["PANAMA"]]
    if pair == {"NA_WEST", "NA_EAST"}:
        return [["PANAMA"]]
    if pair == {"EAST_ASIA", "SOUTH_ASIA"} or pair == {"EAST_ASIA", "MIDDLE_EAST"}:
        return [orient(malacca)]
    if "SOUTH_AMERICA" in pair:
        other = (pair - {"SOUTH_AMERICA"}).pop() if len(pair) > 1 else "SOUTH_AMERICA"
        if other == "EAST_ASIA":
            return [orient(malacca + ["CAPE"])]
        return [[]]
    return [[]]


# Countries whose ports are connected by road (Channel Tunnel links GB to the continent).
LAND_MASSES: list[set[str]] = [{"NL", "BE", "DE", "GB"}, {"CN"}, {"US"}, {"KR"}, {"JP"}, {"SG"}, {"IN"}, {"AE"}, {"BR"}]


def road_connected(a: str, b: str) -> bool:
    ca, cb = PORTS[a].country, PORTS[b].country
    return any(ca in m and cb in m for m in LAND_MASSES)


def nearby_ports(code: str, max_km: float = 1100.0) -> list[tuple[str, float]]:
    """Alternative ports reachable by road from the given port, with distance."""
    here = PORTS[code]
    out = []
    for other in PORTS.values():
        if other.code == code or not road_connected(code, other.code):
            continue
        km = haversine_km(here, other)
        if km <= max_km:
            out.append((other.code, km))
    return sorted(out, key=lambda x: x[1])


def rail_available(origin: str, destination: str) -> bool:
    if origin not in RAIL_ELIGIBLE_PORTS or destination not in RAIL_ELIGIBLE_PORTS:
        return False
    return frozenset({region(origin), region(destination)}) in RAIL_CORRIDORS
