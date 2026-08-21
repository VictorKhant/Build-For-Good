"""Side 3 -- where a person can go to get food right now.

This view exists for people, not organisations, so it is the one place in the
system where privacy has to be reasoned about carefully.

**It lists pantries only. It never lists hubspots.**

Pantries are public addresses that already advertise their hours; naming one
tells nobody anything they could not learn from a flyer. A hubspot is a block
where unsheltered people are known to gather. Publishing "food is being handed
out at 17th & K at 4pm" would tell anyone -- including someone looking to move
people on -- exactly where to find them. Block aggregation was chosen so the
output could not be operationalised for enforcement, and this view is where
that promise would be easiest to break by accident.

So: side 2 (agency) sees hubspots. Side 3 (public) sees pantries. They do not
merge.
"""

from __future__ import annotations

from datetime import datetime

import demand as demand_mod
import rules
import schedule as sched
from economics import CONFIG
from needs import haversine_km


def find(agencies: list[dict], lat: float | None = None, lon: float | None = None,
         now: datetime | None = None, cfg: dict | None = None,
         max_km: float | None = None, ledger=None,
         only_with_food: bool = False) -> dict:
    """Pantries a person could walk or travel to, nearest first.

    On stock: the ledger only knows what THIS platform routed. A pantry may
    well have food from the food bank or its own sources that we never saw, so
    a zero here means "we did not send any today", not "there is nothing".
    The view says so rather than implying an empty shelf and sending someone on
    a wasted walk.
    """
    c = cfg or CONFIG
    now = now or datetime.now()
    ledger = demand_mod.LEDGER if ledger is None else ledger

    stock = ledger.snapshot(now)
    out = []

    for a in agencies:
        # A mobile pantry's base is a real address, but its food is loaded onto
        # a van rather than laid out for callers, so it is flagged rather than
        # presented as somewhere to turn up.
        walk_in = not a["has_mobile_pantry"]
        on_hand = float(stock.get(a["agency_id"], 0.0))
        scheduled = sched.SCHEDULE.committed_lbs(a["agency_id"], now)

        open_now = rules.in_windows(now, a["operating_windows"])
        nxt = rules.next_open(now, a["operating_windows"])

        rec = {
            "agency_id": a["agency_id"],
            "name": a["name"],
            "simulated": a.get("simulated", False),
            "address": a.get("address", ""),
            "lat": a["lat"], "lon": a["lon"],
            "walk_in": walk_in,
            "kind": "walk-in pantry" if walk_in else "mobile pantry base",
            "open_now": open_now,
            "next_open": "open now" if open_now else nxt,
            "hours": _hours_text(a["operating_windows"]),
            "accepts": a["accepts"],
            "delivered_today": on_hand > 0,
            "delivered_today_lbs": round(on_hand, 1),
            "expected_today_lbs": round(scheduled, 1),
            "stock_note": (f"{on_hand:.0f} lb delivered here today via BellyUp"
                           if on_hand > 0 else
                           "no BellyUp deliveries today — they may still have food "
                           "from other sources"),
        }
        if lat is not None and lon is not None:
            km = haversine_km(lat, lon, a["lat"], a["lon"])
            rec["distance_km"] = round(km, 2)
            rec["walk_minutes"] = round(km / 5.0 * 60)   # 5 km/h walking pace
        out.append(rec)

    if max_km is not None and lat is not None:
        out = [r for r in out if r.get("distance_km", 0) <= max_km]
    if only_with_food:
        out = [r for r in out if r["delivered_today"]]

    # nearest first when we know where they are; otherwise those with food and
    # open doors come first
    if lat is not None and lon is not None:
        # open doors first, then nearest -- a closed pantry two streets away is
        # worse than an open one a kilometre off if you are walking there now
        out.sort(key=lambda r: (not r["walk_in"], not r["open_now"],
                                r.get("distance_km", 9e9)))
    else:
        out.sort(key=lambda r: (not r["delivered_today"], not r["open_now"], r["name"]))

    return {
        "view": "pantry_finder",
        "count": len(out),
        "delivered_today": sum(1 for r in out if r["delivered_today"]),
        "open_now": sum(1 for r in out if r["open_now"]),
        "pantries": out,
        "privacy_note": "Pantry locations only. Outreach hubspots are never shown "
                        "in this view.",
    }


def _hours_text(windows: list[dict]) -> str:
    if not windows:
        return "hours not published"
    by_time: dict[tuple[str, str], list[int]] = {}
    for w in windows:
        by_time.setdefault((w["start"], w["end"]), []).append(w["dow"])
    parts = []
    for (start, end), days in by_time.items():
        names = "".join(rules.DOW[d][0] for d in sorted(days))
        parts.append(f"{names} {start}-{end}")
    return "; ".join(parts)
