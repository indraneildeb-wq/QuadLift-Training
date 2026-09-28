"""Deterministic pseudo-random baseline so feeds look alive but are reproducible per day."""

from __future__ import annotations

import hashlib
from datetime import date


def noise(*parts: object, day: date | None = None) -> float:
    """Stable value in [0, 1) for the given key and day."""
    day = day or date.today()
    h = hashlib.sha256("|".join(map(str, (*parts, day.isoformat()))).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64
