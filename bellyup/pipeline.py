"""The whole journey: restaurant surplus -> agency -> people.

    leg 1  collection    restaurant --collected by--> agency pantry
    leg 2  distribution  agency --mobile pantry--> hubspots

Leg 2 only exists when the collecting agency runs a mobile pantry. When a fixed
pantry collects, the journey ends at the pantry and people walk in -- which is
why leg 2 returning nothing is often the correct answer, not a failure.
"""

from __future__ import annotations

from datetime import datetime

import agencies as ag_mod
import collection
import distribution
from economics import CONFIG


def run(donation: dict, agencies: list[dict], hubspots: list[dict],
        now: datetime | None = None, cfg: dict | None = None,
        ledger=None, commit: bool = False) -> dict:
    """Match the donation, then plan the onward run if the winner is mobile."""
    c = cfg or CONFIG
    leg1 = collection.match(donation, agencies, hubspots, now=now, cfg=c,
                            ledger=ledger, commit=commit)

    leg2 = None
    if leg1["matches"]:
        winner = leg1["matches"][0]
        agency = next(a for a in agencies if a["agency_id"] == winner["agency_id"])
        if agency["has_mobile_pantry"]:
            leg2 = distribution.plan(agency, hubspots, winner["accepts_lbs"],
                                     now=now, cfg=c, ledger=ledger, commit=commit)

    return {
        "donation": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                     for k, v in donation.items()},
        "collection": leg1,
        "distribution": leg2,
        "tax": leg1["tax"],
        "simulated_agency_data": leg1["simulated_agency_data"],
        "outcome": _outcome(leg1, leg2),
    }


def _outcome(leg1, leg2) -> dict:
    if not leg1["matches"]:
        return {"status": "no_agency", "summary": leg1["headline"]}
    w = leg1["matches"][0]
    if w["kind"] == "fixed":
        return {"status": "collected_walk_in",
                "summary": f"{w['agency_name']} collects {w['accepts_lbs']:.0f} lb. "
                           f"People come to the pantry for it."}
    if leg2 and leg2.get("feasible"):
        return {"status": "collected_and_distributed",
                "summary": f"{w['agency_name']} collects {w['accepts_lbs']:.0f} lb and "
                           f"its mobile pantry takes {leg2['delivered_lbs']:.0f} lb out "
                           f"to {leg2['n_stops']} hubspot(s)."}
    reason = leg2.get("reason", "") if leg2 else ""
    return {"status": "collected_pending_distribution",
            "summary": f"{w['agency_name']} collects {w['accepts_lbs']:.0f} lb. "
                       f"Mobile run not possible right now — {reason}"}
