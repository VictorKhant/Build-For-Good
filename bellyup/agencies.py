"""Agencies -- the middle layer between restaurants and unsheltered people.

    small restaurant --collected by--> AGENCY --> people
                                          |
                        fixed pantry: people walk in
                        mobile pantry: goes out to hubspots

Two kinds, and they behave differently on the demand side:

**Fixed pantry.** People come to it, so what it can usefully absorb is bounded
by how many people are within walking distance. Give it more than that and the
surplus rots on a shelf, so its intake is CAPPED by walk-in demand.

**Mobile pantry.** It travels, so its reach is not bound to one address. Its
intake is UNCAPPED -- overflow is not a concern because it can carry food to
wherever the need is. It is also the only kind that can serve a hubspot: a
fixed pantry cannot bring food to someone sleeping on a block.

Both absorb the cost of collection. The restaurant pays nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import demand as demand_mod
import needs as needs_mod

DATA_DIR = Path(__file__).resolve().parent / "data"

DEFAULTS = {
    "collects_donations": True,
    "staff_per_run": 1,
    "has_refrigerated_vehicle": False,
    "has_mobile_pantry": False,      # safe direction to be wrong in
    "mobile_capacity_lbs": 0.0,
    "max_hubspot_stops": 0,
    "intake_capacity_lbs_per_day": 600.0,
    "accepts": ["produce", "packaged_dry"],
    "storage": {"refrigerated": False, "frozen": False, "hot_holding": False},
    "operating_windows": [],
    "mobile_windows": [],
    "verified": False,
}


def load(cfg: dict, path: Path | None = None) -> list[dict]:
    """Read agencies.json and apply defaults for anything the roster omits.

    Missing fields are defaulted, never guessed, and the agency stays
    `verified: false` so verify.py surfaces it.
    """
    path = path or (DATA_DIR / "agencies.json")
    blob = json.loads(path.read_text())
    raw = blob["agencies"] if isinstance(blob, dict) else blob
    simulated = blob.get("simulated", False) if isinstance(blob, dict) else False

    out = []
    for a in raw:
        rec = {**DEFAULTS, **a}
        rec.setdefault("wage_per_hour", cfg["WAGE_PER_HOUR"])
        rec.setdefault("cost_per_km", cfg["COST_PER_KM"])
        rec["simulated"] = rec.get("simulated", simulated)
        # a fixed pantry cannot run a hubspot route no matter what else it says
        if not rec["has_mobile_pantry"]:
            rec["mobile_capacity_lbs"] = 0.0
            rec["max_hubspot_stops"] = 0
            rec["mobile_windows"] = []
        out.append(rec)
    return out


def is_simulated(agencies: list[dict]) -> bool:
    return any(a.get("simulated") for a in agencies)


# --------------------------------------------------------------------------
# how much food an agency can usefully absorb
# --------------------------------------------------------------------------

def intake_demand(agencies: list[dict], cfg: dict, idx=None) -> dict[str, dict]:
    """Daily intake budget per agency.

    Fixed pantry: apportioned walk-in population within WALK_IN_RADIUS_M,
    projected forward, converted to pounds, capped by physical intake capacity.

    Mobile pantry: uncapped by demand. Bounded only by what the operation can
    physically take in, because a travelling pantry is not limited to whoever
    happens to live within walking distance of its front door.
    """
    idx = idx or needs_mod.get_index()
    radius = cfg["WALK_IN_RADIUS_M"]

    # attribute blocks to fixed pantries at the walk-in radius, then apportion
    # so two pantries on the same blocks do not each claim the whole population
    fixed = [a for a in agencies if not a["has_mobile_pantry"]]
    catchments = {
        a["agency_id"]: [b.block_id for b in idx.blocks_within(a["lat"], a["lon"], radius)]
        for a in fixed
    }
    shadow = [{"dest_id": aid, "served_block_ids": blocks}
              for aid, blocks in catchments.items()]
    people = demand_mod.apportioned_people(shadow, idx)
    trend = demand_mod.apportioned_trend(shadow, idx)

    horizon_yr = cfg["DEMAND_HORIZON_DAYS"] / 365.25
    out: dict[str, dict] = {}

    for a in agencies:
        aid = a["agency_id"]
        cap = float(a["intake_capacity_lbs_per_day"])

        if a["has_mobile_pantry"]:
            out[aid] = {
                "kind": "mobile",
                "walk_in_people": None,
                "demand_lbs": cap,
                "capped_by": "physical intake capacity",
                "uncapped_by_demand": True,
                "n_blocks": 0,
            }
            continue

        now_p = people.get(aid, 0.0)
        proj_p = max(0.0, now_p + trend.get(aid, 0.0) * horizon_yr)
        lbs = proj_p * cfg["MEALS_PER_PERSON_PER_DAY"] * cfg["LBS_PER_MEAL"]
        budget = min(lbs, cap)
        out[aid] = {
            "kind": "fixed",
            "walk_in_people": round(now_p, 1),
            "walk_in_projected": round(proj_p, 1),
            # persons/year, the same basis hubspot trends use. Storing the
            # annual rate rather than the horizon delta keeps the two
            # comparable when they are normalised against each other.
            "walk_in_trend_per_year": round(trend.get(aid, 0.0), 2),
            "demand_lbs_uncapped": round(lbs, 1),
            "demand_lbs": round(budget, 1),
            "capped_by": "walk-in demand" if lbs < cap else "physical intake capacity",
            "uncapped_by_demand": False,
            "n_blocks": len(catchments.get(aid, [])),
        }
    return out


def summary(agencies: list[dict], budgets: dict) -> str:
    mob = [a for a in agencies if a["has_mobile_pantry"]]
    fix = [a for a in agencies if not a["has_mobile_pantry"]]
    fixed_demand = sum(budgets[a["agency_id"]]["demand_lbs"] for a in fix)
    return (f"{len(agencies)} agencies: {len(mob)} mobile (uncapped intake), "
            f"{len(fix)} fixed pantries ({fixed_demand:.0f} lb/day of walk-in demand)")
