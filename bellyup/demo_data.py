"""Loads the real datasets into the shapes the front end expects.

This is Oscar's `scripts/build_demo_data.py` turned into a live loader. His
version baked `demo/data.js` at build time; serving it from the API instead
means a restaurant can register during the demo and appear alongside the
pre-existing reports without regenerating a file.

Sources, all real (see dataset/README_DATA_PROVENANCE.md):
  hotspots.csv         382 downtown blocks, need in person-equivalents
  businesses.csv       31 food businesses that could donate
  agencies.csv         5 collection / redistribution agencies
  mobile_pantries.csv  14 distribution sites

The only simulated part is which businesses happen to report surplus tonight
and how much -- voluntary end-of-day reporting is the thing that does not
exist yet, which is the whole point of the platform.
"""

from __future__ import annotations

import csv
import random
import re
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "dataset"

SEED = 20260820
N_REPORTING = 14

# The demo models one fixed evening: Thursday, the 3rd Thursday of the month.
# Pantry availability resolves against this so the demo is deterministic.
DEMO_WEEKDAY, DEMO_ORDINAL = "Thursday", 3
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# The clock the board runs on. It MUST agree with DEMO_WEEKDAY/DEMO_ORDINAL
# above: pantry availability is resolved against "the 3rd Thursday", so a
# clock reading Wednesday would have units on site on the wrong night. 18:30
# is the evening these reports come in — kitchens close, surplus is known.
from datetime import datetime as _dt
from datetime import timedelta
DEMO_NOW = _dt(2026, 8, 20, 18, 30)   # Thursday, 3rd Thursday of Aug 2026

# Agencies with no coordinates in the source data, hand-placed to the same
# ~150 m standard the dataset itself uses. A.B. Jones & Co. runs transport
# only, with no fixed site, so it cannot anchor a HQ -> pickup -> drop model.
DEMO_GEOCODES = {
    "Feeding San Diego": (-117.1780, 32.8930),
    "Feeding San Diego (South Bay)": (-117.1040, 32.6996),
    "Catholic Charities Diocese of San Diego": (-117.0995, 32.7846),
}

SHORT_NAMES = {
    "St. Vincent de Paul / Father Joe's Villages (Imperial Ave)": "Father Joe's (Imperial Ave)",
    "St. Vincent De Paul Father Joe's Villages (E Street)": "Father Joe's (E Street)",
    "San Diego Broadway Spanish Seventh Day Adventist": "Broadway Spanish SDA",
    "31st Street Seventh Day Adventist Church": "31st St SDA Church",
}

CONSTANTS = {
    "LBS_PER_MEAL": 1.2,          # Feeding America conversion
    "WAGE_PER_HR": 17.75,         # City of San Diego minimum wage, eff. 2026-01-01
    "COST_PER_MILE": 0.76,        # IRS standard mileage rate, eff. 2026-07-01
    "MEAL_VALUE": 4.25,           # social value per meal served
    "FMV_PER_LB": 1.79,           # fair market value, for the deduction estimate
    "AVG_SPEED_MPH": 18,          # city driving average
    "ROAD_FACTOR": 1.3,           # haversine -> road distance
    "HANDLING_MIN": 25,           # load + unload/serve per run
    "MIN_CANDIDATE_NEED": 1.0,    # blocks below this show on the map, never match
    "ACCESS_BOOST_MAX": 0.5,      # reward boost where weekly food access is poor
    "AGENCY_CAPACITY_LBS": 2000,  # box truck
    "PANTRY_CAPACITY_LBS": 150,   # pantry van / mobile unit
    "DEMO_DATE": "2026-08-20",    # the fixed demo evening (a 3rd Thursday)
    "MAX_DROPS_PER_NIGHT": 2,     # serving limit: deliveries per hotspot per night
    "DROPOFF_CREDIT": 0.5,        # value of stocking a pantry vs feeding a block tonight

    # --- added by the merge: the donor now states expiry and a pickup window,
    # so time has to enter the model ---
    "FRESHNESS_FLOOR": 0.35,      # value retained by food that arrives late in life
    "SAFETY_MARGIN_MIN": 30,      # food must land this far before stated expiry
    "MAX_TRANSIT_MIN": {"prepared": 120, "packaged/produce": 480},
}


