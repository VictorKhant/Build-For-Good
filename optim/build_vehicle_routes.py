#!/usr/bin/env python3
"""Convert optimized allocation arcs into sequential truck trips."""

from __future__ import annotations

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


def main() -> None:
    allocations = read_table("simulation_optimized_allocations")
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
        ordered = nearest_neighbor((float(supplier.lat), float(supplier.lon)), stops)
        points = [(float(agency.lat), float(agency.lon)), (float(supplier.lat), float(supplier.lon))] + [x["point"] for x in ordered]
        route_id = f"truck_trip_{route_number:03d}"
        try:
            road = road_route(points)
            available, error = True, ""
        except Exception as exc:
            road = {"distance_miles": None, "duration_minutes": None, "leg_miles": [], "geometry": None}
            available, error = False, str(exc)
        route_rows.append({
            "route_id": route_id, "agency_id": agency_id, "supplier_id": supplier_id,
            "agency_name": agency.agency_name, "supplier_name": supplier.supplier_name,
            "truck_sequence": route_number, "hotspot_count": len(ordered),
            "meals_loaded": float(deliveries.sum()), "distance_miles": road["distance_miles"],
            "duration_minutes": road["duration_minutes"], "route_available": available,
            "ordering_method": "nearest_neighbor_then_osrm_driving_geometry",
            "geometry_geojson": json.dumps(road["geometry"]) if road["geometry"] else None,
            "route_error": error,
        })
        stop_rows.extend([
            {"route_id": route_id, "stop_sequence": 0, "stop_type": "agency", "entity_id": agency_id, "stop_name": agency.agency_name, "lat": agency.lat, "lon": agency.lon, "meals_delivered": 0},
            {"route_id": route_id, "stop_sequence": 1, "stop_type": "supplier", "entity_id": supplier_id, "stop_name": supplier.supplier_name, "lat": supplier.lat, "lon": supplier.lon, "meals_delivered": 0},
        ])
        for sequence, stop in enumerate(ordered, 2):
            stop_rows.append({"route_id": route_id, "stop_sequence": sequence, "stop_type": "hotspot", "entity_id": stop["hotspot_id"], "stop_name": stop["name"], "lat": stop["point"][0], "lon": stop["point"][1], "meals_delivered": stop["meals"]})

    routes, route_stops = pd.DataFrame(route_rows), pd.DataFrame(stop_rows)
    write_table("simulation_vehicle_routes", routes)
    write_table("simulation_vehicle_route_stops", route_stops)
    print(f"truck trips={len(routes)}, sequential stops={len(route_stops)}, road routes={int(routes.route_available.sum())}")


if __name__ == "__main__":
    main()
