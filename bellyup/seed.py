"""Builds nonprofits, destinations, hubspots and donors.

Seed quality beats seed volume. Every hand-entered fact carries a `verified`
flag and a `source` note; anything marked verified=False needs a human to
confirm it before the demo. Nothing here silently invents an operating hour.

Run:  python seed.py            (uses cached geocodes)
      python seed.py --refresh  (re-geocodes everything)
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

import needs

DATA_DIR = Path(__file__).resolve().parent / "data"
SOURCE_DIR = needs.DATA_DIR
GEOCACHE = DATA_DIR / "geocode_cache.json"

CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

WEEKDAY = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
           "friday": 4, "saturday": 5, "sunday": 6}
ORDINAL = {"1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3,
           "4th": 4, "fourth": 4, "5th": 5, "fifth": 5, "last": -1}

# When a site says "until food runs out" there is no published end time.
DEFAULT_WINDOW_MIN = 120


# --------------------------------------------------------------------------
# geocoding -- Census Geocoder: free, no key, federal
# --------------------------------------------------------------------------

def _load_cache() -> dict:
    return json.loads(GEOCACHE.read_text()) if GEOCACHE.exists() else {}


def _save_cache(cache: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GEOCACHE.write_text(json.dumps(cache, indent=2, sort_keys=True))


def geocode(address: str, cache: dict, refresh: bool = False) -> tuple[float, float] | None:
    key = address.strip().lower()
    if key in cache and not refresh:
        v = cache[key]
        return (v["lat"], v["lon"]) if v else None
    try:
        r = requests.get(CENSUS, params={
            "address": address, "benchmark": "Public_AR_Current", "format": "json",
        }, timeout=25)
        r.raise_for_status()
        m = r.json()["result"]["addressMatches"]
        if not m:
            cache[key] = None
            return None
        c = m[0]["coordinates"]
        cache[key] = {"lat": round(c["y"], 6), "lon": round(c["x"], 6),
                      "matched": m[0]["matchedAddress"]}
        time.sleep(0.15)
        return cache[key]["lat"], cache[key]["lon"]
    except Exception as exc:  # network hiccup should not kill the whole seed
        print(f"    ! geocode failed for {address!r}: {exc}")
        return None


# --------------------------------------------------------------------------
# free-text hours -> open_windows
# --------------------------------------------------------------------------

_TIME_RANGE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\s*(?:-|–|—|to|until|thru|through)\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", re.I)
_TIME_ONE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", re.I)


def _to24(h: int, m: int, mer: str | None, fallback_pm: bool = False) -> int:
    mer = (mer or "").replace(".", "").lower()
    if mer == "pm" and h != 12:
        h += 12
    elif mer == "am" and h == 12:
        h = 0
    elif not mer and fallback_pm and h < 8:
        h += 12
    return h * 60 + m


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_hours(days_text: str, hours_text: str) -> tuple[list[dict], str]:
    """Turn the scraped free text into open_windows. Returns (windows, note)."""
    hours_text = (hours_text or "").strip()
    days_text = (days_text or "").strip()

    dows = [WEEKDAY[d.strip().lower()] for d in days_text.split(";")
            if d.strip().lower() in WEEKDAY]
    if not dows:
        # some entries only list days inside the free text
        dows = sorted({WEEKDAY[w] for w in WEEKDAY if re.search(rf"\b{w}s?\b", hours_text, re.I)})
    if not dows:
        return [], "no days published — site says to phone for current hours"

    weeks = sorted({ORDINAL[o] for o in ORDINAL
                    if re.search(rf"\b{o}\b", hours_text, re.I)})

    rng = _TIME_RANGE.search(hours_text)
    if rng:
        end_mer = rng.group(6)
        start = _to24(int(rng.group(1)), int(rng.group(2) or 0), rng.group(3),
                      fallback_pm=(end_mer or "").replace(".", "").lower() == "pm"
                      and int(rng.group(1)) > int(rng.group(4)))
        end = _to24(int(rng.group(4)), int(rng.group(5) or 0), end_mer)
        note = "parsed from published hours"
        if end <= start:  # e.g. "10:00 am - 12:00 pm" mis-parsed, or crosses noon
            end = start + DEFAULT_WINDOW_MIN
            note = "end time ambiguous — assumed a 2h window"
    else:
        one = _TIME_ONE.search(hours_text)
        if not one:
            return [], f"no parseable time in: {hours_text[:70]!r}"
        start = _to24(int(one.group(1)), int(one.group(2) or 0), one.group(3))
        end = start + DEFAULT_WINDOW_MIN
        note = "open-ended ('until food runs out') — assumed a 2h window"

    windows = []
    for d in dows:
        w = {"dow": d, "start": _fmt(start), "end": _fmt(end)}
        if weeks:
            w["weeks_of_month"] = weeks
        windows.append(w)

    if weeks:
        note += f"; monthly cadence weeks {weeks}"
    return windows, note


# --------------------------------------------------------------------------
# what a distribution type accepts and can store
# --------------------------------------------------------------------------

def profile_for(dist_type: str, food_provided: str) -> dict:
    t = (dist_type or "").upper()
    fp = (food_provided or "").lower()

    if "PRODUCE" in t:
        return {"accepts": ["produce", "bakery"],
                "storage": {"refrigerated": False, "frozen": False, "hot_holding": False},
                "capacity_lbs_per_visit": 1500.0,
                "capacity_basis": "25-30 lb per household across a produce distribution"}
    if "EFAP" in t:
        accepts = ["packaged_dry", "produce"]
        if "dairy" in fp:
            accepts.append("dairy")
        return {"accepts": accepts,
                "storage": {"refrigerated": True, "frozen": "frozen" in fp,
                            "hot_holding": False},
                "capacity_lbs_per_visit": 800.0,
                "capacity_basis": "shelf-stable package distribution, refrigerated/frozen on site"}
    if "SENIOR" in t:
        return {"accepts": ["packaged_dry", "produce"],
                "storage": {"refrigerated": False, "frozen": False, "hot_holding": False},
                "capacity_lbs_per_visit": 400.0,
                "capacity_basis": "senior package distribution, smaller volume"}
    return {"accepts": ["packaged_dry", "produce", "bakery"],
            "storage": {"refrigerated": False, "frozen": False, "hot_holding": False},
            "capacity_lbs_per_visit": 600.0,
            "capacity_basis": "assorted items to a partner nonprofit"}


# --------------------------------------------------------------------------
# hand-curated nonprofits (they perform pickup AND delivery)
# --------------------------------------------------------------------------
# HQ spread is the point: the Food Bank and Feeding San Diego sit in Sorrento
# Valley, ~20 km from Gaslamp, while Father Joe's and the Rescue Mission are
# downtown. For a given restaurant the cheapest org is often not the biggest.

NONPROFITS = [
    {"org_id": "fjv", "name": "Father Joe's Villages",
     "address": "3350 E Street, San Diego, CA 92102",
     "wage_per_hour": 24.00, "staff_per_run": 1, "cost_per_km": 0.43,
     "has_refrigerated_vehicle": True, "hours": (6, 21)},
    {"org_id": "sdfb", "name": "San Diego Food Bank",
     "address": "9850 Distribution Avenue, San Diego, CA 92121",
     "wage_per_hour": 23.00, "staff_per_run": 2, "cost_per_km": 0.48,
     "has_refrigerated_vehicle": True, "hours": (7, 16)},
    {"org_id": "fsd", "name": "Feeding San Diego",
     "address": "9455 Waples Street, San Diego, CA 92121",
     "wage_per_hour": 23.00, "staff_per_run": 1, "cost_per_km": 0.46,
     "has_refrigerated_vehicle": True, "hours": (7, 17)},
    {"org_id": "sdrm", "name": "San Diego Rescue Mission",
     "address": "120 Elm Street, San Diego, CA 92101",
     "wage_per_hour": 21.00, "staff_per_run": 1, "cost_per_km": 0.41,
     "has_refrigerated_vehicle": True, "hours": (6, 20)},
    {"org_id": "alpha", "name": "Alpha Project",
     "address": "3737 Fifth Avenue, San Diego, CA 92103",
     "wage_per_hour": 22.00, "staff_per_run": 1, "cost_per_km": 0.43,
     "has_refrigerated_vehicle": False, "hours": (7, 19)},
    {"org_id": "salvation", "name": "Salvation Army Centre City Corps",
     "address": "825 7th Avenue, San Diego, CA 92101",
     "wage_per_hour": 21.00, "staff_per_run": 1, "cost_per_km": 0.43,
     "has_refrigerated_vehicle": False, "hours": (8, 17)},
    {"org_id": "path", "name": "PATH San Diego",
     "address": "1250 6th Avenue, San Diego, CA 92101",
     "wage_per_hour": 22.00, "staff_per_run": 1, "cost_per_km": 0.43,
     "has_refrigerated_vehicle": False, "hours": (8, 18)},
    {"org_id": "jfs", "name": "Jewish Family Service of San Diego",
     "address": "8804 Balboa Avenue, San Diego, CA 92123",
     "wage_per_hour": 24.00, "staff_per_run": 1, "cost_per_km": 0.45,
     "has_refrigerated_vehicle": True, "hours": (8, 17)},
]

# hand-added downtown destinations. NONE of the 72 Food Bank sites are in
# 92101, so these are the ones that actually serve the count grid.
DOWNTOWN = [
    {"dest_id": "neil_good", "name": "Neil Good Day Center",
     "address": "299 17th Street, San Diego, CA 92101", "dest_type": "day_center",
     "accepts": ["prepared_hot", "prepared_cold", "produce", "packaged_dry", "bakery"],
     "storage": {"refrigerated": True, "frozen": False, "hot_holding": True},
     "capacity_lbs_per_visit": 300.0, "hours": [(0, "07:00", "15:00"), (1, "07:00", "15:00"),
        (2, "07:00", "15:00"), (3, "07:00", "15:00"), (4, "07:00", "15:00")]},
    {"dest_id": "fjv_village", "name": "Father Joe's Villages Dining Room",
     "address": "1501 Imperial Avenue, San Diego, CA 92101", "dest_type": "meal_service",
     "accepts": ["prepared_hot", "prepared_cold", "produce", "packaged_dry", "bakery", "dairy"],
     "storage": {"refrigerated": True, "frozen": True, "hot_holding": True},
     "capacity_lbs_per_visit": 1200.0, "hours": [(d, "06:00", "19:00") for d in range(7)]},
    {"dest_id": "gods_hand", "name": "God's Extended Hand",
     "address": "1625 Island Avenue, San Diego, CA 92101", "dest_type": "meal_service",
     "accepts": ["prepared_hot", "prepared_cold", "packaged_dry", "bakery"],
     "storage": {"refrigerated": True, "frozen": False, "hot_holding": True},
     "capacity_lbs_per_visit": 250.0, "hours": [(d, "16:00", "19:00") for d in (0, 1, 2, 3, 4)]},
    {"dest_id": "ladle", "name": "Ladle Fellowship (First Lutheran)",
     "address": "1420 3rd Avenue, San Diego, CA 92101", "dest_type": "meal_service",
     "accepts": ["prepared_hot", "prepared_cold", "produce", "bakery"],
     "storage": {"refrigerated": True, "frozen": False, "hot_holding": True},
     "capacity_lbs_per_visit": 200.0, "hours": [(2, "08:00", "12:00"), (6, "08:00", "12:00")]},
    {"dest_id": "sdrm_kitchen", "name": "San Diego Rescue Mission Kitchen",
     "address": "120 Elm Street, San Diego, CA 92101", "dest_type": "meal_service",
     "accepts": ["prepared_hot", "prepared_cold", "produce", "packaged_dry", "bakery", "dairy"],
     "storage": {"refrigerated": True, "frozen": True, "hot_holding": True},
     "capacity_lbs_per_visit": 900.0, "hours": [(d, "06:00", "19:00") for d in range(7)]},
    {"dest_id": "rachels", "name": "Rachel's Women's Center",
     "address": "759 8th Avenue, San Diego, CA 92101", "dest_type": "day_center",
     "accepts": ["prepared_cold", "produce", "packaged_dry", "bakery"],
     "storage": {"refrigerated": True, "frozen": False, "hot_holding": False},
     "capacity_lbs_per_visit": 150.0, "hours": [(d, "07:00", "15:00") for d in range(5)]},
    {"dest_id": "salvation_cc", "name": "Salvation Army Centre City Corps",
     "address": "825 7th Avenue, San Diego, CA 92101", "dest_type": "meal_service",
     "accepts": ["prepared_hot", "prepared_cold", "produce", "packaged_dry", "bakery"],
     "storage": {"refrigerated": True, "frozen": False, "hot_holding": True},
     "capacity_lbs_per_visit": 400.0, "hours": [(d, "08:00", "17:00") for d in range(5)]},
    {"dest_id": "path_connections", "name": "PATH Connections Housing",
     "address": "1250 6th Avenue, San Diego, CA 92101", "dest_type": "day_center",
     "accepts": ["prepared_cold", "produce", "packaged_dry", "bakery", "dairy"],
     "storage": {"refrigerated": True, "frozen": False, "hot_holding": False},
     "capacity_lbs_per_visit": 350.0, "hours": [(d, "08:00", "18:00") for d in range(7)]},
    {"dest_id": "alpha_bridge", "name": "Alpha Project Bridge Shelter",
     "address": "1401 Imperial Avenue, San Diego, CA 92101", "dest_type": "meal_service",
     "accepts": ["prepared_hot", "prepared_cold", "produce", "packaged_dry", "bakery"],
     "storage": {"refrigerated": True, "frozen": False, "hot_holding": True},
     "capacity_lbs_per_visit": 500.0, "hours": [(d, "06:00", "20:00") for d in range(7)]},
]

HOURS_CAVEAT = ("Operating hours entered from public listings and NOT independently "
                "confirmed. Verify with the site before relying on this.")


def build_nonprofits(cache, refresh=False) -> list[dict]:
    out, missing = [], []
    for n in NONPROFITS:
        coords = geocode(n["address"], cache, refresh)
        if not coords:
            missing.append(n["name"])
            continue
        lat, lon = coords
        start, end = n["hours"]
        out.append({
            "org_id": n["org_id"], "name": n["name"], "address": n["address"],
            "hq_lat": lat, "hq_lon": lon,
            "wage_per_hour": n["wage_per_hour"], "staff_per_run": n["staff_per_run"],
            "cost_per_km": n["cost_per_km"],
            "has_refrigerated_vehicle": n["has_refrigerated_vehicle"],
            "operating_windows": [{"dow": d, "start": f"{start:02d}:00", "end": f"{end:02d}:00"}
                                  for d in range(7)],
            "geocode_source": "US Census Geocoder, Public_AR_Current",
            "verified": False, "verification_note": HOURS_CAVEAT,
        })
    if missing:
        print(f"    ! {len(missing)} nonprofit(s) could not be geocoded: {missing}")
    return out


def build_foodbank_destinations(cache, idx, refresh=False) -> list[dict]:
    df = pd.read_csv(SOURCE_DIR / "sd_foodbank_sites.csv").fillna("")
    out, unresolved = [], []

    for i, r in df.iterrows():
        addr = f"{r['street']}, {r['city']}, {r['state']} {r['zip']}"
        coords = geocode(addr, cache, refresh)
        if not coords:
            unresolved.append(r["name"])
            continue
        lat, lon = coords
        windows, note = parse_hours(r["days"], r["hours"])
        prof = profile_for(r["distribution_type"], r["food_provided"])
        elig = str(r["eligibility"]).strip()
        open_intake = elig.lower().startswith("all are welcome")

        score = idx.score(lat, lon)
        out.append({
            "dest_id": f"fb_{i:03d}", "name": r["name"], "address": addr,
            "lat": lat, "lon": lon, "dest_type": "food_bank_partner",
            "accepts": prof["accepts"], "storage": prof["storage"],
            "capacity_lbs_per_visit": prof["capacity_lbs_per_visit"],
            "capacity_basis": prof["capacity_basis"],
            "open_windows": windows, "hours_note": note,
            "hours_raw": r["hours"], "distribution_type": r["distribution_type"],
            "eligibility_open": open_intake,
            "eligibility_label": (elig[:70] or "unspecified"),
            "served_block_ids": score["served_block_ids"], "n_blocks": score["n_blocks"],
            "need_now": score["need_now"], "need_trend": score["need_trend"],
            "area": score["area"],
            "source": r["source"], "verified": False, "verification_note": HOURS_CAVEAT,
        })
    if unresolved:
        print(f"    ! {len(unresolved)} food bank site(s) could not be geocoded: {unresolved[:5]}")
    return out


def build_downtown_destinations(cache, idx, refresh=False) -> list[dict]:
    out, missing = [], []
    for d in DOWNTOWN:
        coords = geocode(d["address"], cache, refresh)
        if not coords:
            missing.append(d["name"])
            continue
        lat, lon = coords
        score = idx.score(lat, lon)
        out.append({
            "dest_id": d["dest_id"], "name": d["name"], "address": d["address"],
            "lat": lat, "lon": lon, "dest_type": d["dest_type"],
            "accepts": d["accepts"], "storage": d["storage"],
            "capacity_lbs_per_visit": d["capacity_lbs_per_visit"],
            "capacity_basis": "hand-entered operational estimate",
            "open_windows": [{"dow": dow, "start": s, "end": e} for dow, s, e in d["hours"]],
            "hours_note": "hand-entered from public listing",
            "eligibility_open": True,
            "eligibility_label": "open intake — serves unsheltered clients",
            "served_block_ids": score["served_block_ids"], "n_blocks": score["n_blocks"],
            "need_now": score["need_now"], "need_trend": score["need_trend"],
            "area": score["area"],
            "source": "hand-curated", "verified": False, "verification_note": HOURS_CAVEAT,
        })
    if missing:
        print(f"    ! {len(missing)} downtown destination(s) could not be geocoded: {missing}")
    return out


def build_hubspots(idx, top_n: int = 6) -> list[dict]:
    """Outreach delivery points at the highest-need blocks.

    Aggregated to BLOCK level, never to a precise encampment location, and
    nonprofit-facing only. That is a deliberate design constraint: the output
    must not be operationalisable for enforcement.
    """
    ranked = sorted(
        (b for b in idx.blocks.values() if b.block_id in idx.panel_block_ids),
        key=lambda b: (b.need_now, b.need_trend), reverse=True)[:top_n]

    out = []
    for b in ranked:
        delta = idx.forecast.get(b.area, {}).get("forecast_delta", 0.0)
        out.append({
            "dest_id": f"hub_{b.block_id}", "name": f"Outreach hubspot — {b.label}",
            "address": f"{b.label}, {b.area}", "lat": b.lat, "lon": b.lon,
            "dest_type": "hubspot",
            "accepts": ["prepared_hot", "prepared_cold", "produce", "packaged_dry", "bakery"],
            "storage": {"refrigerated": True, "frozen": False, "hot_holding": True},
            "capacity_lbs_per_visit": 120.0,
            "capacity_basis": "what a two-person outreach team can hand out in one visit",
            "open_windows": [{"dow": d, "start": "07:00", "end": "19:00"} for d in range(7)],
            "hours_note": "outreach team scheduled window",
            "eligibility_open": True, "eligibility_label": "open — street outreach",
            "served_block_ids": [b.block_id], "n_blocks": 1,
            "need_now": b.need_now, "need_trend": b.need_trend + delta,
            "area": b.area, "block_id": b.block_id,
            "nonprofit_facing_only": True,
            "source": "top blocks by need_now, Panel261 latest count date",
            "verified": True,
            "verification_note": "Block-level aggregation only. Never a precise location.",
        })
    return out


def main(refresh: bool = False) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache = _load_cache()
    print("building need index …")
    idx = needs.get_index()

    print("geocoding nonprofits …")
    orgs = build_nonprofits(cache, refresh)
    _save_cache(cache)

    print("geocoding downtown destinations …")
    downtown = build_downtown_destinations(cache, idx, refresh)
    _save_cache(cache)

    print(f"geocoding {len(pd.read_csv(SOURCE_DIR / 'sd_foodbank_sites.csv'))} food bank sites …")
    foodbank = build_foodbank_destinations(cache, idx, refresh)
    _save_cache(cache)

    print("ranking hubspots …")
    hubs = build_hubspots(idx)

    dests = downtown + hubs + foodbank
    (DATA_DIR / "nonprofits.json").write_text(json.dumps(orgs, indent=2))
    (DATA_DIR / "destinations.json").write_text(json.dumps(dests, indent=2))

    print(f"\nnonprofits          {len(orgs)}")
    print(f"destinations        {len(dests)}  "
          f"(downtown {len(downtown)}, hubspots {len(hubs)}, food bank {len(foodbank)})")
    print(f"pairs per donation  {len(orgs) * len(dests)}")
    no_hours = sum(1 for d in dests if not d["open_windows"])
    print(f"destinations w/o parseable hours: {no_hours}")
    print(f"open-intake destinations: {sum(1 for d in dests if d['eligibility_open'])}/{len(dests)}")
    print(f"destinations inside the count grid (need_now > 0): "
          f"{sum(1 for d in dests if d['need_now'] > 0)}")


if __name__ == "__main__":
    main(refresh="--refresh" in sys.argv)
