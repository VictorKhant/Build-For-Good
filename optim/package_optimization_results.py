#!/usr/bin/env python3
"""Package optimization inputs, code, outputs, and an estimate report."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = PROJECT_ROOT / "optim" / "package"

GROUPS = {
    "inputs": [
        "calc/optimization_data/businesses.csv",
        "calc/optimization_data/agencies.csv",
        "calc/optimization_data/hotspots.csv",
        "calc/optimization_data/mobile_pantries.csv",
        "calc/optimization_data/demo_donation_reports.csv",
    ],
    "routes": ["calc/route_cache/route_matrix.csv"],
    "baseline": ["optim/output/baseline_allocations.csv"],
    "optimized": ["optim/output/optimized_allocations.csv"],
    "comparison": [
        "optim/output/comparison.json",
        "optim/output/agency_summary.csv",
        "optim/output/hotspot_summary.csv",
    ],
    "docs": ["optim/README.md", "calc/README_DATA_PROVENANCE.md"],
    "code": [
        "calc/prepare_optimization_inputs.py",
        "calc/road_routing.py",
        "calc/build_route_matrix.py",
        "optim/optimize_allocations.py",
        "calc/validate_optimization_inputs.py",
        "optim/validate_optimization_outputs.py",
        "optim/run_optimization_pipeline.py",
    ],
}


def copy_group(group: str, paths: list[str]) -> None:
    destination = PACKAGE / group
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for relative in paths:
        source = PROJECT_ROOT / relative
        if not source.exists():
            raise FileNotFoundError(source)
        shutil.copy2(source, destination / source.name)


def fmt(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


def change(after: float, before: float) -> float:
    return after - before


def build_estimate() -> str:
    comparison = json.loads((PROJECT_ROOT / "optim" / "output" / "comparison.json").read_text())
    before, after, improvement = (
        comparison["before"], comparison["after"], comparison["improvement"]
    )
    savings = before["transport_cost"] - after["transport_cost"]
    miles_saved = before["total_route_distance"] - after["total_route_distance"]
    minutes_saved = (
        before["total_route_duration_minutes"] - after["total_route_duration_minutes"]
    )
    meal_miles_saved = before["total_meal_miles"] - after["total_meal_miles"]

    baseline = pd.read_csv(PROJECT_ROOT / "optim" / "output" / "baseline_allocations.csv")
    optimized = pd.read_csv(PROJECT_ROOT / "optim" / "output" / "optimized_allocations.csv")
    agency_names = pd.read_csv(PROJECT_ROOT / "calc" / "optimization_data" / "agencies.csv").set_index(
        "agency_id"
    ).agency_name.to_dict()

    workload_rows = []
    for agency_id, agency_name in agency_names.items():
        baseline_rows = baseline[baseline.agency_id.eq(agency_id)]
        optimized_rows = optimized[optimized.agency_id.eq(agency_id)]
        workload_rows.append(
            f"| {agency_name} | {baseline_rows.business_id.nunique()} | "
            f"{baseline_rows.hotspot_block_id.nunique()} | "
            f"{optimized_rows.business_id.nunique()} | "
            f"{optimized_rows.hotspot_block_id.nunique()} |"
        )

    return f"""# Before vs After estimate

Generated from the checked optimization outputs. Both methods use the same 14
synthetic donation reports, 161 candidate hotspots, four routable agencies,
and the same cached OSRM road matrix.

## Executive estimate

Global optimization preserves full candidate-demand coverage while reducing
the modelled allocation-route mileage by **{fmt(abs(improvement['route_distance_change_pct']))}%**.
Under the sourced mileage and wage assumptions, estimated transport cost falls
from **${fmt(before['transport_cost'])}** to **${fmt(after['transport_cost'])}**,
an estimated saving of **${fmt(savings)} per modeled dispatch set**.

This is a planning estimate, not an operating budget quote. The model is a
continuous allocation LP and does not consolidate stops into vehicle tours.

## Metric comparison

