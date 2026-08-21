# BellyUp system optimization

This pipeline runs on the `Patrick` branch and does not modify or push `main`.

## Data contract

Authoritative static data copied from `main`:

- `dataset/hotspots.csv`
- `dataset/businesses.csv`
- `dataset/agencies.csv`
- `dataset/mobile_pantries.csv`

`calc/prepare_optimization_inputs.py` adds deterministic IDs and writes normalized
copies under `calc/optimization_data/`. It also reproduces the 14 seeded reports in
Oscar's current UI: 2,093 synthetic pounds from real business IDs. Every report
has `is_synthetic=true`.

Oscar's UI supplies three explicitly demo-only agency geocodes. They are kept
with `is_synthetic_geocode=true`. A.B. Jones has no fixed HQ and is excluded
from routing. No agency capacity is invented.

The matching demand is `historical_demand`, corresponding to main's stable
`hotspots.csv::need`. `latest_demand` remains separate. To match Oscar's UI,
candidate blocks have `historical_demand >= 1` (161 blocks).

## Before: deterministic greedy

1. Process active reports by `reported_at`, then `donation_id`.
2. Choose the food-compatible, route-available agency closest to the supplier.
3. Visit candidate hotspots in supplier-to-hotspot road-distance order.
4. Allocate no more than remaining donation meals or remaining hotspot demand.
5. Continue to the next hotspot until the donation is exhausted or demand is met.

## After: global two-stage LP

Decision variable `x[d,a,h]` is meals from donation `d`, through agency `a`,
to hotspot `h`. Constraints limit each donation by available meals and each
hotspot by historical demand. Only OSRM-available and food-compatible routes
are variables. Capacity is not constrained because authoritative values do not
exist.

The solver is SciPy HiGHS through `scipy.optimize.linprog`:

1. maximize total meals delivered;
2. preserve the Stage 1 optimum and minimize meal-miles globally.

This is a continuous allocation model, not TSP/CVRP. `total_route_distance`
sums full candidate-route miles for active allocation arcs and therefore does
not claim multi-stop route consolidation.

## Run

Reuse the checked route cache:

```bash
python3 optim/run_optimization_pipeline.py
```

Re-query all OSRM road distances:

```bash
python3 optim/run_optimization_pipeline.py --refresh-routes
```

The refresh requires internet access. OSRM failures are stored as unavailable;
the code does not substitute straight-line or fabricated distances.

## Outputs

- `optim/output/baseline_allocations.csv`
- `optim/output/optimized_allocations.csv`
- `optim/output/comparison.json`
- `optim/output/agency_summary.csv`
- `optim/output/hotspot_summary.csv`

Food value and net benefit remain null because the UI's `$4.25/meal` is labeled
as a demo assumption. Transport cost uses the sourced `$0.76/mile` and
`$17.75/hour` figures documented in `calc/README_DATA_PROVENANCE.md`.
