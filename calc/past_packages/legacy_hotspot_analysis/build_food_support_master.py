#!/usr/bin/env python3
"""Build one normalized master file for demand, suppliers, and distributors."""

from __future__ import annotations

import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEMAND_PATH = ROOT / "demand_points.csv"
BLOCK_MASTER_PATH = ROOT / "block_master.csv"
OUTPUT_PATH = ROOT / "food_support_points_master.csv"

FIELDS = [
    "point_id",
    "point_type",
    "name",
    "block_id",
    "area",
    "lat",
    "lon",
    "demand_value",
    "demand_date",
    "food_type",
    "quantity_lbs",
    "estimated_meals",
    "available_from",
    "available_until",
    "frequency",
    "data_status",
    "source_file",
    "notes",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def blank_record() -> dict[str, str]:
    return {field: "" for field in FIELDS}


def main() -> None:
    demand_rows = read_csv(DEMAND_PATH)
    blocks = read_csv(BLOCK_MASTER_PATH)
    output = []

    for demand in demand_rows:
        record = blank_record()
        record.update(
            {
                "point_id": f"demand_{demand['block_id']}",
                "point_type": "demand",
                "name": demand["block_id"],
                "block_id": demand["block_id"],
                "area": demand["area"],
                "lat": demand["lat"],
                "lon": demand["lon"],
                "demand_value": demand["demand"],
                "demand_date": demand["count_date"],
                "data_status": "observed_and_adjusted",
                "source_file": "demand_points.csv",
                "notes": "Demand uses period-specific individual/tent/vehicle multipliers.",
            }
        )
        output.append(record)

    for block in blocks:
        supplier_count = int(float(block.get("donor_in_block") or 0))
        for index in range(1, supplier_count + 1):
            record = blank_record()
            record.update(
                {
                    "point_id": f"supplier_{block['block_id']}_{index}",
                    "point_type": "supplier",
                    "name": f"Unknown supplier in {block['block_id']}",
                    "block_id": block["block_id"],
                    "area": block["area"],
                    "lat": block["lat"],
                    "lon": block["lon"],
                    "data_status": "block_centroid_proxy",
                    "source_file": "block_master.csv",
                    "notes": "Name, exact location, food type, quantity, and availability are not provided.",
                }
            )
            output.append(record)

        distribution_count = int(float(block.get("foodsite_in_block") or 0))
        for index in range(1, distribution_count + 1):
            record = blank_record()
            record.update(
                {
                    "point_id": f"distribution_{block['block_id']}_{index}",
                    "point_type": "distribution",
                    "name": f"Unknown food distribution site in {block['block_id']}",
                    "block_id": block["block_id"],
                    "area": block["area"],
                    "lat": block["lat"],
                    "lon": block["lon"],
                    "data_status": "block_centroid_proxy",
                    "source_file": "block_master.csv",
                    "notes": "Site identity and exact coordinates are not present in the joined block data.",
                }
            )
            output.append(record)

    type_order = {"demand": 0, "supplier": 1, "distribution": 2}
    output.sort(key=lambda row: (type_order[row["point_type"]], row["point_id"]))

    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(output)

    counts = {
        point_type: sum(row["point_type"] == point_type for row in output)
        for point_type in type_order
    }
    print(f"Wrote {len(output)} points to {OUTPUT_PATH}")
    print(", ".join(f"{key}={value}" for key, value in counts.items()))
    print("Blank supplier food fields are intentional: no inventory data was provided.")


if __name__ == "__main__":
    main()
