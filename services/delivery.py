"""Configurable delivery pricing engine.

Rule rows live in the ``delivery_rules`` table and are fully editable from the
Mini App admin panel (add/edit/reorder/enable). Rules are evaluated top-down;
the FIRST matching rule decides the fee. Supported rule types:

* ``free_over``  — free delivery when items_total >= free_over, else flat
* ``flat``       — constant fee regardless of total (optionally capped by km)
* ``per_km``     — amount per kilometer of haversine distance
* ``percent``    — percent of items_total, optional floor/ceil via amount
"""
from __future__ import annotations

import math
from typing import Any

from database import Database


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def _rule_fee(
    rule: dict[str, Any], items_total: int, distance_km: float | None
) -> int | None:
    rtype = rule["rule_type"]

    if rtype == "free_over":
        free_over = rule.get("free_over") or 0
        if items_total >= free_over:
            return 0
        return int(rule.get("amount") or 0) or None

    if rtype == "flat":
        max_km = rule.get("max_km")
        if max_km is not None and distance_km is not None and distance_km > max_km:
            return None
        return int(rule.get("amount") or 0)

    if rtype == "per_km":
        if distance_km is None:
            return None
        return int(round(float(rule.get("amount") or 0) * distance_km))

    if rtype == "percent":
        fee = items_total * float(rule.get("percent") or 0) / 100.0
        return int(round(fee))

    return None


async def calculate_delivery(
    db: Database,
    branch: dict[str, Any],
    items_total: int,
    lat: float | None = None,
    lon: float | None = None,
) -> int:
    """Return the delivery fee in UZS for a cart; first matching rule wins."""
    rules = await db.list_delivery_rules(branch["id"], only_active=True)
    distance_km: float | None = None
    if (
        lat is not None
        and lon is not None
        and branch.get("lat") is not None
        and branch.get("lon") is not None
    ):
        distance_km = haversine_km(lat, lon, branch["lat"], branch["lon"])

    for rule in rules:
        min_total = int(rule.get("min_total") or 0)
        if items_total < min_total:
            continue
        fee = _rule_fee(rule, items_total, distance_km)
        if fee is None:
            continue
        return max(0, fee)

    # No rule matched -> treat delivery as free (admin controls rules).
    return 0
