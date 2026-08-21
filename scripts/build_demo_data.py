"""Build demo/data.js from the datasets in newdata/.

Reads:
  newdata/hotspots.csv    382 downtown blocks with need (person-equivalents)
  newdata/businesses.csv  31 food businesses (suppliers)
  newdata/agencies.csv    5 collection/redistribution agencies

Emits demo/data.js — a single JS file (no fetch, works over file://) holding:
  HOTSPOTS   blocks with need >= 0.5 (map layer); need >= 1 are match candidates
  SUPPLIERS  all businesses, ~half carrying a simulated "tonight" surplus report
  AGENCIES   the 4 fixed-site agencies (A.B. Jones has no HQ, so no cost model)
  CONSTANTS  cost/conversion figures from README_DATA_PROVENANCE.md sources

The surplus reports are SIMULATED (seeded, reproducible): the platform's real
input would be voluntary end-of-day reporting, which doesn't exist yet.
Quantities are scaled to facility type/size. Everything else is real data.
"""
import csv, json, random, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
rng = random.Random(20260820)

# The demo models one fixed evening: Thursday 2026-08-20 — the 3rd Thursday
# of the month. Pantry-unit availability is resolved against this at build
# time so the demo stays deterministic.
DEMO_WEEKDAY, DEMO_ORDINAL = "Thursday", 3
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def available_tonight(day_list):
    """Does this schedule put staff on site on the demo evening?
    Handles 'Daily', 'Friday', 'Tuesday-Thursday', '1st & 4th Thursday'."""
    d = day_list.strip()
    if d.lower() == "daily":
        return True
    if "-" in d:
        a, _, b = [*map(str.strip, d.partition("-"))]
        if a in WEEKDAYS and b in WEEKDAYS:
            return WEEKDAYS.index(a) <= WEEKDAYS.index(DEMO_WEEKDAY) <= WEEKDAYS.index(b)
    if DEMO_WEEKDAY not in d:
        return False
    ordinals = [int(n) for n in re.findall(r"(\d)(?:st|nd|rd|th)", d)]
    return DEMO_ORDINAL in ordinals if ordinals else True

# Agencies missing coordinates in the source data, hand-placed for the demo
# (same ~±150 m standard the dataset itself uses). A.B. Jones & Co. has no
# fixed site, so it cannot anchor the HQ->pickup->dropoff cost model.
DEMO_GEOCODES = {
    "Feeding San Diego": (-117.1780, 32.8930),
    "Feeding San Diego (South Bay)": (-117.1040, 32.6996),
    "Catholic Charities Diocese of San Diego": (-117.0995, 32.7846),
}

def pretty_location(loc):
    """'17TH ST & K ST' -> '17th St & K St', '09TH AV' -> '9th Av'."""
    words = []
    for w in loc.split():
        if w[0].isdigit():
            words.append(w.lstrip("0").lower() or "0")
        else:
            words.append(w.capitalize())
    return " ".join(words)


# ---------------------------------------------------------------- hotspots
hotspots = []
for r in csv.DictReader(open(ROOT / "newdata/hotspots.csv")):
    need = float(r["need"])
    if need < 0.5:
        continue
    hotspots.append({
        "id": r["block_id"],
        "location": pretty_location(r["location"]),
        "area": r["area"],
        "lon": float(r["lon"]),
        "lat": float(r["lat"]),
        "need": need,
        "rank": int(r["need_rank"]),
        "priority": r["priority"],
        "persistence": float(r["persistence"]) if r["persistence"] else None,
        "accessDays": float(r["food_access_days_per_week"] or 0),
        "unservedDaily": float(r["unserved_need_daily"] or 0),
    })
hotspots.sort(key=lambda h: -h["need"])

# ---------------------------------------------------------------- suppliers
def simulate_report(b, order):
    """Seeded surplus quantities scaled to facility type."""
    t = b["facility_type"]
    if t == "grocery":
        lbs = rng.randint(120, 420)
        items = rng.choice([
            "day-old bakery, produce, dairy nearing date",
            "produce trims, deli overstock, packaged goods",
            "bakery, prepared deli trays, bagged produce",
        ])
    elif t == "hotel":
        rooms = 250
        for tok in b["size_metric"].split():
            if tok.isdigit():
                rooms = int(tok); break
        lbs = max(25, int(rooms * rng.uniform(0.10, 0.22)))
        items = rng.choice([
            "banquet buffet trays (hot line, chafing)",
            "conference catering overage, plated entrees",
            "breakfast buffet + event catering leftovers",
        ])
    elif t == "venue":
        lbs = rng.randint(180, 600)
        items = rng.choice([
            "concession overstock + suite catering",
            "event concessions, boxed meals unclaimed",
        ])
    else:  # health
        lbs = rng.randint(60, 180)
        items = "cafeteria service line overage, packaged meals"
    hh, mm = divmod(17 * 60 + 25 + order * rng.randint(11, 23), 60)
    return {"lbs": lbs, "items": items, "time": f"{hh:02d}:{mm:02d}"}

businesses = list(csv.DictReader(open(ROOT / "newdata/businesses.csv")))
rng.shuffle(businesses)
n_reporting = 14
suppliers = []
for i, b in enumerate(businesses):
    s = {
        "id": f"S{i:02d}",
        "name": b["business_name"],
        "type": b["facility_type"],
        "address": b["address"],
        "lon": float(b["lon"]),
        "lat": float(b["lat"]),
        "surplus": b["surplus_type"],  # 'prepared' | 'packaged/produce'
        "report": simulate_report(b, i) if i < n_reporting else None,
    }
    suppliers.append(s)
