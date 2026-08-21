# Simulation data

Editable planning inputs for testing supplier supply and agency capacity.

## Files

- `supplier_supply.csv`: 14 current synthetic donation reports joined to their
  real supplier names and locations. `available_meals = available_food_lbs / 1.2`.
- `agency_capacity.csv`: one row per agency with simulated daily meal/lbs
  capacity, vehicle count, and maximum routes per day.
- `simulation_dataset.csv`: a unified long-form view with `entity_type` equal
  to `supplier` or `agency`.

All quantity/capacity rows use `scenario_id=planning_demo_v1` and
`is_synthetic=true`. Supplier quantities reproduce the current Oscar-aligned
demo reports. Agency capacities are planning assumptions because authoritative
capacity data is not currently available; replace them with confirmed agency
values before operational use.

Regenerate after changing the source donations or capacity assumptions:

```bash
python3 calc/build_simulation_dataset.py
```

Run capacity-constrained planning:

```bash
python3 optim/run_simulation.py
```

Simulation outputs are written to `optim/output/simulation/` without
overwriting the standard unconstrained results.
