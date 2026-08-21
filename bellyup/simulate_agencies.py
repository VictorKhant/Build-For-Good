"""SIMULATED agency data -- placeholder until the real roster arrives.

Agencies are the middle layer in the revised model:

    small restaurant  --collected by-->  AGENCY PANTRY  --mobile pantry-->  hubspot

The restaurant pays nothing and donates for the tax deduction. The agency
absorbs both runs. Only agencies with a MOBILE pantry can serve a hubspot; a
fixed pantry cannot bring food to people sleeping on a block.

EVERY record here carries `"simulated": true`. Nothing in this file is a real
organisation, and no real organisation's name, address or hours appear in it.
Names are generic and descriptive by design -- inventing realistic-looking org
records is how fake data ends up quoted as real on a slide.

Replace by writing a real data/agencies.json with the same fields (see
AGENCY_SCHEMA.md) and deleting nothing else. No code changes needed.

    python simulate_agencies.py          # write data/agencies.json
    python simulate_agencies.py --show   # print what it generated
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
OUT = DATA_DIR / "agencies.json"

SEED = 42  # fixed so the demo is the same every run

# Neighbourhood anchors around and near the downtown count grid. Coordinates
# are approximate neighbourhood centres, not real addresses.
# Anchors. The first block sits INSIDE the downtown count grid, where the
# counted unsheltered population actually is; the rest are surrounding
# neighbourhoods. The split matters: a fixed pantry draws walk-in traffic only
# from people within walking distance, so one placed in Normal Heights has no
# measured demand at all. That is a real property of the problem, not a bug, and
# the mix here is chosen so the model has to show it.
AREAS_IN_GRID = [
    ("East Village",   32.7120, -117.1520),
    ("City Center",    32.7175, -117.1610),
    ("Cortez",         32.7205, -117.1585),
    ("Gaslamp",        32.7110, -117.1600),
    ("Marina",         32.7085, -117.1690),
    ("Columbia",       32.7185, -117.1690),
]

AREAS_NEARBY = [
    ("Barrio Logan",   32.6960, -117.1430),
    ("Logan Heights",  32.6990, -117.1330),
    ("Sherman Heights", 32.7060, -117.1420),
    ("Golden Hill",    32.7180, -117.1330),
    ("Little Italy",   32.7250, -117.1690),
    ("Bankers Hill",   32.7280, -117.1620),
    ("Grant Hill",     32.7080, -117.1370),
    ("North Park",     32.7480, -117.1290),
    ("City Heights",   32.7490, -117.0980),
    ("Hillcrest",      32.7480, -117.1620),
    ("Normal Heights", 32.7620, -117.1180),
    ("Mountain View",  32.6950, -117.1170),
]

AREAS = AREAS_IN_GRID + AREAS_NEARBY


KINDS = [
    ("Community Pantry",   True),   # (suffix, likely to run a mobile pantry)
    ("Food Pantry",        False),
    ("Neighborhood Pantry", True),
    ("Community Kitchen",  True),
    ("Outreach Pantry",    True),
    ("Family Resource Pantry", False),
]

ALL_TYPES = ["prepared_hot", "prepared_cold", "produce", "packaged_dry", "bakery", "dairy"]


def build(n_mobile: int = 7, n_fixed: int = 9) -> list[dict]:
    rng = random.Random(SEED)

    # Fixed pantries are placed mostly inside the grid, because a walk-in pantry
    # only functions where people already are. Mobile pantries are spread wider
    # -- they travel, so their address matters far less.
    in_grid = AREAS_IN_GRID[:]
    nearby = AREAS_NEARBY[:]
    rng.shuffle(in_grid)
    rng.shuffle(nearby)

    fixed_spots = in_grid[:5] + nearby[:max(0, n_fixed - 5)]
    mobile_spots = nearby[max(0, n_fixed - 5):][:n_mobile]
    if len(mobile_spots) < n_mobile:
        mobile_spots += in_grid[5:5 + (n_mobile - len(mobile_spots))]

    agencies = []
    for i in range(n_mobile + n_fixed):
        mobile = i < n_mobile
        spots = mobile_spots if mobile else fixed_spots
        idx_spot = (i if mobile else i - n_mobile) % len(spots)
        area, lat0, lon0 = spots[idx_spot]
        suffix = rng.choice([k for k, m in KINDS if m == mobile] or [KINDS[0][0]])

        # jitter a few hundred metres off the neighbourhood centre
        lat = round(lat0 + rng.uniform(-0.004, 0.004), 6)
        lon = round(lon0 + rng.uniform(-0.005, 0.005), 6)

        refrigerated = mobile or rng.random() < 0.5
        accepts = ["produce", "packaged_dry", "bakery"]
        if refrigerated:
            accepts += ["prepared_cold", "dairy"]
        if mobile and rng.random() < 0.6:
            accepts.append("prepared_hot")

        open_days = sorted(rng.sample(range(5), rng.choice([3, 4, 5])))
        start = rng.choice(["08:00", "09:00", "10:00"])
        end = rng.choice(["15:00", "16:00", "17:00", "18:00"])

        a = {
            "agency_id": f"sim_ag_{i + 1:02d}",
            "name": f"{area} {suffix}",
            "simulated": True,
            "data_source": "SIMULATED — replace before any public claim",
            "address": f"{area}, San Diego, CA",
            "lat": lat, "lon": lon,
            "neighborhood": area,

            # --- collection side (leg 1: restaurant -> pantry) ---
            "collects_donations": True,
            "wage_per_hour": round(rng.uniform(19.0, 25.0), 2),
            "staff_per_run": rng.choice([1, 1, 1, 2]),
            "cost_per_km": round(rng.uniform(0.38, 0.52), 2),
            "has_refrigerated_vehicle": refrigerated,
            "operating_windows": [
                {"dow": d, "start": start, "end": end} for d in open_days
            ],

            # --- pantry side (what it can take in and hold) ---
            "accepts": sorted(set(accepts)),
            "storage": {
                "refrigerated": refrigerated,
                "frozen": refrigerated and rng.random() < 0.5,
                "hot_holding": "prepared_hot" in accepts,
            },
            "intake_capacity_lbs_per_day": rng.choice([300, 400, 600, 800, 1200]),

            # --- distribution side (leg 2: pantry -> hubspot) ---
            "has_mobile_pantry": mobile,
            "mobile_capacity_lbs": rng.choice([150, 200, 250, 300]) if mobile else 0,
            # A mix of morning and evening runs. Evening outreach is common --
            # people are settled at a hubspot by then -- and it is what lets a
            # lunchtime donation reach someone the same day.
            "mobile_windows": ([
                {"dow": d, **rng.choice([
                    {"start": "07:00", "end": "12:00"},
                    {"start": "14:00", "end": "19:00"},
                    {"start": "16:00", "end": "20:00"},
                ])}
                for d in sorted(rng.sample(range(7), rng.choice([4, 5, 6])))
            ] if mobile else []),
            "max_hubspot_stops": rng.choice([2, 3, 3]) if mobile else 0,

            "verified": False,
            "verification_note": "SIMULATED PLACEHOLDER. Not a real organisation.",
        }
        agencies.append(a)

    return agencies


def main(show: bool = False) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    agencies = build()
    OUT.write_text(json.dumps({
        "simulated": True,
        "warning": "PLACEHOLDER DATA. Every agency below is invented. Replace with "
                   "the real roster before presenting, publishing or contacting anyone.",
        "schema": "AGENCY_SCHEMA.md",
        "count": len(agencies),
        "agencies": agencies,
    }, indent=2))

    mob = [a for a in agencies if a["has_mobile_pantry"]]
    print(f"wrote {OUT}")
    print(f"  {len(agencies)} simulated agencies — {len(mob)} with mobile pantries "
          f"(only these can serve a hubspot)")
    if show:
        print()
        print(f"  {'name':38} {'mobile':>7} {'intake':>7} {'fridge':>7}  days")
        for a in agencies:
            days = "".join("MTWTFSS"[w["dow"]] for w in a["operating_windows"])
            print(f"  {a['name'][:36]:38} "
                  f"{('yes ' + str(a['mobile_capacity_lbs']) + 'lb') if a['has_mobile_pantry'] else 'no':>7} "
                  f"{a['intake_capacity_lbs_per_day']:7} "
                  f"{'yes' if a['has_refrigerated_vehicle'] else 'no':>7}  {days}")


if __name__ == "__main__":
    main(show="--show" in sys.argv)
