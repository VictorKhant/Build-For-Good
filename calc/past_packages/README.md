# Past packages

This directory contains superseded prototypes moved from the repository root
on 2026-08-21. Nothing was deleted or merged destructively.

## Contents

- `legacy_hotspot_analysis/`: the original downtown hotspot parsing,
  demand-point construction, three-set distance calculation, and their CSV/TXT
  outputs.
- `legacy_matching_map/`: the original single-donation matching scripts, demo
  inputs/results, and `hotspot_map.html`.
- `exploratory_data/`: Lane C encampment request and Lane E rent trend extracts.
- `python_cache/`: compiled cache files that previously sat in `__pycache__/`.

## Current replacement

The maintained flow is in the repository root:

```text
dataset/
  -> calc/prepare_optimization_inputs.py
  -> calc/optimization_data/
  -> calc/build_route_matrix.py
  -> calc/route_cache/
  -> optim/optimize_allocations.py
  -> optim/output/
  -> optim/package_optimization_results.py
  -> optim/package/
```

Run `python3 optim/run_optimization_pipeline.py` to reproduce the active results.

## Restoring an archived prototype

The archived scripts were originally written with repository-root-relative
paths. To rerun one unchanged, move its complete group back to the repository
root on a temporary branch. The authoritative raw files in `dataset/` were not
moved.
