#!/usr/bin/env python3
"""Normalize authoritative main data and create explicitly synthetic donations."""

from __future__ import annotations

import re
import random
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT_ROOT / "dataset"
OUT = PROJECT_ROOT / "calc" / "optimization_data"
DEMO_AGENCY_GEOCODES = {
    "Feeding San Diego": (-117.1780, 32.8930),
    "Feeding San Diego (South Bay)": (-117.1040, 32.6996),
    "Catholic Charities Diocese of San Diego": (-117.0995, 32.7846),
}


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return result or "unknown"


def main() -> None:
    OUT.mkdir(exist_ok=True)

    businesses = pd.read_csv(SOURCE / "businesses.csv")
    businesses.insert(0, "business_id", businesses.business_name.map(lambda x: f"biz_{slug(x)}"))
    businesses.to_csv(OUT / "businesses.csv", index=False)

    agencies = pd.read_csv(SOURCE / "agencies.csv")
    agencies.insert(0, "agency_id", agencies.agency_name.map(lambda x: f"agency_{slug(x)}"))
    agencies["is_synthetic_geocode"] = False
    for name, (lon, lat) in DEMO_AGENCY_GEOCODES.items():
        mask = agencies.agency_name.eq(name) & agencies[["lat", "lon"]].isna().any(axis=1)
        agencies.loc[mask, ["lon", "lat"]] = [lon, lat]
        agencies.loc[mask, "geocode_method"] = "approximate_manual_demo_from_oscar_ui"
        agencies.loc[mask, "is_synthetic_geocode"] = True
    agencies["usable_for_routing"] = agencies[["lat", "lon"]].notna().all(axis=1)
    agencies["capacity_meals"] = pd.NA
    agencies.to_csv(OUT / "agencies.csv", index=False)

    hotspots = pd.read_csv(SOURCE / "hotspots.csv")
    hotspots = hotspots.rename(
        columns={
            "need": "historical_demand",
            "latest_persons": "latest_demand",
            "need_rank": "rank",
        }
    )
    hotspots.to_csv(OUT / "hotspots.csv", index=False)

    pantries = pd.read_csv(SOURCE / "mobile_pantries.csv")
    pantries.insert(0, "site_id", pantries.site_name.map(lambda x: f"pantry_{slug(x)}"))
    pantries.to_csv(OUT / "mobile_pantries.csv", index=False)

    # Match Oscar-implementation's seeded UI reports exactly. These are not observed donations.
    rng = random.Random(20260820)
    selected_records = businesses.to_dict("records")
    rng.shuffle(selected_records)
    rows = []
    base = pd.Timestamp("2026-08-20T17:25:00-07:00")
    for index, business in enumerate(selected_records[:14], 1):
        facility_type = business["facility_type"]
        if facility_type == "grocery":
            pounds = rng.randint(120, 420)
            description = rng.choice([
                "day-old bakery, produce, dairy nearing date",
                "produce trims, deli overstock, packaged goods",
                "bakery, prepared deli trays, bagged produce",
            ])
        elif facility_type == "hotel":
            rooms = 250
            for token in str(business["size_metric"]).split():
                if token.isdigit():
                    rooms = int(token)
                    break
            pounds = max(25, int(rooms * rng.uniform(0.10, 0.22)))
            description = rng.choice([
                "banquet buffet trays (hot line, chafing)",
                "conference catering overage, plated entrees",
                "breakfast buffet + event catering leftovers",
            ])
        elif facility_type == "venue":
            pounds = rng.randint(180, 600)
            description = rng.choice([
                "concession overstock + suite catering",
                "event concessions, boxed meals unclaimed",
            ])
        else:
            pounds = rng.randint(60, 180)
            description = "cafeteria service line overage, packaged meals"
        prepared = business["surplus_type"] == "prepared"
        report_offset = (index - 1) * rng.randint(11, 23)
        reported = base + pd.Timedelta(minutes=report_offset)
        ready = reported + pd.Timedelta(minutes=20)
        expires = ready + pd.Timedelta(hours=4 if prepared else 12)
        rows.append(
            {
                "donation_id": f"synthetic_{index:03d}",
                "business_id": business["business_id"],
                "reported_at": reported.isoformat(),
                "food_type": "prepared" if prepared else "packaged/produce",
                "condition": "refrigerated" if prepared else "ambient",
                "quantity_lbs": pounds,
                "ready_at": ready.isoformat(),
                "expires_at": expires.isoformat(),
                "status": "active",
                "is_synthetic": True,
                "description": description,
                "synthetic_source": "Oscar-implementation/scripts/build_demo_data.py seed=20260820",
            }
        )
    pd.DataFrame(rows).to_csv(OUT / "demo_donation_reports.csv", index=False)

    print(f"businesses={len(businesses)}, agencies={len(agencies)}, hotspots={len(hotspots)}, pantries={len(pantries)}")
    print(f"synthetic active donations={len(rows)}")
    print(f"routing-ready agencies={int(agencies.usable_for_routing.sum())}/{len(agencies)}")


if __name__ == "__main__":
    main()