def available_tonight(day_list: str) -> bool:
    """Does this schedule put staff on site on the demo evening?

    Handles 'Daily', 'Friday', 'Tuesday-Thursday', '1st & 4th Thursday'.
    """
    d = (day_list or "").strip()
    if d.lower() == "daily":
        return True
    if "-" in d:
        a, _, b = (x.strip() for x in d.partition("-"))
        if a in WEEKDAYS and b in WEEKDAYS:
            return WEEKDAYS.index(a) <= WEEKDAYS.index(DEMO_WEEKDAY) <= WEEKDAYS.index(b)
    if DEMO_WEEKDAY not in d:
        return False
    ordinals = [int(n) for n in re.findall(r"(\d)(?:st|nd|rd|th)", d)]
    return DEMO_ORDINAL in ordinals if ordinals else True


def pretty_location(loc: str) -> str:
    """'17TH ST & K ST' -> '17th St & K St', '09TH AV' -> '9th Av'."""
    words = []
    for w in loc.split():
        words.append((w.lstrip("0").lower() or "0") if w[0].isdigit() else w.capitalize())
    return " ".join(words)


def _rows(name: str) -> list[dict]:
    with open(DATA_DIR / name, newline="") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------
# hotspots
# --------------------------------------------------------------------------

def load_hotspots() -> list[dict]:
    out = []
    for r in _rows("hotspots.csv"):
        need = float(r["need"])
        if need < 0.5:
            continue
        out.append({
            "id": r["block_id"],
            "location": pretty_location(r["location"]),
            "area": r["area"],
            "lon": float(r["lon"]), "lat": float(r["lat"]),
            "need": need,
            "rank": int(r["need_rank"]),
            "priority": r["priority"],
            "persistence": float(r["persistence"]) if r["persistence"] else None,
            "accessDays": float(r["food_access_days_per_week"] or 0),
            "unservedDaily": float(r["unserved_need_daily"] or 0),
        })
    out.sort(key=lambda h: -h["need"])
    return out


# --------------------------------------------------------------------------
# suppliers
# --------------------------------------------------------------------------

def _simulate_report(b: dict, order: int, rng: random.Random,
                     rng_window: random.Random) -> dict:
    """Quantities and items draw from `rng` in exactly the original order.

    The pickup-window jitter uses a SEPARATE stream. Drawing it from the same
    generator would shift every later supplier's quantity, silently changing
    numbers that have already been rehearsed against.
    """
    t = b["facility_type"]
    if t == "grocery":
        lbs = rng.randint(120, 420)
        items = rng.choice([
            "day-old bakery, produce, dairy nearing date",
            "produce trims, deli overstock, packaged goods",
            "bakery, prepared deli trays, bagged produce",
        ])
        hours = 36
    elif t == "hotel":
        rooms = 250
        for tok in b["size_metric"].split():
            if tok.isdigit():
                rooms = int(tok)
                break
        lbs = max(25, int(rooms * rng.uniform(0.10, 0.22)))
        items = rng.choice([
            "banquet buffet trays (hot line, chafing)",
            "conference catering overage, plated entrees",
            "breakfast buffet + event catering leftovers",
        ])
        hours = 4
    elif t == "venue":
        lbs = rng.randint(180, 600)
        items = rng.choice([
            "concession overstock + suite catering",
            "event concessions, boxed meals unclaimed",
        ])
        hours = 6
    else:  # health
        lbs = rng.randint(60, 180)
        items = "cafeteria service line overage, packaged meals"
        hours = 12

    hh, mm = divmod(17 * 60 + 25 + order * rng.randint(11, 23), 60)
    reported = f"{hh:02d}:{mm:02d}"
    # Pickup window: from the report time to a couple of hours later, which is
    # how long a kitchen will realistically hold food on a loading dock.
    eh, em = divmod(hh * 60 + mm + rng_window.choice([90, 120, 150]), 60)
    xh, xm = divmod(hh * 60 + mm + hours * 60, 60)
    return {
        "lbs": lbs, "items": items, "time": reported,
        "pickupFrom": reported, "pickupTo": f"{eh % 24:02d}:{em:02d}",
        "expiresAt": f"{xh % 24:02d}:{xm:02d}",
        "expiresInHours": hours,
        "freshness": "fresh",
    }


