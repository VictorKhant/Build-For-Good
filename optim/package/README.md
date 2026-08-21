# BellyUp optimization package

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

- `inputs/`: businesses.csv, agencies.csv, hotspots.csv, mobile_pantries.csv, demo_donation_reports.csv
- `routes/`: route_matrix.csv
- `baseline/`: baseline_allocations.csv
- `optimized/`: optimized_allocations.csv
- `comparison/`: comparison.json, agency_summary.csv, hotspot_summary.csv
- `docs/`: README.md, README_DATA_PROVENANCE.md
- `code/`: prepare_optimization_inputs.py, road_routing.py, build_route_matrix.py, optimize_allocations.py, validate_optimization_inputs.py, validate_optimization_outputs.py, run_optimization_pipeline.py

See `comparison/estimate_before_after.md` for the readable estimate and
`comparison/comparison.json` for machine-readable metrics.

Regenerate the pipeline and package:

```bash
python3 optim/run_optimization_pipeline.py
python3 optim/package_optimization_results.py
```
