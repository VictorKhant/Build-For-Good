#!/usr/bin/env python3
"""Build simulation inputs and run capacity-constrained planning."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = PROJECT_ROOT / "optim" / "output" / "simulation"


def run(relative_path: str, *args: str) -> None:
    print(f"\n== {relative_path} {' '.join(args)} ==", flush=True)
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / relative_path), *args],
        cwd=PROJECT_ROOT,
        check=True,
    )


def write_report() -> None:
    comparison = json.loads((OUTPUT / "comparison.json").read_text())
    before = comparison["before"]
    after = comparison["after"]
    improvement = comparison["improvement"]
    allocations = pd.read_csv(OUTPUT / "optimized_allocations.csv")
    agencies = pd.read_csv(
        PROJECT_ROOT / "calc" / "simulation_data" / "agency_capacity.csv"
    )[["agency_id", "agency_name", "capacity_meals_per_day"]]
    businesses = pd.read_csv(
        PROJECT_ROOT / "calc" / "optimization_data" / "businesses.csv"
    )[["business_id", "business_name"]]
    allocations = allocations.merge(agencies, on="agency_id").merge(
        businesses, on="business_id"
    )

    agency_rows = []
    for agency in agencies.itertuples(index=False):
        rows = allocations[allocations.agency_id.eq(agency.agency_id)]
        meals = rows.meals_allocated.sum()
        agency_rows.append(
            f"| {agency.agency_name} | {agency.capacity_meals_per_day:.0f} | "
            f"{meals:.2f} | {meals / agency.capacity_meals_per_day * 100:.2f}% | "
            f"{rows.business_id.nunique()} | {rows.hotspot_block_id.nunique()} |"
        )

    supplier_rows = []
    grouped = allocations.groupby(["agency_name", "business_name"], sort=True).agg(
        meals=("meals_allocated", "sum"),
        hotspots=("hotspot_block_id", "nunique"),
    ).reset_index()
    for row in grouped.itertuples(index=False):
        supplier_rows.append(
            f"| {row.agency_name} | {row.business_name} | "
            f"{row.meals:.2f} | {row.hotspots} |"
        )

    report = f"""# Capacity-constrained simulation plan

This scenario uses the supplier quantities and synthetic daily agency
capacities in `calc/simulation_data/`. Capacity is enforced in both the greedy
baseline and the two-stage LP.

## Service result

- Available supply: **{after['total_meals_available']:.2f} meals**
- Candidate demand: **{after['total_demand']:.2f} meals**
- People/demand served: **{after['people_fed']:.2f}**
- Coverage: **{after['demand_coverage_pct']:.2f}%**
- Unmet demand: **{after['unmet_demand']:.2f}**

## Optimized agency plan

| Agency | Capacity meals/day | Assigned meals | Utilization | Suppliers | Hotspots |
|---|---:|---:|---:|---:|---:|
{chr(10).join(agency_rows)}

## Agency-to-supplier plan

| Agency | Supplier | Meals | Hotspots |
|---|---|---:|---:|
{chr(10).join(supplier_rows)}

## Greedy vs global LP under capacity

| Metric | Greedy | Global LP | Change |
|---|---:|---:|---:|
| People served | {before['people_fed']:.2f} | {after['people_fed']:.2f} | {improvement['additional_people_fed']:.2f} |
| Allocation-arc miles | {before['total_route_distance']:.2f} | {after['total_route_distance']:.2f} | {improvement['route_distance_change_pct']:+.2f}% |
| Meal-miles | {before['total_meal_miles']:.2f} | {after['total_meal_miles']:.2f} | {improvement['meal_miles_change_pct']:+.2f}% |

The LP objective is meal-miles, so it reduces distance weighted by the number
of meals. The unweighted sum of full route miles across positive allocation
arcs can rise when the LP splits flow across more arcs. Neither value is a
consolidated multi-stop vehicle tour; vehicle routing remains future work.

All agency capacities are synthetic planning assumptions, not confirmed
operational capacities.
"""
    (OUTPUT / "simulation_plan.md").write_text(report, encoding="utf-8")
    print(f"simulation report={OUTPUT / 'simulation_plan.md'}")


def main() -> None:
    run("calc/build_simulation_dataset.py")
    run("calc/validate_optimization_inputs.py")
    run("optim/optimize_allocations.py", "--simulation")
    run("optim/validate_optimization_outputs.py", "--simulation")
    write_report()


if __name__ == "__main__":
    main()
