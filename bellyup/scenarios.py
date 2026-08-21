"""The three rehearsed demo scenarios.

Anchored to a fixed datetime so the numbers on stage are the numbers in
rehearsal. Wednesday 2026-08-19, week 3 of the month -- deliberately a week
when the "1st and 3rd Thursday" food bank sites are in cadence, so the
weeks_of_month logic is exercised rather than bypassed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

DEMO_NOW = datetime(2026, 8, 19, 14, 30)  # Wednesday 14:30


def _d(**kw) -> datetime:
    return DEMO_NOW + timedelta(**kw)


SCENARIOS = {
    "good_match": {
        "label": "1 — The good match",
        "blurb": "60 lb of packaged dry goods out of a Gaslamp restaurant, five days "
                 "to expiry. The winning nonprofit is not the one with the nearest "
                 "HQ: the three-leg route is what is being minimised, not the first leg.",
        "donation": {
            "donor_name": "Nolita Hall",
            "lat": 32.7245, "lon": -117.1698,
            "address": "2305 India St, San Diego, CA 92101",
            "food_type": "packaged_dry", "quantity_lbs": 60.0, "condition": "ambient",
            "ready_at": _d(minutes=30), "expires_at": _d(days=5),
            "sb1383_tier": 2,
        },
    },
    "rejection": {
        "label": "2 — The rejection",
        "blurb": "12 lb of hot prepared food with 90 minutes to expiry. Almost "
                 "everything fails, and each failure names itself. This is the slide "
                 "people remember.",
        "donation": {
            "donor_name": "Hotel Republic Rooftop Kitchen",
            "lat": 32.7157, "lon": -117.1611,
            "address": "421 W B St, San Diego, CA 92101",
            "food_type": "prepared_hot", "quantity_lbs": 12.0, "condition": "hot",
            "ready_at": _d(minutes=10), "expires_at": _d(minutes=90),
            "sb1383_tier": 2,
        },
    },
    "thesis": {
        "label": "3 — The thesis",
        "blurb": "50 lb of produce out of south East Village — small enough that one "
                 "site takes all of it, so the engine has to CHOOSE rather than "
                 "spread. With the forecast on it takes the FARTHER site, PATH "
                 "Connections, whose blocks count 141 people and are rising 32/yr, "
                 "over Father Joe's, which is closer and counts more people today "
                 "(178) but is falling 35/yr. Drag BETA_TREND to zero and the answer "
                 "flips to the closer site. That single interaction is the argument: "
                 "the model routes to where need is going, not only where it is. "
                 "Note the flip needs a scarce load — above ~70 lb both sites fill to "
                 "their daily budget and there is no choice left to make.",
        "donation": {
            "donor_name": "El Paisa Fresh Mexican Grill",
            "lat": 32.7075, "lon": -117.1425,
            "address": "2494 Imperial Avenue, San Diego, CA 92102",
            "food_type": "produce", "quantity_lbs": 50.0, "condition": "ambient",
            "ready_at": _d(minutes=15), "expires_at": _d(days=2),
            "sb1383_tier": 2,
        },
    },
}


# A fourth beat, for the supply-side argument. Not a single donation but a
# sequence: what happens when the mandate works and everybody donates at once.
SEQUENCE = {
    "saturation": {
        "label": "4 — Saturation",
        "blurb": "Twenty downtown restaurants each report 80 lb of surplus in the same "
                 "afternoon — 1,600 lb. Scored one at a time against a static world "
                 "every one of them routes to the same site, 4.6x its capacity, and "
                 "the surplus rots. With demand budgets and a running ledger the load "
                 "spreads, then the engine starts REFUSING donations once downtown's "
                 "measured demand is met. Refusing is the correct answer: the "
                 "bottleneck downtown is not supply, it is absorption.",
        "quantity_lbs": 80.0,
        "food_type": "produce",
        "condition": "ambient",
        "n_donors": 20,
        "ready_at": _d(minutes=15),
        "expires_at": _d(days=2),
    },
}


def sequence(name: str, donors: list[dict]) -> list[dict]:
    """Expand a sequence scenario into individual donations over real donors."""
    s = SEQUENCE[name]
    tier2 = [d for d in donors if d.get("sb1383_tier") == 2][: s["n_donors"]]
    return [{
        "donor_name": t["donor_name"], "lat": t["lat"], "lon": t["lon"],
        "address": t["address"], "food_type": s["food_type"],
        "quantity_lbs": s["quantity_lbs"], "condition": s["condition"],
        "ready_at": s["ready_at"], "expires_at": s["expires_at"],
        "sb1383_tier": 2,
    } for t in tier2]


def get(name: str) -> dict:
    s = SCENARIOS[name]
    return {**s, "donation": dict(s["donation"])}