def load_suppliers(hotspots=None) -> list[dict]:
    """Every business, curated and self-registered, with tonight's reports.

    The simulated-report draw runs over the CURATED rows only. Self-registered
    restaurants are appended to businesses.csv, and if they took part in the
    shuffle then signing one up would re-order the draw and silently change
    which fourteen businesses report and how much -- numbers that have already
    been rehearsed against.
    """
    import registry

    rows = registry.businesses()
    curated = [r for r in rows if not registry.is_self_registered(r)]
    mine = [r for r in rows if registry.is_self_registered(r)]

    rng = random.Random(SEED)
    rng_window = random.Random(SEED + 1)
    rng.shuffle(curated)

    out = []
    for i, b in enumerate(curated):
        out.append({
            "id": f"S{i:02d}",
            "name": b["business_name"],
            "type": b["facility_type"],
            "address": b["address"],
            "lon": float(b["lon"]), "lat": float(b["lat"]),
            "surplus": b["surplus_type"],
            "sb1383Tier": b.get("sb1383_tier") or None,
            "registered": False,
            "report": (_simulate_report(b, i, rng, rng_window)
                       if i < N_REPORTING else None),
        })
    out.sort(key=lambda s: (s["report"] is None, s["report"]["time"] if s["report"] else ""))

    ids = registry.next_id()
    own = []
    for n, b in enumerate(mine):
        own.append({
            "id": f"R{100 + n}",
            "name": b["business_name"],
            "type": b["facility_type"] or "restaurant",
            "address": b["address"],
            "lon": float(b["lon"]), "lat": float(b["lat"]),
            "surplus": b["surplus_type"] or "prepared",
            "sb1383Tier": b.get("sb1383_tier") or None,
            "registered": True,
            "report": None,
        })

    # persisted reports override the simulated ones, and are the ONLY source
    # of a report for a self-registered restaurant
    saved = registry.reports()
    for s in own + out:
        if s["name"] in saved:
            rep = saved[s["name"]]
            if rep is None:
                s["report"] = None
                continue
            if rep.get("_surplus_type"):
                s["surplus"] = rep["_surplus_type"]
            s["report"] = {k: v for k, v in rep.items() if not k.startswith("_")}

    gone = registry.opted_out_names()
    return [s for s in own + out if s["name"] not in gone]


# --------------------------------------------------------------------------
# collectors: agencies and mobile pantry units
# --------------------------------------------------------------------------

def load_agencies() -> list[dict]:
    """Agencies from agencies.csv, split by whether they can actually collect.

    `mobile_capable` is load-bearing. An agency with a vehicle can be sent to a
    restaurant and on to a hotspot -- that is what a collector is. An agency
    marked `no` is a fixed site that RECEIVES donations; giving it a box truck
    would invent a fleet it does not have and quietly change every dispatch.

    Both kinds are returned, tagged. Only `mobileCapable` ones become
    collectors; the rest ride along as drop-off points on the map.
    """
    out = []
    for a in _rows("agencies.csv"):
        name = a["agency_name"]
        lon, lat, geocode = a["lon"], a["lat"], a["geocode_method"]
        if not lon and name in DEMO_GEOCODES:
            lon, lat = DEMO_GEOCODES[name]
            geocode = "approximate_manual_demo"
        if not lon:
            continue     # transport-only, no fixed site to route from

        # "unknown" is treated as capable: the five original agencies are the
        # county's distribution networks, and two are only unknown because the
        # roster did not say. A new row must say `no` to be a fixed site.
        mobile = (a.get("mobile_capable") or "unknown").strip().lower() != "no"

        out.append({
            "id": name.split()[0].upper()[:4] + str(len(out)),
            "name": name,
            "program": a["program"],
            "lon": float(lon), "lat": float(lat),
            "acceptsPrepared": a["accepts_prepared"] == "yes",
            "note": a["note"],
            "geocode": geocode,
            "mobileCapable": mobile,
            "agencyType": (a.get("agency_type") or "").strip(),
            "role": (a.get("role") or "").strip(),
            "address": a.get("address", ""),
            "phone": a.get("phone", ""),
        })
    return out


def collecting_agencies(agencies=None) -> list[dict]:
    """Only the agencies that can be dispatched to collect."""
    agencies = load_agencies() if agencies is None else agencies
    return [a for a in agencies if a.get("mobileCapable", True)]


def dropoff_agencies(agencies=None) -> list[dict]:
    """Fixed sites that receive donations but cannot go and get them."""
    agencies = load_agencies() if agencies is None else agencies
    return [a for a in agencies if not a.get("mobileCapable", True)]


def load_pantries() -> list[dict]:
    out = []
    for i, p in enumerate(_rows("mobile_pantries.csv")):
        avail = available_tonight(p["day_list"])
        public = p["downtown_relevant"] == "True"
        out.append({
            "id": f"P{i:02d}",
            "name": SHORT_NAMES.get(p["site_name"], p["site_name"]),
            "operator": p["operator"],
            "lon": float(p["lon"]), "lat": float(p["lat"]),
            "program": p["program"],
            "schedule": p["day_list"] + (f" {p['start_time']}–{p['end_time']}"
                                         if p["end_time"] else ""),
            "daysPerWeek": float(p["days_per_week"]),
            "startTime": p["start_time"], "endTime": p["end_time"],
            "acceptsPrepared": "meal" in p["program"].lower(),
            "availableTonight": avail,
            "dispatchable": avail and public,
            "whyNot": (None if avail and public
                       else "serves home-bound individuals only" if not public
                       else f"no unit tonight — runs {p['day_list']}"),
        })
    return out


