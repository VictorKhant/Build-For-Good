# BellyUp — engine internals

> **Start at the repo root `README.md`** for what this is and how to run it.
> This file covers the role-scoped build served at `/roles`.
>
> The main demo at `/` is the **dispatch board**: `demo_data.py` + `dispatch.py`
> + `registry.py` + `static/board/`. It uses the real datasets in `dataset/` and
> supersedes the simulated agency roster described below.

Connecting small restaurants with agencies that feed unsheltered San Diegans.

```
small restaurant  --collected by-->  AGENCY  ------------------>  people
                                       |
                       fixed pantry:   people walk in
                       mobile pantry:  goes out to hubspots
```

The restaurant pays nothing and carries no transport liability. It donates for
the enhanced tax deduction. The agency absorbs the cost of every run — so cost
efficiency is still what gets optimised, just on the agency's books.

## Run

```bash
../.venv/bin/python -m uvicorn app:app --port 8000   # / board, /roles views

../.venv/bin/python run_demo.py            # scenarios, both legs, no browser
../.venv/bin/python verify.py              # what still needs a human
../.venv/bin/python simulate_agencies.py   # regenerate the placeholder roster
../.venv/bin/python seed.py                # hubspots + geocoding
```

## Modules

| file | role |
|---|---|
| `needs.py` | block need + area forecast from the DSDP record |
| `demand.py` | apportioned demand budgets + the delivery ledger — core IP |
| `agencies.py` | agency loader, walk-in vs mobile intake demand — core IP |
| `economics.py` | cost model, `CONFIG`, tax deduction estimate |
| `collection.py` | **leg 1** — which agency collects — core IP |
| `distribution.py` | **leg 2** — mobile pantry run to hubspots — core IP |
| `pipeline.py` | chains both legs |
| `rules.py` | shared windows, storage, rejection reporting |
| `seed.py` | hubspots, geocoding |
| `sb1383_donors.py` | donor layer from OpenStreetMap |
| `simulate_agencies.py` | **placeholder** agency data |
| `verify.py` | verification worksheet, ranked by demo impact |

### The dispatch board (served at `/`)

| file | role |
|---|---|
| `demo_data.py` | loads `dataset/` into the shapes the board expects |
| `dispatch.py` | reward−cost engine, freshness, hotspot ledger — core |
| `registry.py` | persistence: registrations, nightly reports, opt-outs |
| `geocode.py` | address → coordinates (Census, then Nominatim) |
| `static/board/` | the board UI |

`matching.py` is **superseded** by `collection.py` + `distribution.py` and is no
longer imported anywhere. Safe to delete.

## The two demand models

The asymmetry is the point.

**Fixed pantry — capped.** People come to it, so what it can usefully absorb is
bounded by the counted population within walking distance
(`WALK_IN_RADIUS_M`, 800 m). Give it more and the surplus rots on a shelf.
Catchments are apportioned so two pantries on the same blocks do not each claim
the whole population.

**Mobile pantry — uncapped.** It travels, so its reach is not bound to one
address; overflow is not a concern. It is also the only kind that can serve a
hubspot: a fixed pantry cannot bring food to someone sleeping on a block.

**Hubspots — capped, always.** An agency that travels can find somewhere to take
food, but a single block only holds so many people. Food dropped beyond that is
food left on a pavement.

## Two radii, two different questions

- `SERVICE_RADIUS_M` (300 m) — which blocks to *attribute* to a site for
  scoring. Small because downtown blocks are ~90×60 m; at 800 m each site sweeps
  120–190 of 382 cells and the ranking inverts.
- `WALK_IN_RADIUS_M` (800 m) — how far a person will actually *walk* carrying
  food home. Larger, and applies to fixed pantries only.

## Why the pitch changed

Small restaurants are **not** covered by SB 1383 — the mandate reaches Tier One
(supermarkets) and Tier Two (250+ seats, 5,000 sq ft) generators only. Of the 379
OSM donors in the downtown bbox, ~196 are the small independents this targets and
none are legally required to donate. "The supply is mandated" is no longer the
argument. The **enhanced deduction under IRC §170(e)(3)** is the incentive, which
is why `economics.tax_deduction()` computes and displays it.

## Known limitations

- Straight-line distance × road factor, not routed. Fine for triage, not dispatch.
- A mobile pantry that collects at 15:00 may not run until the next day. The
  engine reports the deferral rather than pretending it is instant.
- Monthly visual street count: a known undercount, measuring visibility as much
  as prevalence.
- Tax figures are estimates from per-pound defaults, labelled as such everywhere.

## Not yet verified

**`data/agencies.json` is simulated.** Every agency in it is invented. Replace it
per `AGENCY_SCHEMA.md`; no code changes needed. `verify.py` prints a banner while
placeholder data is in place.

This applies to the `/roles` views only. The dispatch board at `/` uses the real
`dataset/agencies.csv` and `dataset/mobile_pantries.csv`.
