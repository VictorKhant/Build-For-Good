"""How much food a destination is actually looking for, and what it has had.

`need_now` is a STOCK -- people standing on a block on one count night. Food
demand is a FLOW -- pounds per day. Converting one to the other is what stops
the engine piling every donation onto whichever site happens to score best.

Two ideas live here:

1. **Apportionment.** At a 300 m radius, destination catchments overlap heavily.
   If each one claims the full population of every block it touches, the summed
   demand quadruple-counts the same people (3,006 against 670 actually
   counted). So each block's count is split among the destinations serving it,
   and the apportioned total then reconciles to the real population.

2. **Projection.** The budget is built from need projected forward, not need
   today, so a block whose count is climbing earns a bigger budget before the
   people arrive. This is the forecast entering the supply side, the same way
   BETA_TREND enters the ranking.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date as Date
from datetime import datetime

import needs as needs_mod


def apportioned_people(dests: list[dict], idx=None) -> dict[str, float]:
    """Split each block's counted population among the destinations serving it.

    A block covered by three catchments contributes a third of its count to
    each. Destinations outside the grid get 0 -- correct, they serve nobody the
    count data can see.
    """
    idx = idx or needs_mod.get_index()

    covered = Counter()
    for d in dests:
        for b in d.get("served_block_ids", []):
            covered[b] += 1

    out: dict[str, float] = {}
    for d in dests:
        total = 0.0
        for bid in d.get("served_block_ids", []):
            blk = idx.blocks.get(bid)
            if blk is None or not covered[bid]:
                continue
            total += blk.need_now / covered[bid]
        out[d["dest_id"]] = total
    return out


def apportioned_trend(dests: list[dict], idx=None) -> dict[str, float]:
    """Same split, applied to the block-level trend in persons/year.

    Deliberately the block trend only, not the area forecast delta: the delta is
    an adjusted monthly total and mixing it into a raw observed-persons budget
    would break the basis (data rule 1).
    """
    idx = idx or needs_mod.get_index()
    covered = Counter()
    for d in dests:
        for b in d.get("served_block_ids", []):
            covered[b] += 1

    out: dict[str, float] = {}
    for d in dests:
        total = 0.0
        for bid in d.get("served_block_ids", []):
            blk = idx.blocks.get(bid)
            if blk is None or not covered[bid]:
                continue
            total += blk.need_trend / covered[bid]
        out[d["dest_id"]] = total
    return out


def daily_demand(dests: list[dict], cfg: dict, idx=None) -> dict[str, dict]:
    """Pounds per day each destination is looking for.

        people   = apportioned count, projected DEMAND_HORIZON_DAYS forward
        demand   = people x MEALS_PER_PERSON_PER_DAY x LBS_PER_MEAL

    Capped by the site's physical per-visit capacity: a walk-in fridge does not
    grow because the block outside it did.
    """
    idx = idx or needs_mod.get_index()
    people = apportioned_people(dests, idx)
    trend = apportioned_trend(dests, idx)
    horizon_yr = cfg["DEMAND_HORIZON_DAYS"] / 365.25

    out: dict[str, dict] = {}
    for d in dests:
        did = d["dest_id"]
        now_p = people.get(did, 0.0)
        proj_p = max(0.0, now_p + trend.get(did, 0.0) * horizon_yr)
        lbs = proj_p * cfg["MEALS_PER_PERSON_PER_DAY"] * cfg["LBS_PER_MEAL"]
        capped = min(lbs, d.get("capacity_lbs_per_visit", lbs))
        out[did] = {
            "people_now": round(now_p, 1),
            "people_projected": round(proj_p, 1),
            "trend_per_year": round(trend.get(did, 0.0), 1),
            "demand_lbs_uncapped": round(lbs, 1),
            "capacity_lbs_per_visit": d.get("capacity_lbs_per_visit", 0.0),
            "daily_demand_lbs": round(capped, 1),
            "capped_by_capacity": capped < lbs,
        }
    return out


class Ledger:
    """What each destination has already been committed today.

    In-memory and per-day, per the no-database constraint. The point of it is
    simple: a destination that has already received its day's food must stop
    winning matches, or the engine cheerfully wastes the surplus it just routed.
    """

    def __init__(self) -> None:
        self._committed: dict[tuple[str, Date], float] = defaultdict(float)
        self._log: list[dict] = []

    def committed(self, dest_id: str, when: datetime | Date) -> float:
        return self._committed[(dest_id, _as_date(when))]

    def remaining(self, dest_id: str, when: datetime | Date, budget: float) -> float:
        return max(0.0, budget - self.committed(dest_id, when))

    def commit(self, dest_id: str, when: datetime | Date, lbs: float,
               donor: str = "", org_id: str = "") -> None:
        self._committed[(dest_id, _as_date(when))] += lbs
        self._log.append({
            "dest_id": dest_id, "date": _as_date(when).isoformat(),
            "lbs": round(lbs, 1), "donor": donor, "org_id": org_id,
        })

    def commit_match(self, match: dict, donation: dict) -> None:
        """Record every stop on an accepted run."""
        for stop in match["stops"]:
            self.commit(stop["dest_id"], datetime.fromisoformat(stop["arrival_at"]),
                        stop["lbs"], donation.get("donor_name", ""), match["org_id"])

    def snapshot(self, when: datetime | Date) -> dict[str, float]:
        d = _as_date(when)
        return {k[0]: v for k, v in self._committed.items() if k[1] == d and v > 0}

    @property
    def log(self) -> list[dict]:
        return list(self._log)

    def reset(self) -> None:
        self._committed.clear()
        self._log.clear()


def _as_date(when: datetime | Date) -> Date:
    return when.date() if isinstance(when, datetime) else when


# one process-wide ledger; the API exposes it for inspection and reset
LEDGER = Ledger()