| Metric | Before: greedy | After: global LP | Change |
|---|---:|---:|---:|
| Meals available | {fmt(before['total_meals_available'])} | {fmt(after['total_meals_available'])} | 0.00 |
| People fed / demand served | {fmt(before['people_fed'])} | {fmt(after['people_fed'])} | {fmt(change(after['people_fed'], before['people_fed']))} |
| Demand coverage | {fmt(before['demand_coverage_pct'])}% | {fmt(after['demand_coverage_pct'])}% | {fmt(improvement['coverage_percentage_point_change'])} pp |
| Unmet demand | {fmt(before['unmet_demand'])} | {fmt(after['unmet_demand'])} | {fmt(change(after['unmet_demand'], before['unmet_demand']))} |
| Food utilization | {fmt(before['food_utilization_pct'])}% | {fmt(after['food_utilization_pct'])}% | {fmt(improvement['food_utilization_change'])} pp |
| Hotspots served | {before['hotspots_served']} | {after['hotspots_served']} | {after['hotspots_served'] - before['hotspots_served']} |
| Allocation-route miles | {fmt(before['total_route_distance'])} | {fmt(after['total_route_distance'])} | **-{fmt(miles_saved)} ({fmt(abs(improvement['route_distance_change_pct']))}%)** |
| Average allocation route | {fmt(before['average_route_distance'])} mi | {fmt(after['average_route_distance'])} mi | {fmt(change(after['average_route_distance'], before['average_route_distance']))} mi |
| Meal-miles | {fmt(before['total_meal_miles'])} | {fmt(after['total_meal_miles'])} | **-{fmt(meal_miles_saved)} ({fmt(abs(improvement['meal_miles_change_pct']))}%)** |
| Meals per route mile | {fmt(before['meals_per_mile'], 3)} | {fmt(after['meals_per_mile'], 3)} | **+{fmt(improvement['meals_per_mile_change_pct'])}%** |
| Summed route duration | {fmt(before['total_route_duration_minutes'])} min | {fmt(after['total_route_duration_minutes'])} min | **-{fmt(minutes_saved)} min** |
| Transport cost proxy | ${fmt(before['transport_cost'])} | ${fmt(after['transport_cost'])} | **-${fmt(savings)} ({fmt(abs(improvement['transport_cost_change_pct']))}%)** |

## Interpretation

- People fed does not increase because the synthetic supply is about 1,744
  meals while candidate demand is only 938.6. Greedy already covers 100%.
- The LP therefore creates value through efficiency: it preserves Stage 1's
  maximum service and minimizes meal-miles in Stage 2.
- Food utilization remains 53.81% because the test scenario has substantially
  more food than candidate hotspot demand.
- `total_route_distance` sums one full Agency → Supplier → Hotspot route for
  each positive allocation arc. It is suitable for a controlled Before/After
  comparison, but it is not a consolidated multi-stop vehicle route.

## Agency workload comparison

| Agency | Baseline suppliers | Baseline hotspots | Optimized suppliers | Optimized hotspots |
|---|---:|---:|---:|---:|
{chr(10).join(workload_rows)}

The LP is not instructed to balance agencies and no authoritative capacity is
available. Work may therefore concentrate at the lowest-distance eligible
agency. Capacity and utilization remain blank rather than invented.

## Estimate assumptions

- Pounds per meal: 1.2
- Vehicle cost: $0.76 per mile
- Labor: $17.75 per hour
- Road distance/time: cached OSRM/OpenStreetMap driving matrix
- Demand: main `hotspots.csv` historical demand, candidate threshold >= 1
- Donations: synthetic, reproducible Oscar UI reports tied to real businesses
- Three agency locations: explicitly marked Oscar UI demo geocodes
- Agency capacity: unavailable and not constrained
- Food value and net benefit: not estimated because no reliable production
  value is available
"""


def build_readme() -> str:
    rows = []
    for group, paths in GROUPS.items():
        rows.append(f"- `{group}/`: " + ", ".join(Path(path).name for path in paths))
    return """# BellyUp optimization package

Organized snapshot of the BellyUp Before/After optimization. Source pipeline
files remain in their original locations; this folder is a review package.

## What the system does

BellyUp combines homeless demand, surplus-food supply, food-bank agencies, and
actual road travel into one allocation problem. It then uses two-stage linear
programming to maximize service first and minimize transportation second.

There are **four major stages in the full pipeline**. Stage 4, the optimizer,
internally contains a **two-stage LP**. These are two different uses of the
word “stage.”

```text
Historical homeless data
        -> [1] Demand modeling
        -> 161 candidate hotspots + estimated demand

Donation reports at real businesses
        -> [2] Supply modeling
        -> suppliers + available meals

Suppliers + agencies + hotspots
        -> [3] Distribution network
        -> OSRM road distance/time matrix + feasible allocation arcs

Feasible network
        -> [4] Global optimization
        -> LP Stage 1: maximize demand served
        -> LP Stage 2: minimize meal-miles at the Stage 1 service level
        -> optimized allocations
```