# --------------------------------------------------------------------------
# history -- the past week's confirmed deliveries
# --------------------------------------------------------------------------

def load_history(suppliers=None, agencies=None, pantries=None,
                 hotspots=None) -> list[dict]:
    """A seeded ledger of the previous seven evenings.

    Drawn from its OWN generator, and only after the caller has built
    everything else, so adding history cannot disturb tonight's simulated
    reports. Gives the ledger view something to sit on: a platform with no
    yesterday looks like a prototype.
    """
    import math

    suppliers = suppliers if suppliers is not None else load_suppliers()
    agencies = agencies if agencies is not None else load_agencies()
    pantries = pantries if pantries is not None else load_pantries()
    hotspots = hotspots if hotspots is not None else load_hotspots()

    C = CONSTANTS
    rng = random.Random(SEED + 7)

    def road_mi(a, b):
        rad = math.pi / 180
        dlat = (b["lat"] - a["lat"]) * rad
        dlon = (b["lon"] - a["lon"]) * rad
        x = (math.sin(dlat / 2) ** 2 + math.cos(a["lat"] * rad)
             * math.cos(b["lat"] * rad) * math.sin(dlon / 2) ** 2)
        return 2 * 3958.76 * math.asin(math.sqrt(x)) * C["ROAD_FACTOR"]

    hist_lbs = {"grocery": (120, 420), "hotel": (30, 160),
                "venue": (180, 600), "health": (60, 180),
                "restaurant": (25, 120)}
    # only agencies that can be dispatched -- a fixed drop-off site never
    # drove anywhere, so it cannot appear in a delivery history either
    collectors = (
        [{**a, "kind": "agency", "cap": C["AGENCY_CAPACITY_LBS"]}
         for a in agencies if a.get("mobileCapable", True)]
        + [{**p, "kind": "pantry", "cap": C["PANTRY_CAPACITY_LBS"]}
           for p in pantries if p["whyNot"] != "serves home-bound individuals only"]
    )
    top_blocks = hotspots[:30]
    demo_date = _dt.fromisoformat(C["DEMO_DATE"]).date()

    # only businesses that were on the platform back then
    pool = [s for s in suppliers if not s.get("registered")]
    if not pool or not collectors or not top_blocks:
        return []

    out = []
    for back in range(7, 0, -1):
        day = demo_date - timedelta(days=back)
        for _ in range(rng.randint(2, 5)):
            b = rng.choice(pool)
            eligible = [c for c in collectors
                        if b["surplus"] != "prepared" or c["acceptsPrepared"]]
            if not eligible:
                continue
            col = rng.choice(eligible)
            lbs = rng.randint(*hist_lbs.get(b["type"], (60, 200)))
            collected = min(lbs, col["cap"])
            meals = collected / C["LBS_PER_MEAL"]
            h = rng.choice(top_blocks)
            served = min(meals, h["need"])
            boost = 1 + C["ACCESS_BOOST_MAX"] * (7 - min(h["accessDays"], 7)) / 7
            reward = served * C["MEAL_VALUE"] * boost + (meals - served) * C["MEAL_VALUE"] * 0.5
            miles = road_mi(col, b) + road_mi(b, h)
            cost = ((miles / C["AVG_SPEED_MPH"] * 60 + C["HANDLING_MIN"]) / 60
                    * C["WAGE_PER_HR"] + miles * C["COST_PER_MILE"])
            out.append({
                "receipt": f"BU-{day:%Y%m%d}-{len(out):03d}",
                "date": day.isoformat(),
                "time": f"{rng.randint(17, 20)}:{rng.randint(0, 59):02d}",
                "supplierId": b["id"], "supplier": b["name"],
                "lbs": lbs, "collectedLbs": collected,
                "servedMeals": round(served), "surplusMeals": round(meals - served),
                "collector": col["name"], "kind": col["kind"],
                "hotspotId": h["id"], "hotspot": h["location"],
                "fmv": round(lbs * C["FMV_PER_LB"], 2),
                "net": round(reward - cost, 2),
            })
    return out
