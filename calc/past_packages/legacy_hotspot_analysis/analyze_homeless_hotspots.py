#!/usr/bin/env python3
"""Rank downtown San Diego homeless-concentration hotspots.

The analysis uses the fixed 261-block panel so every block is compared over
the same 12 observation dates. A "person-equivalent" is calculated as:

    individuals + tents_structures + vehicles

This follows the raw-observed basis in Claude.md and deliberately avoids
mixing the adjusted ``total`` field with its components.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from statistics import mean


ROOT = Path(__file__).resolve().parent
DEFAULT_COUNTS = ROOT / "dataset" / "BlockLevel_Counts_Panel261.csv"
DEFAULT_GRID = ROOT / "dataset" / "Downtown_BlockGrid.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank blocks and neighborhoods by homeless concentration."
    )
    parser.add_argument("--counts", type=Path, default=DEFAULT_COUNTS)
    parser.add_argument("--grid", type=Path, default=DEFAULT_GRID)
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of block results to print to the terminal (default: 20).",
    )
    return parser.parse_args()


def number(value: str | None) -> float:
    if value is None or not value.strip():
        return 0.0
    return float(value)


def load_locations(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {row["block_id"]: row for row in csv.DictReader(handle)}


def load_observations(path: Path) -> tuple[dict[str, list[dict]], list[str]]:
    observations: dict[str, list[dict]] = defaultdict(list)
    dates: set[str] = set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            people = (
                number(row.get("individuals"))
                + number(row.get("tents_structures"))
                + number(row.get("vehicles"))
            )
            observations[row["block_id"]].append(
                {"date": row["count_date"], "people": people, "area": row["area"]}
            )
            dates.add(row["count_date"])
    return observations, sorted(dates)


def build_block_ranking(
    observations: dict[str, list[dict]], locations: dict[str, dict[str, str]], dates: list[str]
) -> list[dict]:
    latest_date = dates[-1]
    results = []
    for block_id, rows in observations.items():
        by_date = {row["date"]: row["people"] for row in rows}
        values = [by_date.get(date, 0.0) for date in dates]
        location = locations.get(block_id, {})
        streets = [location.get("st_east", ""), location.get("st_north", "")]
        results.append(
            {
                "block_id": block_id,
                "area": rows[0]["area"],
                "cross_streets": " & ".join(part for part in streets if part),
                "lon": location.get("lon", ""),
                "lat": location.get("lat", ""),
                "historical_mean": round(mean(values), 2),
                "latest_people": round(by_date.get(latest_date, 0.0), 2),
                "historical_max": round(max(values), 2),
                "dates_nonzero": sum(value > 0 for value in values),
                "persistence": round(sum(value > 0 for value in values) / len(dates), 3),
                "latest_date": latest_date,
            }
        )
    return sorted(
        results,
        key=lambda row: (
            -row["historical_mean"],
            -row["latest_people"],
            -row["persistence"],
            row["block_id"],
        ),
    )


def build_area_ranking(
    observations: dict[str, list[dict]], dates: list[str]
) -> list[dict]:
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for rows in observations.values():
        for row in rows:
            totals[row["area"]][row["date"]] += row["people"]

    latest_date = dates[-1]
    results = []
    for area, by_date in totals.items():
        values = [by_date.get(date, 0.0) for date in dates]
        results.append(
            {
                "area": area,
                "historical_mean": round(mean(values), 2),
                "latest_people": round(by_date.get(latest_date, 0.0), 2),
                "historical_max": round(max(values), 2),
                "latest_date": latest_date,
            }
        )
    return sorted(results, key=lambda row: (-row["historical_mean"], row["area"]))


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    if args.top < 1:
        raise SystemExit("--top must be at least 1")

    locations = load_locations(args.grid)
    observations, dates = load_observations(args.counts)
    if not observations or not dates:
        raise SystemExit(f"No observations found in {args.counts}")

    blocks = build_block_ranking(observations, locations, dates)
    areas = build_area_ranking(observations, dates)
    block_output = args.output_dir / "homeless_hotspots_ranked.csv"
    area_output = args.output_dir / "homeless_areas_ranked.csv"
    write_csv(block_output, blocks)
    write_csv(area_output, areas)

    print(f"Observation dates: {dates[0]} to {dates[-1]} ({len(dates)} dates)")
    print("Ranking metric: historical mean person-equivalents per count")
    print(f"\nTop {min(args.top, len(blocks))} blocks:")
    for rank, row in enumerate(blocks[: args.top], 1):
        print(
            f"{rank:>2}. {row['cross_streets'] or row['block_id']} "
            f"({row['area']}): mean={row['historical_mean']:.2f}, "
            f"latest={row['latest_people']:.2f}, max={row['historical_max']:.2f}"
        )
    print("\nNeighborhoods:")
    for rank, row in enumerate(areas, 1):
        print(
            f"{rank:>2}. {row['area']}: mean={row['historical_mean']:.2f}, "
            f"latest={row['latest_people']:.2f}, max={row['historical_max']:.2f}"
        )
    print(f"\nWrote {block_output}")
    print(f"Wrote {area_output}")


if __name__ == "__main__":
    main()
