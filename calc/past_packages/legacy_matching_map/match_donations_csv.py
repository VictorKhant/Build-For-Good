#!/usr/bin/env python3
"""Match demo CSV donations to their best Agency -> Supplier -> Hotspot route."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from road_routing import (
    road_distances_many_to_one_miles,
    road_distances_one_to_many_miles,
    road_route,
)


ROOT = Path(__file__).resolve().parent
LBS_PER_MEAL = 1.2


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?", default=ROOT / "demo_donation_inputs.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "demo_donation_routes.csv")
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def best_match(donation: dict[str, str], agencies: list[dict], hotspots: list[dict]) -> dict:
    supplier_lat = float(donation["lat"])
    supplier_lon = float(donation["lon"])
    quantity_lbs = float(donation["quantity_lbs"])
    if quantity_lbs <= 0:
        raise ValueError(f"{donation['donation_id']}: quantity_lbs must be positive")
    meals = quantity_lbs / LBS_PER_MEAL
    candidates = []

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
            demand = float(hotspot["demand"])
            served_need = min(meals, demand)
            route_miles = pickup_miles + delivery_miles
            score = served_need / (1 + route_miles)
            candidates.append(
                {
                    "donation_id": donation["donation_id"],
                    "supplier_name": donation["supplier_name"],
                    "supplier_type": donation["supplier_type"],
                    "food_type": donation["food_type"],
                    "quantity_lbs": round(quantity_lbs, 2),
                    "estimated_meals": round(meals, 2),
                    "agency_id": agency["agency_id"],
                    "agency_name": agency["name"],
                    "agency_lat": agency["lat"],
                    "agency_lon": agency["lon"],
                    "supplier_lat": donation["lat"],
                    "supplier_lon": donation["lon"],
                    "hotspot_block_id": hotspot["block_id"],
                    "hotspot_area": hotspot["area"],
                    "hotspot_lat": hotspot["lat"],
                    "hotspot_lon": hotspot["lon"],
                    "hotspot_demand": round(demand, 2),
                    "served_need": round(served_need, 2),
                    "route_sequence": "agency -> supplier -> hotspot",
                    "agency_to_supplier_miles": round(pickup_miles, 3),
                    "supplier_to_hotspot_miles": round(delivery_miles, 3),
                    "route_total_miles": round(route_miles, 3),
                    "distance_method": "osrm_openstreetmap_driving",
                    "score": round(score, 6),
                    "route_duration_minutes": "",
                    "route_geojson": "",
                    "input_data_status": donation.get("data_status", ""),
                    "agency_data_status": agency["data_status"],
                }
            )
    best = max(candidates, key=lambda row: (row["score"], -row["route_total_miles"]))
    route = road_route(
        [
            (float(best["agency_lat"]), float(best["agency_lon"])),
            (float(best["supplier_lat"]), float(best["supplier_lon"])),
            (float(best["hotspot_lat"]), float(best["hotspot_lon"])),
        ]
    )
    best["agency_to_supplier_miles"] = round(route["leg_miles"][0], 3)
    best["supplier_to_hotspot_miles"] = round(route["leg_miles"][1], 3)
    best["route_total_miles"] = round(route["distance_miles"], 3)
    best["route_duration_minutes"] = round(route["duration_minutes"], 1)
    best["score"] = round(best["served_need"] / (1 + route["distance_miles"]), 6)
    best["route_geojson"] = json.dumps(route["geometry"], separators=(",", ":"))
    return best


def main() -> None:
    args = arguments()
    donations = read_csv(args.input)
    agencies = read_csv(ROOT / "agency_points.csv")
    hotspots = [
        row for row in read_csv(ROOT / "demand_points.csv") if float(row["demand"]) > 0
    ]
    matches = [best_match(donation, agencies, hotspots) for donation in donations]

    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(matches[0]))
        writer.writeheader()
        writer.writerows(matches)

    print(f"Wrote {len(matches)} best routes to {args.output}")
    for match in matches:
        print(
            f"{match['donation_id']}: {match['agency_id']} -> "
            f"{match['supplier_name']} -> {match['hotspot_block_id']} "
            f"({match['route_total_miles']:.2f} road miles, OSRM)"
        )


if __name__ == "__main__":
    main()
