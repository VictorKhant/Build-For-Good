#!/usr/bin/env python3
"""Create Manhattan distances among demand, supplier, and distribution sets.

Supplier and distribution source files currently contain only block-level
presence counts. Each point is therefore placed at its block centroid. Two
distribution sites in the same block remain separate points with equal
coordinates.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEMAND_PATH = ROOT / "demand_points.csv"
MASTER_PATH = ROOT / "block_master.csv"
OUTPUT_PATH = ROOT / "demand_supplier_distribution_distances.csv"
EARTH_RADIUS_MILES = 3_958.7613


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def point_xy(lat: float, lon: float, reference_lat_radians: float) -> tuple[float, float]:
    """Project lon/lat to local equirectangular x/y coordinates in miles."""
    return (
        EARTH_RADIUS_MILES * math.radians(lon) * math.cos(reference_lat_radians),
        EARTH_RADIUS_MILES * math.radians(lat),
    )


def distance_miles(first: dict, second: dict) -> float:
    return abs(first["x"] - second["x"]) + abs(first["y"] - second["y"])


def expand_block_points(rows: list[dict[str, str]], count_column: str, prefix: str) -> list[dict]:
    points = []
    for row in rows:
        count = int(float(row.get(count_column) or 0))
        for index in range(1, count + 1):
            points.append(
                {
                    "id": f"{prefix}_{row['block_id']}_{index}",
                    "block_id": row["block_id"],
                    "area": row["area"],
                    "lat": float(row["lat"]),
                    "lon": float(row["lon"]),
                }
            )
    return points


def main() -> None:
    demand_rows = [row for row in read_csv(DEMAND_PATH) if float(row["demand"]) > 0]
    master_rows = read_csv(MASTER_PATH)
    suppliers = expand_block_points(master_rows, "donor_in_block", "supplier")
    distributions = expand_block_points(master_rows, "foodsite_in_block", "distribution")

    if not demand_rows or not suppliers or not distributions:
        raise SystemExit("All three point sets must contain at least one point")

    all_latitudes = [float(row["lat"]) for row in demand_rows]
    all_latitudes += [point["lat"] for point in suppliers + distributions]
    reference_lat = math.radians(sum(all_latitudes) / len(all_latitudes))

    demands = []
    for row in demand_rows:
        lat, lon = float(row["lat"]), float(row["lon"])
        x, y = point_xy(lat, lon, reference_lat)
        demands.append({**row, "lat_float": lat, "lon_float": lon, "x": x, "y": y})
    for point in suppliers + distributions:
        point["x"], point["y"] = point_xy(point["lat"], point["lon"], reference_lat)

    fields = [
        "demand_id", "demand_block_id", "demand_area", "demand_lat", "demand_lon",
        "demand_value", "supplier_id", "supplier_block_id", "supplier_area",
        "supplier_lat", "supplier_lon", "distribution_id", "distribution_block_id",
        "distribution_area", "distribution_lat", "distribution_lon",
        "demand_to_supplier_miles", "supplier_to_distribution_miles",
        "demand_to_distribution_miles", "triple_total_miles", "distance_method",
        "point_location_assumption",
    ]
    row_count = 0
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for demand in demands:
            for supplier in suppliers:
                demand_supplier = distance_miles(demand, supplier)
                for distribution in distributions:
                    supplier_distribution = distance_miles(supplier, distribution)
                    demand_distribution = distance_miles(demand, distribution)
                    writer.writerow(
                        {
                            "demand_id": f"demand_{demand['block_id']}",
                            "demand_block_id": demand["block_id"],
                            "demand_area": demand["area"],
                            "demand_lat": demand["lat"],
                            "demand_lon": demand["lon"],
                            "demand_value": demand["demand"],
                            "supplier_id": supplier["id"],
                            "supplier_block_id": supplier["block_id"],
                            "supplier_area": supplier["area"],
                            "supplier_lat": supplier["lat"],
                            "supplier_lon": supplier["lon"],
                            "distribution_id": distribution["id"],
                            "distribution_block_id": distribution["block_id"],
                            "distribution_area": distribution["area"],
                            "distribution_lat": distribution["lat"],
                            "distribution_lon": distribution["lon"],
                            "demand_to_supplier_miles": round(demand_supplier, 3),
                            "supplier_to_distribution_miles": round(supplier_distribution, 3),
                            "demand_to_distribution_miles": round(demand_distribution, 3),
                            "triple_total_miles": round(demand_supplier + supplier_distribution, 3),
                            "distance_method": "local_equirectangular_manhattan_miles",
                            "point_location_assumption": "block_centroid",
                        }
                    )
                    row_count += 1

    print(f"Demand points: {len(demands)}")
    print(f"Supplier points: {len(suppliers)}")
    print(f"Distribution points: {len(distributions)}")
    print(f"Wrote {row_count} triples to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