### 1. Demand modeling — where is food needed?

Historical homeless counts are converted into estimated hotspot demand. The
current scenario retains 161 blocks with `historical_demand >= 1`:

```text
Hotspot = (location, estimated meal demand)
```

Total candidate demand is **938.60 meals/person-equivalents**.

### 2. Supply modeling — where is surplus food available?

The test scenario contains 14 reproducible synthetic donation reports tied to
real hotels, supermarkets, hospitals, venues, and other businesses:

```text
Supplier = (location, available food/meals)
```

Total modeled supply is **1,744.17 meals**, so supply exceeds candidate demand.
The synthetic flag is retained explicitly; these reports are not presented as
observed production donations.

### 3. Distribution network — how can food move?

The physical operating path is modeled as:

```text
Agency -> Supplier pickup -> Homeless hotspot delivery
```

For each eligible supplier-agency-hotspot combination, the cached OSRM /
OpenStreetMap matrix supplies actual driving distance and duration. This joins
the three independent datasets into a network whose arcs carry distance, time,
and estimated transport cost.

### 4. Global optimization — how should food be allocated?

For allocation variable `x[s,a,h]`, the optimizer considers all feasible
suppliers `s`, agencies `a`, and hotspots `h` simultaneously.

**LP Stage 1 — maximize service.** Maximize total meals delivered subject to
donation supply, hotspot demand, food compatibility, and route availability.
This makes service the first priority and avoids the trivial minimum-cost
answer of delivering nothing. The current maximum is **938.60 / 938.60**, or
**100% demand coverage**.

**LP Stage 2 — minimize transportation.** Preserve the maximum service level
from Stage 1, then minimize total meal-miles:

```text
minimize sum(x[s,a,h] * road_distance[s,a,h])
```

This asks how the entire 938.60-meal allocation can be rearranged to reduce
transportation without feeding fewer people.

## Why global LP improves on greedy

The baseline processes reports sequentially and chooses the closest eligible
agency and remaining hotspots at each step. That is deterministic and easy to
explain, but each decision sees only the current report. A locally short route
can consume a combination that would be more valuable to a later donation.

The LP considers every feasible supplier × agency × hotspot allocation at the
same time. It therefore finds a system-wide solution rather than a sequence of
locally attractive decisions.

## Current Before/After result

| Metric | Greedy baseline | Global LP | Change |
|---|---:|---:|---:|
| People/demand served | 938.60 | 938.60 | 0.00 |
| Demand coverage | 100.00% | 100.00% | 0.00 pp |
| Food utilization | 53.81% | 53.81% | 0.00 pp |
| Modeled allocation-route miles | 939.96 | 803.41 | **-14.53%** |
| Meal-miles | 5,087.54 | 4,383.84 | **-13.83%** |
| Estimated transport cost | $1,260.36 | $1,062.66 | **-$197.70** |

People served and food utilization do not increase because supply already
exceeds total candidate demand and the greedy baseline reaches full coverage.
The LP's value in this scenario is transportation efficiency while preserving
the same service level.

The mileage values sum a full Agency -> Supplier -> Hotspot route for each
positive allocation arc. This is a controlled allocation comparison, not yet
a consolidated multi-stop vehicle-routing or fleet schedule.

## Folders

""" + "\n".join(rows) + """

See `comparison/estimate_before_after.md` for the readable estimate and
`comparison/comparison.json` for machine-readable metrics.

Regenerate the pipeline and package:

```bash
python3 optim/run_optimization_pipeline.py
python3 optim/package_optimization_results.py
```
"""


def main() -> None:
    PACKAGE.mkdir(exist_ok=True)
    for group, paths in GROUPS.items():
        copy_group(group, paths)
    comparison_dir = PACKAGE / "comparison"
    (comparison_dir / "estimate_before_after.md").write_text(
        build_estimate(), encoding="utf-8"
    )
    (PACKAGE / "README.md").write_text(build_readme(), encoding="utf-8")
    print(f"Packaged results at {PACKAGE}")
    for group in GROUPS:
        print(f"  {group}: {len(list((PACKAGE / group).iterdir()))} files")


if __name__ == "__main__":
    main()