# feed reads newest-first; give it a stable order by report time
suppliers.sort(key=lambda s: (s["report"] is None, s["report"]["time"] if s["report"] else ""))

# ---------------------------------------------------------------- agencies
agencies = []
for a in csv.DictReader(open(ROOT / "newdata/agencies.csv")):
    name = a["agency_name"]
    lon, lat = a["lon"], a["lat"]
    geocode = a["geocode_method"]
    if not lon and name in DEMO_GEOCODES:
        lon, lat = DEMO_GEOCODES[name]
        geocode = "approximate_manual_demo"
    if not lon:
        continue  # A.B. Jones & Co. — transport-only, no fixed HQ
    agencies.append({
        "id": name.split()[0].upper()[:4] + str(len(agencies)),
        "name": name,
        "program": a["program"],
        "lon": float(lon),
        "lat": float(lat),
        "acceptsPrepared": a["accepts_prepared"] == "yes",
        "note": a["note"],
        "geocode": geocode,
    })

# ---------------------------------------------------------------- pantries
# Mobile pantry sites can also dispatch a unit to collect and distribute —
# but only when their schedule has staff on site tonight, and only sites
# serving the general public (Special Delivery serves home-bound only).
SHORT_NAMES = {
    "St. Vincent de Paul / Father Joe's Villages (Imperial Ave)": "Father Joe's (Imperial Ave)",
    "St. Vincent De Paul Father Joe's Villages (E Street)": "Father Joe's (E Street)",
    "San Diego Broadway Spanish Seventh Day Adventist": "Broadway Spanish SDA",
    "31st Street Seventh Day Adventist Church": "31st St SDA Church",
}
pantries = []
for i, p in enumerate(csv.DictReader(open(ROOT / "newdata/mobile_pantries.csv"))):
    avail = available_tonight(p["day_list"])
    public = p["downtown_relevant"] == "True"
    pantries.append({
        "id": f"P{i:02d}",
        "name": SHORT_NAMES.get(p["site_name"], p["site_name"]),
        "operator": p["operator"],
        "lon": float(p["lon"]),
        "lat": float(p["lat"]),
        "program": p["program"],
        "schedule": p["day_list"] + (f" {p['start_time']}–{p['end_time']}" if p["end_time"] else ""),
        "daysPerWeek": float(p["days_per_week"]),
        "acceptsPrepared": "meal" in p["program"].lower(),
        "availableTonight": avail,
        "dispatchable": avail and public,
        "whyNot": (None if avail and public
                   else "serves home-bound individuals only" if not public
                   else f"no unit tonight — runs {p['day_list']}"),
    })

# ---------------------------------------------------------------- constants
constants = {
    "LBS_PER_MEAL": 1.2,        # Feeding America conversion
    "WAGE_PER_HR": 17.75,       # City of San Diego minimum wage, eff. 2026-01-01
    "COST_PER_MILE": 0.76,      # IRS standard mileage rate, eff. 2026-07-01
    "MEAL_VALUE": 4.25,         # demo assumption: social value per meal served ($)
    "FMV_PER_LB": 1.79,         # demo assumption: fair market value of donated food, for tax-deduction estimate
    "AVG_SPEED_MPH": 18,        # city driving average (demo assumption)
    "ROAD_FACTOR": 1.3,         # haversine -> road distance multiplier (demo assumption)
    "HANDLING_MIN": 25,         # load + unload/serve time per run (demo assumption)
    "MIN_CANDIDATE_NEED": 1.0,  # blocks below this are shown but not matched
    "ACCESS_BOOST_MAX": 0.5,    # reward boost for blocks with poor weekly food access
    "AGENCY_CAPACITY_LBS": 2000,  # box truck (demo assumption)
    "PANTRY_CAPACITY_LBS": 150,   # pantry van / mobile unit (demo assumption)
}

out = ROOT / "demo/data.js"
out.write_text(
    "// Generated by scripts/build_demo_data.py — do not edit by hand.\n"
    f"const HOTSPOTS = {json.dumps(hotspots, indent=1)};\n"
    f"const SUPPLIERS = {json.dumps(suppliers, indent=1)};\n"
    f"const AGENCIES = {json.dumps(agencies, indent=1)};\n"
    f"const PANTRIES = {json.dumps(pantries, indent=1)};\n"
    f"const CONSTANTS = {json.dumps(constants, indent=1)};\n"
)
reporting = [s for s in suppliers if s["report"]]
dispatchable = [p for p in pantries if p["dispatchable"]]
print(f"wrote {out}")
print(f"  hotspots: {len(hotspots)} (candidates need>=1: {sum(1 for h in hotspots if h['need'] >= 1)})")
print(f"  suppliers: {len(suppliers)} ({len(reporting)} reporting tonight, "
      f"{sum(s['report']['lbs'] for s in reporting)} lbs total)")
print(f"  agencies: {len(agencies)} matchable")
print(f"  pantries: {len(pantries)} ({len(dispatchable)} units available tonight: "
      + ", ".join(p['name'] for p in dispatchable) + ")")
