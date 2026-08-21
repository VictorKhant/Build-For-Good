#!/usr/bin/env python3
"""Convert optimized allocation arcs into sequential truck trips."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from calc.database import read_table, write_table
from calc.road_routing import road_route


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=["greedy", "optimized", "route_optimized"], default="optimized")
    return parser.parse_args()


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    radius = 3958.76
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    value = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 2 * radius * math.asin(math.sqrt(value))


def nearest_neighbor(start: tuple[float, float], stops: list[dict]) -> list[dict]:
    remaining, ordered, current = stops[:], [], start
    while remaining:
        chosen = min(remaining, key=lambda x: (haversine(current, x["point"]), x["hotspot_id"]))
        remaining.remove(chosen)
        ordered.append(chosen)
        current = chosen["point"]
    return ordered


def route_length(start: tuple[float, float], stops: list[dict]) -> float:
    points = [start] + [stop["point"] for stop in stops]
    return sum(haversine(a, b) for a, b in zip(points, points[1:]))


def two_opt(start: tuple[float, float], stops: list[dict]) -> list[dict]:
    """Improve a fixed delivery sequence without changing allocations."""
    best = stops[:]
    best_distance = route_length(start, best)
    improved = True
    while improved:
        improved = False
        for left in range(len(best) - 1):
            for right in range(left + 2, len(best) + 1):
                candidate = best[:left] + list(reversed(best[left:right])) + best[right:]
                distance = route_length(start, candidate)
                if distance + 1e-9 < best_distance:
                    best, best_distance, improved = candidate, distance, True
    return best


def weighted_percentile(values: list[float], weights: list[float], percentile: float) -> float:
    pairs = sorted(zip(values, weights))
    target = sum(weights) * percentile
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= target:
            return value
    return pairs[-1][0] if pairs else 0.0


def main() -> None:
    args = arguments()
    allocation_table = {
        "greedy": "simulation_baseline_allocations",
        "optimized": "simulation_optimized_allocations",
        "route_optimized": "simulation_route_optimized_allocations",
    }[args.method]
    allocations = read_table(allocation_table)
    agencies = read_table("agency_capacity").set_index("agency_id")
    suppliers = read_table("supplier_supply").drop_duplicates("supplier_id").set_index("supplier_id")
    hotspots = read_table("hotspots").set_index("block_id")
    route_rows, stop_rows = [], []

    for route_number, ((agency_id, supplier_id), group) in enumerate(
        allocations.groupby(["agency_id", "business_id"], sort=True), 1
    ):
        agency, supplier = agencies.loc[agency_id], suppliers.loc[supplier_id]
        deliveries = group.groupby("hotspot_block_id").meals_allocated.sum()
        stops = []
        for hotspot_id, meals in deliveries.items():
            hotspot = hotspots.loc[hotspot_id]
            stops.append({"hotspot_id": hotspot_id, "meals": float(meals), "point": (float(hotspot.lat), float(hotspot.lon)), "name": hotspot.location})
        supplier_point = (float(supplier.lat), float(supplier.lon))
        ordered = nearest_neighbor(supplier_point, stops)
        if args.method == "route_optimized":
            ordered = two_opt(supplier_point, ordered)
        points = [(float(agency.lat), float(agency.lon)), (float(supplier.lat), float(supplier.lon))] + [x["point"] for x in ordered]
        route_id = f"{args.method}_truck_trip_{route_number:03d}"
        try:
            road = road_route(points)
            available, error = True, ""
        except Exception as exc:
            road = {"distance_miles": None, "duration_minutes": None, "leg_miles": [], "leg_minutes": [], "geometry": None}
            available, error = False, str(exc)
        meal_distances, meal_minutes, meal_weights = [], [], []
        cumulative_miles = cumulative_minutes = 0.0
        for stop_index, stop in enumerate(ordered, 1):
            leg_index = stop_index  # leg 0 is Agency -> Supplier; food starts after pickup.
            if available:
                cumulative_miles += road["leg_miles"][leg_index]
                cumulative_minutes += road["leg_minutes"][leg_index]
            meal_distances.append(cumulative_miles)
            meal_minutes.append(cumulative_minutes)
            meal_weights.append(stop["meals"])
        loaded = float(deliveries.sum())
        route_rows.append({
            "route_id": route_id, "agency_id": agency_id, "supplier_id": supplier_id,
            "agency_name": agency.agency_name, "supplier_name": supplier.supplier_name,
            "truck_sequence": route_number, "hotspot_count": len(ordered),
            "meals_loaded": loaded, "distance_miles": road["distance_miles"],
            "duration_minutes": road["duration_minutes"], "route_available": available,
            "meals_per_truck_mile": loaded / road["distance_miles"] if available and road["distance_miles"] else None,
            "average_meal_distance_miles": sum(v*w for v,w in zip(meal_distances,meal_weights)) / loaded if available and loaded else None,
            "average_meal_transit_minutes": sum(v*w for v,w in zip(meal_minutes,meal_weights)) / loaded if available and loaded else None,
            "p95_meal_transit_minutes": weighted_percentile(meal_minutes, meal_weights, .95) if available else None,
            "max_meal_transit_minutes": max(meal_minutes) if available and meal_minutes else None,
            "ordering_method": (
                "nearest_neighbor_plus_2opt_then_osrm_driving_geometry"
                if args.method == "route_optimized"
                else "nearest_neighbor_then_osrm_driving_geometry"
            ),
            "geometry_geojson": json.dumps(road["geometry"]) if road["geometry"] else None,
            "route_error": error,
        })
        stop_rows.extend([
            {"route_id": route_id, "stop_sequence": 0, "stop_type": "agency", "entity_id": agency_id, "stop_name": agency.agency_name, "lat": agency.lat, "lon": agency.lon, "meals_delivered": 0, "leg_miles_from_previous": 0, "leg_minutes_from_previous": 0, "meal_distance_from_pickup_miles": None, "meal_transit_from_pickup_minutes": None},
            {"route_id": route_id, "stop_sequence": 1, "stop_type": "supplier", "entity_id": supplier_id, "stop_name": supplier.supplier_name, "lat": supplier.lat, "lon": supplier.lon, "meals_delivered": 0, "leg_miles_from_previous": road["leg_miles"][0] if available else None, "leg_minutes_from_previous": road["leg_minutes"][0] if available else None, "meal_distance_from_pickup_miles": 0, "meal_transit_from_pickup_minutes": 0},
        ])
        for delivery_index, stop in enumerate(ordered):
            sequence = delivery_index + 2
            stop_rows.append({"route_id": route_id, "stop_sequence": sequence, "stop_type": "hotspot", "entity_id": stop["hotspot_id"], "stop_name": stop["name"], "lat": stop["point"][0], "lon": stop["point"][1], "meals_delivered": stop["meals"], "leg_miles_from_previous": road["leg_miles"][delivery_index + 1] if available else None, "leg_minutes_from_previous": road["leg_minutes"][delivery_index + 1] if available else None, "meal_distance_from_pickup_miles": meal_distances[delivery_index] if available else None, "meal_transit_from_pickup_minutes": meal_minutes[delivery_index] if available else None})

    routes, route_stops = pd.DataFrame(route_rows), pd.DataFrame(stop_rows)
    write_table(f"simulation_{args.method}_vehicle_routes", routes)
    write_table(f"simulation_{args.method}_vehicle_route_stops", route_stops)
    print(f"method={args.method}, truck trips={len(routes)}, sequential stops={len(route_stops)}, road routes={int(routes.route_available.sum())}")


if __name__ == "__main__":
    main()
