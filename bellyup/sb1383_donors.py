"""Downtown surplus generators, pulled from OpenStreetMap.

SB 1383 legally requires large food businesses to donate surplus edible food.
OSM has no sales figures, floor areas or seat counts, so the tier assigned here
is an *approximation* from tags -- every donor carries `tier_basis` recording
exactly what the guess was made from. That limitation is stated on stage, not
buried.

  Tier One (since Jan 2022): supermarkets, grocery stores over 10,000 sq ft,
    food distributors, wholesale food vendors.
  Tier Two (since Jan 2024): hotels with 200+ rooms and on-site food,
    restaurants with 250+ seats or 5,000 sq ft, venues serving 2,000+/day,
    health facilities, large event venues.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent / "data"
CACHE = DATA_DIR / "donors.json"

# narrowed to the block grid footprint
BBOX = (32.695, -117.171, 32.724, -117.134)  # S, W, N, E

OVERPASS = "https://overpass-api.de/api/interpreter"

QUERY = """
[out:json][timeout:90];
(
  nwr["amenity"~"^(restaurant|fast_food|cafe|food_court)$"]({s},{w},{n},{e});
  nwr["shop"~"^(supermarket|grocery|convenience|deli|bakery|greengrocer)$"]({s},{w},{n},{e});
  nwr["tourism"="hotel"]({s},{w},{n},{e});
  nwr["amenity"="conference_centre"]({s},{w},{n},{e});
);
out center tags;
"""


def classify(tags: dict) -> tuple[int | None, str]:
    """Best-effort SB 1383 tier from OSM tags. Returns (tier, basis)."""
    shop = tags.get("shop", "")
    amenity = tags.get("amenity", "")
    tourism = tags.get("tourism", "")

    if shop in ("supermarket", "greengrocer") or tags.get("shop") == "wholesale":
        return 1, f"shop={shop} — supermarket/grocery, Tier One since Jan 2022"

    if tourism == "hotel":
        rooms = tags.get("rooms")
        try:
            n_rooms = int(rooms) if rooms else None
        except ValueError:
            n_rooms = None
        if n_rooms and n_rooms >= 200:
            return 2, f"tourism=hotel, rooms={n_rooms} (>=200) with on-site food"
        if n_rooms:
            return None, f"tourism=hotel, rooms={n_rooms} — under the 200-room threshold"
        return None, "tourism=hotel, room count not in OSM — tier unresolved"

    if amenity == "conference_centre":
        return 2, "amenity=conference_centre — large venue, seats not in OSM"

    if amenity in ("restaurant", "food_court"):
        return 2, ("amenity=%s — Tier Two only if 250+ seats or 5,000 sq ft; "
                   "neither is in OSM, so this is an approximation" % amenity)

    if amenity in ("fast_food", "cafe") or shop in ("grocery", "convenience", "deli", "bakery"):
        return None, f"{amenity or shop} — below the Tier Two thresholds in almost all cases"

    return None, "no tier signal in tags"


def fetch(force: bool = False) -> list[dict]:
    """Query Overpass, cache to data/donors.json. Cached read needs no network."""
    if CACHE.exists() and not force:
        return json.loads(CACHE.read_text())["donors"]

    s, w, n, e = BBOX
    q = QUERY.format(s=s, w=w, n=n, e=e)
    resp = requests.post(OVERPASS, data={"data": q}, timeout=120,
                         headers={"User-Agent": "BellyUp/hackathon (San Diego food recovery)"})
    resp.raise_for_status()
    elements = resp.json().get("elements", [])

    donors, seen = [], set()
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue  # unnamed POIs are not actionable donors
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        key = (name, round(lat, 5), round(lon, 5))
        if key in seen:
            continue
        seen.add(key)

        tier, basis = classify(tags)
        street = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
        donors.append({
            "donor_id": f"osm_{el['type']}_{el['id']}",
            "donor_name": name,
            "lat": round(float(lat), 6),
            "lon": round(float(lon), 6),
            "address": street or "address not in OSM",
            "osm_category": tags.get("amenity") or tags.get("shop") or tags.get("tourism"),
            "cuisine": tags.get("cuisine"),
            "sb1383_tier": tier,
            "tier_basis": basis,
        })

    donors.sort(key=lambda d: (d["sb1383_tier"] or 9, d["donor_name"]))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "bbox": {"south": s, "west": w, "north": n, "east": e},
        "source": "OpenStreetMap via Overpass API, ODbL",
        "tier_caveat": "Tiers are tag approximations. OSM carries no sales figures, "
                       "floor areas or seat counts.",
        "count": len(donors),
        "donors": donors,
    }, indent=2))
    return donors


if __name__ == "__main__":
    import sys
    ds = fetch(force="--force" in sys.argv)
    from collections import Counter
    print(f"{len(ds)} donors in bbox {BBOX}")
    print("by tier:      ", dict(Counter(d["sb1383_tier"] for d in ds)))
    print("by category:  ", dict(Counter(d["osm_category"] for d in ds).most_common(8)))
    print("\nsample Tier Two:")
    for d in [x for x in ds if x["sb1383_tier"] == 2][:6]:
        print(f"  {d['donor_name'][:38]:38} {d['lat']:.4f},{d['lon']:.4f}  {d['osm_category']}")
