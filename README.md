# BellyUp

**Surplus food → the streets that need it. San Diego.**

Food businesses report end-of-day surplus. The platform picks the best
**collector → hotspot** dispatch — an agency box truck or a mobile pantry unit —
by maximising need-weighted meals served net of what the run actually costs to
operate.

```
business reports surplus  ──►  COLLECTOR  ──►  HOTSPOT
   (restaurant, hotel,          (agency truck      (block where unsheltered
    grocery, venue)              or pantry van)     people are counted)
```

Nobody pays to donate. The collector absorbs the cost of the run, so cost
efficiency is optimised on *their* books — and the donor gets a fair-market-value
figure for the deduction.

---

## Run it

```bash
python3 -m venv .venv
./.venv/bin/pip install fastapi uvicorn pandas numpy shapely requests

cd bellyup
../.venv/bin/python -m uvicorn app:app --port 8000
```

Then open **http://localhost:8000**.

| Route | What it is |
|---|---|
| `/` | **Dispatch board** — the main demo |
| `/roles` | Earlier three-role view (restaurant / agency / find-food) |

Console harnesses, no browser needed:

```bash
cd bellyup
../.venv/bin/python run_demo.py   # rehearsed scenarios, both legs
../.venv/bin/python verify.py     # what still needs a human to check
```

---

## The dispatch board

**Left — tonight's reports.** Fourteen businesses reporting surplus, each with
pounds, what it is, a pickup window and an expiry. Click one to compute its
dispatch.

**Centre — the map.** 207 hotspot blocks sized by need, suppliers, agency HQs,
fixed drop-off sites and mobile pantry sites. The match animates: it scans candidate blocks, shortlists,
then locks a route.

**Right — the recommendation.** The winning collector → pickup → hotspot triple,
people fed, route miles, net benefit, the reward/cost breakdown, what got ruled
out and why, and the runners-up.

**Confirm to book it.** A dispatch is a recommendation until you confirm it.
Confirming issues a receipt, enters the delivery ledger, and marks the hotspot
served on the map.

**Ledger** (top right). Every confirmed dispatch: tonight's plus the past week.
Delivery log, donor receipts aggregated for tax records, and which hotspots have
been served. "Reset tonight" clears the evening; history stays.

**Light / dark** toggle beside it. The choice persists per browser, and the map
tiles and geometry follow it.

### Restaurants can register themselves

The **"+ Report surplus · new restaurant"** button takes a name and an address
(geocoded — no coordinates to look up), then tonight's numbers: pounds, food
type, pickup window, expiry, condition.

A registered restaurant is **appended to `dataset/businesses.csv`** and is a
partner from then on. Every business on the board — curated or self-registered —
has an **UPDATE** button, because surplus differs every night and a fixed
quantity per partner would make the feed a fixture rather than a report.
"Nothing to donate tonight" is a real answer and clears the report.

---

## The model

```
net    = reward − cost

reward = meals served × $4.25 × accessBoost × freshness
       + overflow meals × $4.25 × 0.5
cost   = (drive + 25 min handling) × $17.75/hr
       + road miles × $0.76/mi
```

**`accessBoost`** weights up blocks with poor scheduled food access:
`1 + 0.5 × (7 − access days/week) / 7`. A block already served daily needs the
next van less than one served twice a month.

**`freshness`** decays with the share of the food's life spent by the time it
reaches a person, floored at 0.35. This is why a four-hour hotel tray behaves
differently from bakery goods with two days on them — without hard-coding a
preference for either.

**Hard constraints** reject a pair outright, each with a reason the donor can act
on: `PICKUP_WINDOW_MISSED`, `EXPIRES_BEFORE_SERVED`, `TRANSIT_TOO_LONG`,
`BLOCK_NEED_MET`, `NO_PREPARED_HANDLING`.

**`mobile_capable`** decides who can be dispatched, and it changes the shape of
the match. An agency with a vehicle runs two legs — out to the restaurant, on to
a hotspot. One marked `no` is a fixed site with no vehicle, so it is **one leg**:
the food goes to the site and people come to it. There is no hotspot to serve
and no distribution run to cost.

A drop-off is credited at `DROPOFF_CREDIT` (0.5) — the same rate the model
already gives overflow meals that "ride along to the pantry network", because
that is exactly what it is. Stocking a pantry is worth less than feeding a
counted block tonight, so a drop-off only outranks a routed run when that run
genuinely was not worth making. Tonight, routed wins all 14 reports.

A drop-off still has to pay for itself. If the site is far enough that the run
costs more than the food is worth, it is refused with `DROPOFF_NOT_WORTH_IT` —
moving food to a pantry you cannot afford to reach is not a rescue.

**Serving limits** come out of the ledger, and they answer two different
questions. A block only holds so many people, so once its need is met further
food there is food left on a pavement. And nobody sends five separate vans to
one corner in an evening, so `MAX_DROPS_PER_NIGHT` caps deliveries per block
regardless of need remaining. A block hitting either limit leaves the candidate
pool and turns green on the map; a partially served one only offers what is
left.

