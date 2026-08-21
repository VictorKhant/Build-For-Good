# Build-For-Good

## Current system

The active implementation is the system-level allocation pipeline on the
`Patrick` branch:

- `dataset/`: authoritative source data
- `calc/`: normalized inputs, simulation data, OSRM route cache, calculation code, and archives
- `optim/`: LP code, baseline/optimized outputs, and organized review package
- `api/`: FastAPI endpoints for SQL data, Greedy, global LP, and simulation runs

Runtime data is stored in `calc/sql/bellyup.db`. Original CSV snapshots are
preserved under `calc/original_files/`.

Start the API and open Swagger at `http://127.0.0.1:8000/docs`:

```bash
python3 -m uvicorn api.main:app --reload --port 8000
```

Start with `optim/README.md`. The readable comparison is
`optim/package/comparison/estimate_before_after.md`.

## Archived work

Earlier prototypes were moved, not deleted, under `calc/past_packages/`:

- `legacy_hotspot_analysis/`: early demand-point and hotspot calculations
- `legacy_matching_map/`: pair matching, demo routes, and the old HTML map
- `exploratory_data/`: independent Lane C and Lane E explorations
- `python_cache/`: historical compiled Python cache files

These files are retained for reference but are not inputs to the current
optimization pipeline. See `calc/past_packages/README.md` for restoration notes.

## Original project notes

Idea decided
https://docs.google.com/document/d/1nCFYEG20TWInWzfGQdEUBfr9bYeSWD21FrcXUpcACO8/edit?usp=sharing


not homeless driven but hubspot driven


cost = distance and if there is enough food to be worth the trip (drivers time/wage)






https://docs.google.com/document/d/1N3y-IhrwdAa8J013vRXJEiilZxjxhp-DVIZHVV5tT_o/edit?usp=sharing

datasets:
https://drive.google.com/drive/folders/1cJ6_sIiJ8FG_IqZ7LN4ET__ZR_N8yWwv
