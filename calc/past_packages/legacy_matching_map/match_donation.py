#!/usr/bin/env python3
"""Match one food donation to ranked (agency, homeless hotspot) pairs."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

from road_routing import (
    road_distances_many_to_one_miles,
    road_distances_one_to_many_miles,
    road_route,
)


ROOT = Path(__file__).resolve().parent
LBS_PER_MEAL = 1.2


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Return the best agency and homeless hotspot for a donation."
    )
    parser.add_argument("input", type=Path, help="Donation JSON input file")
    parser.add_argument("--top", type=int, default=10, help="Number of matches to return")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "ranked_donation_matches.csv"
    )
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def validate_donation(donation: dict) -> None:
    required = {
        "supplier_name", "supplier_type", "lat", "lon", "food_type",
        "quantity_lbs", "ready_at", "expires_at",
    }
    missing = sorted(required - donation.keys())
    if missing:
        raise ValueError(f"Missing donation fields: {', '.join(missing)}")
    if float(donation["quantity_lbs"]) <= 0:
        raise ValueError("quantity_lbs must be greater than zero")
    ready = datetime.fromisoformat(donation["ready_at"])
    expires = datetime.fromisoformat(donation["expires_at"])
    if expires <= ready:
        raise ValueError("expires_at must be later than ready_at")


def main() -> None:
    args = arguments()
    if args.top < 1:
        raise SystemExit("--top must be at least 1")
    with args.input.open(encoding="utf-8") as handle:
        donation = json.load(handle)
    validate_donation(donation)

    agencies = read_csv(ROOT / "agency_points.csv")
    hotspots = [
        row for row in read_csv(ROOT / "demand_points.csv") if float(row["demand"]) > 0
    ]
    meals = float(donation["quantity_lbs"]) / LBS_PER_MEAL
    supplier_lat, supplier_lon = float(donation["lat"]), float(donation["lon"])

    matches = []
    agency_points = [(float(row["lat"]), float(row["lon"])) for row in agencies]
    hotspot_points = [(float(row["lat"]), float(row["lon"])) for row in hotspots]
    supplier_point = (supplier_lat, supplier_lon)
    pickup_distances = road_distances_many_to_one_miles(agency_points, supplier_point)
    delivery_distances = road_distances_one_to_many_miles(supplier_point, hotspot_points)

    for agency_index, agency in enumerate(agencies):
        agency_lat, agency_lon = float(agency["lat"]), float(agency["lon"])
        pickup_miles = pickup_distances[agency_index]
        for hotspot_index, hotspot in enumerate(hotspots):
            hotspot_lat, hotspot_lon = float(hotspot["lat"]), float(hotspot["lon"])
            delivery_miles = delivery_distances[hotspot_index]
            route_miles = pickup_miles + delivery_miles
            demand = float(hotspot["demand"])
            served_need = min(meals, demand)
            # Higher served need is better; longer travel is worse. The +1 avoids
            # division by zero. This score is transparent, not a routing API cost.
            score = served_need / (1 + route_miles)
            matches.append(
                {
                    "rank": 0,
                    "agency_id": agency["agency_id"],
                    "agency_name": agency["name"],
                    "agency_data_status": agency["data_status"],
                    "hotspot_block_id": hotspot["block_id"],
                    "hotspot_area": hotspot["area"],
                    "hotspot_demand": round(demand, 2),
                    "supplier_name": donation["supplier_name"],
                    "supplier_type": donation["supplier_type"],
                    "food_type": donation["food_type"],
                    "quantity_lbs": round(float(donation["quantity_lbs"]), 2),
                    "estimated_meals": round(meals, 2),
                    "served_need": round(served_need, 2),
                    "route_mode": "agency_pickup_and_delivery",
                    "passes_agency": True,
                    "route_sequence": "agency -> supplier -> hotspot",
                    "agency_to_supplier_miles": round(pickup_miles, 3),
                    "supplier_to_hotspot_miles": round(delivery_miles, 3),
                    "route_total_miles": round(route_miles, 3),
                    "distance_method": "osrm_openstreetmap_driving",
                    "route_duration_minutes": "",
                    "route_geojson": "",
                    "score": round(score, 6),
                    "explanation": (
                        f"Can direct up to {served_need:.2f} meal-equivalents toward "
                        f"demand {demand:.2f} over a {route_miles:.2f} mile road route."
                    ),
                }
            )

    matches.sort(
        key=lambda row: (-row["score"], row["route_total_miles"], row["agency_id"], row["hotspot_block_id"])
    )
    selected = matches[: args.top]
    for rank, match in enumerate(selected, 1):
        match["rank"] = rank
        agency = next(row for row in agencies if row["agency_id"] == match["agency_id"])
        hotspot = next(
            row for row in hotspots if row["block_id"] == match["hotspot_block_id"]
        )
        route = road_route(
            [
                (float(agency["lat"]), float(agency["lon"])),
                supplier_point,
                (float(hotspot["lat"]), float(hotspot["lon"])),
            ]
        )
        match["agency_to_supplier_miles"] = round(route["leg_miles"][0], 3)
        match["supplier_to_hotspot_miles"] = round(route["leg_miles"][1], 3)
        match["route_total_miles"] = round(route["distance_miles"], 3)
        match["route_duration_minutes"] = round(route["duration_minutes"], 1)
        match["score"] = round(match["served_need"] / (1 + route["distance_miles"]), 6)
        match["route_geojson"] = json.dumps(route["geometry"], separators=(",", ":"))

    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(selected[0]))
        writer.writeheader()
        writer.writerows(selected)

    print(json.dumps({"best_match": selected[0], "alternatives": selected[1:]}, indent=2))
    print(f"Wrote top {len(selected)} matches to {args.output}")


if __name__ == "__main__":
    main()