---

## Data

Read from `dataset/`. Sources and provenance in `README_DATA_PROVENANCE.md`;
`COLUMN_PROVENANCE.csv` marks every column EVENT or EXTERNAL.

| File | Rows | What it is |
|---|---|---|
| `hotspots.csv` | 382 | Blocks with need in person-equivalents, from the DSDP count |
| `businesses.csv` | 31 + | Food businesses. **Self-registered restaurants are appended here** |
| `agencies.csv` | 10 | Agencies. `mobile_capable` splits them: 4 collect, 5 receive at a fixed site |
| `mobile_pantries.csv` | 14 | Distribution sites, with schedules |
| `surplus_reports.csv` | — | Tonight's numbers, any supplier. Written by the app |
| `opted_out_businesses.csv` | — | Businesses that left the platform. Written by the app |

Tonight's confirmed deliveries live in memory and clear with "Reset tonight".
The previous week's ledger is generated from a seeded draw so the view has a
yesterday — a platform with no history looks like a prototype.

Plus the raw DSDP event files (`BlockLevel_Counts_Panel261.csv`,
`Downtown_BlockGrid.geojson`, `DowntownCounts_Monthly.csv`, …) that the derived
datasets are built from.

### Where writes go, and why

Self-registered restaurants go into `businesses.csv` itself, using its exact nine
columns, identified by `source_url = "self-registered via BellyUp"`. Everything
else in that file carries a real citation.

Nightly reports live in `surplus_reports.csv` rather than `businesses.csv` —
that table describes *who a business is*, not what it happened to have on one
evening.

Removing a restaurant does one of two things. A self-registered row is deleted
outright. A curated one is recorded as an **opt-out** and filtered at load, so
its externally sourced row is never rewritten — that is also the truer
description: they left the platform, they did not stop existing. The footer
offers an undo.

> ⚠ `build_final_datasets.py` **regenerates** `businesses.csv` from raw sources.
> Re-running it overwrites self-registered restaurants. Their identity fields are
> kept in `surplus_reports.csv` so they can be rebuilt.

---

## Layout

```
bellyup/
  app.py            FastAPI: board, roles, geocoding
  demo_data.py      loads the datasets into the shapes the UI expects
  dispatch.py       the reward−cost engine, freshness, ledger      ← core
  registry.py       persistence: registrations, reports, opt-outs
  geocode.py        address → coordinates (Census, then Nominatim)
  static/board/     the dispatch board UI

  # the earlier role-scoped build, served at /roles
  needs.py          block need + area forecast from the DSDP record
  demand.py         apportioned demand budgets + delivery ledger
  agencies.py       walk-in vs mobile intake demand
  economics.py      cost model, CONFIG, tax deduction
  collection.py     leg 1 — which agency collects
  distribution.py   leg 2 — mobile pantry run to hotspots
  pipeline.py       chains both legs
  pantry_finder.py  public "where can I get food" view
  run_demo.py       console harness
  verify.py         verification worksheet, ranked by demo impact

dataset/            all data, curated + app-written
```

`matching.py` is superseded by `collection.py` + `distribution.py` and is no
longer imported.

---

## Constants, and where they came from

| Constant | Value | Source |
|---|---|---|
| Meal conversion | 1.2 lb | Feeding America |
| Labour | $17.75/hr | City of San Diego minimum wage, eff. 2026-01-01 |
| Vehicle | $0.76/mi | IRS standard mileage rate, eff. 2026-07-01 |
| Meal value | $4.25 | Demo assumption |
| FMV of donated food | $1.79/lb | Demo assumption, for the deduction estimate |

Distances are straight-line × 1.3 at 18 mph — fine for triage, not dispatch.

---

## Known limitations

- **Surplus reports are simulated.** Which businesses report tonight and how much
  is a seeded draw. Voluntary end-of-day reporting is the thing that does not
  exist yet — it is what the platform is for. Everything else is real data.
- The count is a monthly visual street sweep: a known undercount, measuring
  visibility as much as prevalence.
- Two agencies have hand-placed coordinates; A.B. Jones & Co. has no fixed site
  and cannot anchor a cost model, so it is excluded.
- Registrations and reports persist to CSV, not a database. Fine for a demo,
  not concurrent-safe under real load.

---

## Background

- [Idea doc](https://docs.google.com/document/d/1nCFYEG20TWInWzfGQdEUBfr9bYeSWD21FrcXUpcACO8/edit?usp=sharing)
- [Notes](https://docs.google.com/document/d/1N3y-IhrwdAa8J013vRXJEiilZxjxhp-DVIZHVV5tT_o/edit?usp=sharing)
- [Datasets](https://drive.google.com/drive/folders/1cJ6_sIiJ8FG_IqZ7LN4ET__ZR_N8yWwv)
