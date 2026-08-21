"""Runtime state the three views share: pickup schedule and agency intake limits.

In-memory, per the no-database constraint. Two things live here that the
matching engine alone cannot express:

**Scheduled pickups.** Once a restaurant is matched to an agency, that agency
needs to see it on a list -- "these are the collections you have today". The
match is a decision; the schedule is the commitment.

**Agency intake limits.** An agency can say "I only want 200 lb today",
independently of what its walk-in demand or vehicle capacity implies. That is an
operational choice the data cannot make for them: staff off sick, fridge already
full, a van in the shop. The declared limit always tightens, never loosens --
it cannot make a pantry absorb more than its people can eat.
"""

from __future__ import annotations

import itertools
from datetime import date as Date
from datetime import datetime

_ids = itertools.count(1)


class Schedule:
    """Pickups an agency has committed to."""

    def __init__(self) -> None:
        self._pickups: list[dict] = []

    def add(self, donation: dict, match: dict, when: datetime) -> dict:
        rec = {
            "pickup_id": f"pu_{next(_ids):04d}",
            "agency_id": match["agency_id"],
            "agency_name": match["agency_name"],
            "donor_name": donation.get("donor_name", ""),
            "donor_lat": donation["lat"], "donor_lon": donation["lon"],
            "donor_address": donation.get("address", ""),
            "food_type": donation["food_type"],
            "condition": donation["condition"],
            "quantity_lbs": round(float(match["accepts_lbs"]), 1),
            "offered_lbs": round(float(donation["quantity_lbs"]), 1),
            "pickup_at": match["pickup_at"],
            "distance_km": match["one_way_km"],
            "round_trip_km": match["round_trip_km"],
            "cost": match["transport_cost"],
            "cost_per_meal": match["cost_per_meal"],
            "meals": match["meals"],
            "freshness": match.get("freshness"),
            "reaches_people_at": match.get("reaches_people_at"),
            "date": _as_date(when).isoformat(),
            "status": "scheduled",
        }
        self._pickups.append(rec)
        return rec

    def for_agency(self, agency_id: str, when: datetime | Date | None = None,
                   max_km: float | None = None) -> list[dict]:
        d = _as_date(when).isoformat() if when else None
        out = [p for p in self._pickups
               if p["agency_id"] == agency_id
               and (d is None or p["date"] == d)
               and (max_km is None or p["distance_km"] <= max_km)]
        return sorted(out, key=lambda p: p["pickup_at"])

    def all(self, when: datetime | Date | None = None) -> list[dict]:
        d = _as_date(when).isoformat() if when else None
        return [p for p in self._pickups if d is None or p["date"] == d]

    def committed_lbs(self, agency_id: str, when: datetime | Date) -> float:
        d = _as_date(when).isoformat()
        return sum(p["quantity_lbs"] for p in self._pickups
                   if p["agency_id"] == agency_id and p["date"] == d
                   and p["status"] != "cancelled")

    def cancel(self, pickup_id: str) -> bool:
        for p in self._pickups:
            if p["pickup_id"] == pickup_id:
                p["status"] = "cancelled"
                return True
        return False

    def reset(self) -> None:
        self._pickups.clear()


class Limits:
    """Agency-declared caps on how much they want to take, per day."""

    def __init__(self) -> None:
        self._limits: dict[tuple[str, Date], float] = {}

    def set(self, agency_id: str, when: datetime | Date, lbs: float | None) -> None:
        key = (agency_id, _as_date(when))
        if lbs is None:
            self._limits.pop(key, None)
        else:
            self._limits[key] = max(0.0, float(lbs))

    def get(self, agency_id: str, when: datetime | Date) -> float | None:
        return self._limits.get((agency_id, _as_date(when)))

    def cap(self, agency_id: str, when: datetime | Date, budget: float) -> float:
        """Apply the declared limit to a demand-derived budget.

        Only ever tightens. An agency saying "send me 900 lb" does not make its
        walk-in population able to eat 900 lb.
        """
        lim = self.get(agency_id, when)
        return budget if lim is None else min(budget, lim)

    def snapshot(self, when: datetime | Date) -> dict[str, float]:
        d = _as_date(when)
        return {k[0]: v for k, v in self._limits.items() if k[1] == d}

    def reset(self) -> None:
        self._limits.clear()


def _as_date(when: datetime | Date) -> Date:
    return when.date() if isinstance(when, datetime) else when


SCHEDULE = Schedule()
LIMITS = Limits()
