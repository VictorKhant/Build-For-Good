#!/usr/bin/env python3
"""Build and cache Agency -> Supplier -> Hotspot OSRM route metrics."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from road_routing import road_metrics_many_to_one, road_metrics_one_to_many


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA = PROJECT_ROOT / "calc" / "optimization_data"
CACHE = PROJECT_ROOT / "calc" / "route_cache" / "route_matrix.csv"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Re-request routes already cached")
    return parser.parse_args()


def main() -> None:
    args = arguments()
    businesses = pd.read_csv(DATA / "businesses.csv").set_index("business_id")
    donations = pd.read_csv(DATA / "demo_donation_reports.csv")
    donations = donations[donations.status.eq("active")]
    agencies = pd.read_csv(DATA / "agencies.csv")
    agencies = agencies[agencies[["lat", "lon"]].notna().all(axis=1)]
    hotspots = pd.read_csv(DATA / "hotspots.csv")
    hotspots = hotspots[
        hotspots.historical_demand.ge(1) & hotspots[["lat", "lon"]].notna().all(axis=1)
    ]
    CACHE.parent.mkdir(exist_ok=True)
    existing = pd.read_csv(CACHE) if CACHE.exists() and not args.refresh else pd.DataFrame()
    completed = set(existing.donation_id.unique()) if not existing.empty else set()
    new_rows = []

    agency_points = list(zip(agencies.lat, agencies.lon))
    hotspot_points = list(zip(hotspots.lat, hotspots.lon))
    for _, donation in donations.sort_values("reported_at").iterrows():
        if donation.donation_id in completed:
            print(f"cached {donation.donation_id}")
            continue
        business = businesses.loc[donation.business_id]
        supplier = (float(business.lat), float(business.lon))
        pickups = road_metrics_many_to_one(agency_points, supplier)
        deliveries = road_metrics_one_to_many(supplier, hotspot_points)
        for agency_tuple, pickup in zip(agencies.itertuples(index=False), pickups):
            for hotspot_tuple, delivery in zip(hotspots.itertuples(index=False), deliveries):
                available = pickup["available"] and delivery["available"]
                new_rows.append(
                    {
                        "agency_id": agency_tuple.agency_id,
                        "donation_id": donation.donation_id,
                        "business_id": donation.business_id,
                        "hotspot_block_id": hotspot_tuple.block_id,
                        "agency_to_supplier_miles": pickup["miles"],
                        "supplier_to_hotspot_miles": delivery["miles"],
                        "route_total_miles": (
                            pickup["miles"] + delivery["miles"] if available else None
                        ),
                        "route_duration_minutes": (
                            pickup["minutes"] + delivery["minutes"] if available else None
                        ),
                        "route_available": available,
                        "food_compatible": (
                            donation.food_type != "prepared"
                            or str(agency_tuple.accepts_prepared).lower() == "yes"
                        ),
                        "route_error": pickup["error"] or delivery["error"],
                        "distance_method": "osrm_openstreetmap_driving",
                    }
                )
        print(f"fetched {donation.donation_id}: {len(agencies) * len(hotspots)} combinations")

    result = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True)
    result.to_csv(CACHE, index=False)
    print(f"route rows={len(result)}, available={int(result.route_available.sum())}, cache={CACHE}")


if __name__ == "__main__":
    main()
