#!/usr/bin/env python3
"""Build current optimization demand points from block-level observations."""

from __future__ import annotations

import argparse
import csv
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate block-level demand points.")
    parser.add_argument(
        "--counts", type=Path, default=ROOT / "dataset" / "BlockLevel_Counts.csv"
    )
    parser.add_argument(
        "--grid", type=Path, default=ROOT / "dataset" / "Downtown_BlockGrid.csv"
    )
    parser.add_argument(
        "--methodology",
        type=Path,
        default=ROOT / "dataset" / "Methodology_Periods.csv",
    )
    parser.add_argument("--date", help="Observation date (YYYY-MM-DD); default is latest.")
    parser.add_argument("--output", type=Path, default=ROOT / "demand_points.csv")
    return parser.parse_args()


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def multiplier_for(
    observation_date: date, periods: list[dict[str, str]]
) -> dict[str, str]:
    matches = []
    for period in periods:
        start = parse_date(period["effective_from"])
        end = parse_date(period["effective_to"]) if period["effective_to"] else date.max
        if start <= observation_date <= end:
            matches.append(period)
    if len(matches) != 1:
        raise ValueError(
            f"Expected one methodology period for {observation_date}, found {len(matches)}"
        )
    return matches[0]


def numeric(value: str | None) -> float:
    return float(value) if value and value.strip() else 0.0


def clean_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def main() -> None:
    args = arguments()
    counts = read_rows(args.counts)
    grid = {row["block_id"]: row for row in read_rows(args.grid)}
    periods = read_rows(args.methodology)
    if not counts:
        raise SystemExit(f"No count rows found in {args.counts}")

    available_dates = sorted({row["count_date"] for row in counts})
    selected_date = args.date or available_dates[-1]
    if selected_date not in available_dates:
        raise SystemExit(
            f"Date {selected_date} is unavailable. Choose one of: {', '.join(available_dates)}"
        )

    period = multiplier_for(parse_date(selected_date), periods)
    individual_multiplier = numeric(period["individual_multiplier"])
    tent_multiplier = numeric(period["tent_multiplier"])
    vehicle_multiplier = numeric(period["vehicle_multiplier"])

    selected = {
        row["block_id"]: row for row in counts if row["count_date"] == selected_date
    }
    output_rows = []
    for block_id, location in grid.items():
        observation = selected.get(block_id)
        # A block missing from the selected count is not silently treated as zero.
        if observation is None:
            continue
        individuals = numeric(observation.get("individuals"))
        tents = numeric(observation.get("tents_structures"))
        vehicles = numeric(observation.get("vehicles"))
        demand = (
            individuals * individual_multiplier
            + tents * tent_multiplier
            + vehicles * vehicle_multiplier
        )
        output_rows.append(
            {
                "block_id": block_id,
                "area": location["area"],
                "lat": location["lat"],
                "lon": location["lon"],
                "individuals": clean_number(individuals),
                "tents": clean_number(tents),
                "vehicles": clean_number(vehicles),
                "demand": clean_number(demand),
                "count_date": selected_date,
                "method": period["method"],
                "individual_multiplier": clean_number(individual_multiplier),
                "tent_multiplier": clean_number(tent_multiplier),
                "vehicle_multiplier": clean_number(vehicle_multiplier),
            }
        )

    output_rows.sort(key=lambda row: (-float(row["demand"]), row["block_id"]))
    for rank, row in enumerate(output_rows, 1):
        row["demand_rank"] = rank

    fieldnames = [
        "demand_rank",
        "block_id",
        "area",
        "lat",
        "lon",
        "individuals",
        "tents",
        "vehicles",
        "demand",
        "count_date",
        "method",
        "individual_multiplier",
        "tent_multiplier",
        "vehicle_multiplier",
    ]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    total_demand = sum(float(row["demand"]) for row in output_rows)
    positive_points = sum(float(row["demand"]) > 0 for row in output_rows)
    print(f"Wrote {len(output_rows)} demand points to {args.output}")
    print(f"Date={selected_date}, method={period['method']}")
    print(f"Positive points={positive_points}, total demand={total_demand:.2f}")


if __name__ == "__main__":
    main()
