# BellyUp — Build Spec

*Connecting small restaurants with agencies that feed unsheltered San Diegans.*

**Status: built and running.** This file describes what exists, not a plan.
Run it with `cd bellyup && ../.venv/bin/python -m uvicorn app:app --port 8000`
then open `http://localhost:8000`.

> **Revision note.** This replaces the original spec, which modelled restaurants
> donating to nonprofits who delivered to food-bank partner sites. That model was
> retired after a conversation with a San Diego Food Bank contact. What changed
> and why is recorded in §12, because the reasoning matters more than the diff.

---

## 0. What we are building

A **three-sided platform**. Small restaurants have surplus and no way to move it.
Agencies have vehicles, food-safety compliance and staff. Unsheltered people are
measured by the DSDP count but are not app users.

```
small restaurant  --collected by-->  AGENCY  ------------------>  people
                                       |
                       fixed pantry:   people walk in
                       mobile pantry:  goes out to hubspots
```

| Side | Sees | Optimised for |
|---|---|---|
| **Restaurant** | agencies who will collect | agency cost-efficiency; donor pays nothing |
| **Agency** | hubspots + scheduled pickups | need-weighted allocation, budgets, ledger |
| **Person needing food** | pantries, open now first | proximity, opening hours |

**The restaurant pays nothing and carries no transport liability.** It donates
for the enhanced tax deduction. The agency absorbs the cost of every run — so
cost efficiency is still what gets optimised, just on the agency's books.

**Pitch line:** *The food is being thrown away, the need is measured, and the
matching layer is what's missing.*

### ⚠ Two things that must not break

**Hubspots are agency-facing only.** They are block-level locations where
unsheltered people gather. `/api/agency/*` serves them. The restaurant view and
the public view never do — `_donor_safe()` strips them, and `pantry_finder`
returns pantries only. Publishing "food is handed out at 17th & K at 4pm" would
hand anyone, including someone looking to move people on, a map of where to find
them. Block aggregation was chosen precisely so the output could not be
operationalised for enforcement.

**Block need shading is agency-view only** in the UI, for the same reason.

---

## 1. Stack

No build step, no auth, no database.

- **Backend:** Python 3.11 + FastAPI + uvicorn
- **Data:** pandas, numpy, shapely (no geopandas, no statsmodels)
- **Frontend:** one static `index.html` — vanilla JS + Leaflet, OSM tiles, no key
- **Storage:** in-memory + CSV/GeoJSON/JSON on disk

```
bellyup/
  app.py                  # FastAPI, role-scoped routes
  needs.py                # block need + area forecast
  demand.py               # apportioned demand budgets + delivery ledger
  agencies.py             # agency loader, walk-in vs mobile intake demand
  economics.py            # cost model, CONFIG, freshness, tax deduction
  collection.py           # LEG 1 — which agency collects
  distribution.py         # LEG 2 — mobile pantry run to hubspots
  pipeline.py             # chains both legs
  schedule.py             # pickup schedule + agency intake limits
  pantry_finder.py        # public view
  rules.py                # shared windows, storage, rejection reporting
  seed.py                 # hubspots, geocoding
  sb1383_donors.py        # donor layer from OpenStreetMap
  simulate_agencies.py    # PLACEHOLDER agency data
  scenarios.py            # rehearsed demo scenarios
  run_demo.py             # console harness
  verify.py               # verification worksheet
  data/                   # generated: agencies, donors, destinations, geocode cache
  static/index.html
  AGENCY_SCHEMA.md        # what the real agency roster must contain
```

Datasets are read from `../dataset/` — one source of truth, so a re-pull from
`main` reaches the app with no copy step.

---

## 2. Data models

```python
# donation (from a small restaurant)
{"donor_name": str, "lat": float, "lon": float, "address": str,
 "food_type": "prepared_hot"|"prepared_cold"|"produce"|"packaged_dry"|"bakery"|"dairy",
 "quantity_lbs": float, "condition": "hot"|"refrigerated"|"frozen"|"ambient",
 "ready_at": datetime, "expires_at": datetime}

# agency  (see AGENCY_SCHEMA.md — this is what the real roster must supply)
{"agency_id": str, "name": str, "lat": float, "lon": float,
 "has_mobile_pantry": bool,          # THE most important field
 "mobile_capacity_lbs": float, "mobile_windows": [...], "max_hubspot_stops": int,
 "collects_donations": bool, "accepts": [...], "storage": {...},
 "intake_capacity_lbs_per_day": float, "has_refrigerated_vehicle": bool,
 "wage_per_hour": float, "staff_per_run": int, "cost_per_km": float,
 "operating_windows": [{"dow": 0-6, "start": "08:00", "end": "17:00",
                        "weeks_of_month": [1,3]}]}   # weeks_of_month optional

# hubspot (block-level outreach point, agency-facing only)
{"dest_id": str, "name": str, "lat": float, "lon": float, "block_id": str,
 "need_now": float, "need_trend": float, "capacity_lbs_per_visit": float}
```

`weeks_of_month` exists because most Food Bank partner sites distribute on a
monthly cadence ("1st and 3rd Thursday"). Flattening that to weekly would have
the engine confidently propose runs to sites closed three weeks in four. `-1`
means "last".

---

## 3. `needs.py` — need from the historical record

### 3.1 Block need
1. `Downtown_BlockGrid.geojson` (382 polygons) + `BlockLevel_Counts_Panel261.csv`.
   **Panel261 only** — the full file grows by 121 blocks in 2022 and isn't
   comparable over time.
2. Per block, `individuals + tents_structures + vehicles` (raw observed basis).
3. `need_now` = latest count date. `need_trend` = OLS slope over the last 5
   dates, persons/year.

### 3.2 Area forecast
`DowntownCounts_Monthly.csv`, `area_type == "neighborhood"`, `component == "total"`.
Linear trend + month-of-year dummies + a `fellowship_month` boolean, fitted with
`numpy.linalg.lstsq`. Forecasts hold fellowship at 0.

The fellowship regressor earns its place: it absorbs **+48 people/month** in East
Village. Without it that staffing change lands in the trend and the long decline
reads far steeper than it is.

### 3.3 Two radii — different questions, different answers
- **`SERVICE_RADIUS_M` = 300 m** — which blocks to *attribute* to a site for
  scoring.
- **`WALK_IN_RADIUS_M` = 800 m** — how far a person will actually *walk*
  carrying food home. Fixed pantries only.

The original spec used 800 m for both. On this grid that is wrong for
attribution: downtown blocks run ~90×60 m, so 800 m sweeps 120–190 of the 382
cells, the score measures centrality rather than catchment, and **the ranking
inverts** — Rachel's Women's Center places 1st at 800 m and 8th at 200 m. Both
constants are live-tunable.

### 3.4 Data rules that must not be broken
1. Never sum `total` with `individual`/`tent`/`vehicle` — `total` is already
   adjusted (`≈ individual + 1.75×tent + 2.03×vehicle`).
2. Use `Panel261` for anything across time.
3. Join on `area`, not raw labels. The GeoJSON carries `neighborhood`; canonical
   `area` comes from `Downtown_BlockGrid.csv`.
4. `Outside Perimeter` is **null before April 2021**, not zero.
5. 2025 is missing Jul, Aug, Oct, Nov. Dropped, never interpolated.
6. Polygons, not centroids, for point-in-block joins.
7. Exclude `PRE2017` rows from component work.

---

## 4. `demand.py` + `agencies.py` — supply must not exceed absorption

**This is the part the original spec had no concept of, and it is load-bearing.**

Scored one donation at a time against a static world, the engine sent every
donation to whichever site scored best: twenty downtown restaurants reporting
80 lb each put **1,600 lb into one 350 lb site**, 4.6× capacity, and the surplus
rots. `need_now` is a *stock* (people counted on one night); food demand is a
*flow* (pounds per day).

### 4.1 Apportionment
Catchments overlap, so each block's count is split among the sites serving it.
Apportioned total is **665** against **670** actually counted. Unapportioned it
is 3,006 — a 4.5× overcount of the same people.

### 4.2 The three demand rules — the asymmetry is the point

| | Intake cap | Why |
|---|---|---|
| **Fixed pantry** | **capped** by walk-in population | people come to it; food beyond that rots on a shelf |
| **Mobile pantry** | **uncapped** by demand | it travels; reach is not bound to one address |
| **Hubspot** | **always capped** | a single block only holds so many people |

`daily_demand = people projected DEMAND_HORIZON_DAYS forward ×
MEALS_PER_PERSON_PER_DAY × LBS_PER_MEAL`, capped by physical capacity. A block
whose count is climbing earns a bigger budget *before* the people arrive — the
forecast entering the supply side.

### 4.3 Ledger and limits
`demand.LEDGER` records what each destination has been committed today.
`schedule.LIMITS` lets an agency declare its own daily cap — staff off sick,
fridge full, van in the shop. **A declared limit only ever tightens**; an agency
saying "send me 900 lb" does not make its walk-in population able to eat 900 lb.

---

## 5. `economics.py` — cost, freshness, tax

Every constant lives in `CONFIG`; every one has an entry in `CONFIG_SOURCES`
with a source and a `verified` flag. `verify.py` lists the unverified ones.

### 5.1 Freshness — why mobile pantries win restaurant food
The engine does **not** hard-code a preference for mobile pantries. It models
how long food waits before someone eats it:

- **mobile** — next scheduled run + drive to nearest hubspot + handout ≈ **1.1 h**
- **fixed** — next opening + `FIXED_PANTRY_DWELL_HOURS` (18 h) waiting for
  walk-in traffic ≈ **18.4 h**

Value then decays with the share of shelf life consumed, floored at
`FRESHNESS_FLOOR`. Consequences:

| Donation | Top 3 | Fixed pantries |
|---|---|---|
| prepared hot, 4 h | all mobile | rejected `SPOILS_BEFORE_REACHED` |
| prepared cold, 12 h | all mobile | rejected |
| produce, 2 days | all mobile | viable, 75% freshness |
| packaged dry, 5 days | all mobile | viable, 90% freshness |

Restaurant food goes to mobile pantries every time — **because prepared food
genuinely cannot sit for 18 hours**, not by fiat. This survives the obvious
question, "why not just always use mobile?" `MOBILE_PRIORITY_BONUS` exists for a
blunter thumb on the scale and is deliberately left at **0.0**.

### 5.2 Cost
Leg 1 is a round trip `pantry → restaurant → pantry`. Leg 2 is
`pantry → hub → hub → pantry`, visit order exhaustive over ≤ `MAX_STOPS` so
there is no TSP heuristic to defend. Straight line × `ROAD_FACTOR`; no routing
API, nothing to fail on stage.

```
fuel      = route_km × cost_per_km
personnel = total_min/60 × wage_per_hour × staff_per_run
meals     = lbs / LBS_PER_MEAL
value     = meals × VALUE_PER_MEAL × freshness × need_multiplier
net_value = value − (fuel + personnel)
```

`need_multiplier = 1 + ALPHA_NEED·norm(need_now) + BETA_TREND·norm(need_trend)`.
Min-max normalised across surviving candidates. **`BETA_TREND` is where the
forecast enters the ranking.**

### 5.3 Tax deduction
`basis + 50% of (FMV − basis), capped at 2 × basis` — IRC §170(e)(3) enhanced
deduction for donated food inventory. A 60 lb donation estimates **$135**.
Labelled an estimate everywhere, with "confirm with your accountant".

---

## 6. `collection.py` — leg 1 constraints

First failure rejects the pair. Ordered so the reason reported is the most
useful one, not whichever fired first by accident.

| `reason_code` | Meaning |
|---|---|
| `NOT_COLLECTING` | agency does not collect |
| `QTY_TOO_SMALL` | below the minimum for a dedicated run |
| `TYPE_NOT_ACCEPTED` | agency does not take this food type |
| `NO_STORAGE` | no storage for this condition |
| `NO_COLD_VEHICLE` | cold food, no refrigerated vehicle |
| `NO_WALK_IN_DEMAND` | fixed pantry with no counted population near it |
| `LIMIT_REACHED` | agency's own declared cap for today |
| `INTAKE_SATURATED` | walk-in demand already met today |
| `COLD_CHAIN` | round trip exceeds the safe window |
| `EXPIRES_IN_TRANSIT` | back at the pantry too close to expiry |
| `AGENCY_CLOSED` | not operating at pickup time |
| `SPOILS_BEFORE_REACHED` | would only reach people after the food expires |
| `NET_NEGATIVE` | run costs more than the food is worth |
| `INEFFICIENT` | exceeds the cost-per-meal ceiling |

`rules.summarise()` orders reasons by **what they explain, not how often they
fired**. 416 county sites being seniors-only is the commonest reason and the
least informative; "every pantry has met its demand" is what the donor needs.

Leg 2 (`distribution.py`) allocates greedily on marginal value: each extra stop
is added only when the food it carries outweighs the detour.

---

## 7. API — role-scoped

```
GET  /api/config          POST /api/config        live tuning
GET  /api/blocks          GET  /api/forecast      map + forecast
GET  /api/scenarios

POST /api/restaurant/match      ?commit ?max_km   -> agencies + tax, NO hubspots
GET  /api/restaurant/donors

GET  /api/agency                                  roster
GET  /api/agency/{id}           ?max_km           hubspots + pickups + limit
POST /api/agency/{id}/limit                       declare a daily cap
POST /api/agency/{id}/distribute ?commit          plan the mobile run
DEL  /api/agency/pickup/{id}

GET  /api/pantries        ?lat ?lon ?max_km       PUBLIC — pantries only

POST /api/reset   GET /api/state   POST /api/demo/{key}
```

---

## 8. UI — one page, three roles

Role switcher top-right. Leaflet map left, panel right.

- **Restaurant** — form or map click → ranked agencies, tax estimate, route to
  the winner, grouped rejection reasons. No hubspots, no need shading.
- **Agency** — pick agency, set daily limit, distance filter, scheduled pickups,
  hubspots with demand-fill bars, "plan today's mobile run" drawing the route.
  Block need shading on.
- **Find food** — pantries near you, open now first, walking minutes, hours.
  No hubspots, no need shading.

A banner shows while agency data is simulated.

---

## 9. Honesty rules baked into the product

- **The pantry finder never claims a pantry is empty.** The ledger only knows
  what BellyUp routed; a pantry may have food from the food bank we never saw.
  It says "no BellyUp deliveries today — they may still have food from other
  sources" rather than sending someone on a wasted walk.
- **Every rejected pair returns a reason.** "680 pairs screened, 11 viable,
  here is why the rest failed" is what makes the engine look intelligent rather
  than like a distance sort.
- **Simulated data announces itself** in the UI banner, `run_demo.py`,
  `verify.py`, and a `simulated: true` field on every record.

---

## 10. What to say on stage

**The finding.** Downtown counts, constant footprint: 1,289 average in the 12
months to Jul 2023, 970 in the 12 after — **−24.8%**. The Unsafe Camping
Ordinance took effect 31 Jul 2023; the break lands three to four months later. A
falling street count is what housing people looks like *and* what moving people
looks like. Present both; don't editorialise.

**The waste.** The US discards 30–40% of its food supply *(USDA)*. People two
blocks from full kitchens go hungry.

**The incentive — and why the mandate argument is gone.** SB 1383 reaches Tier
One (supermarkets) and Tier Two (250+ seats, 5,000 sq ft) generators only. Of the
379 OSM food businesses in the downtown bbox, **~196 are the small independents
this targets, and none are legally required to donate**. "The supply is mandated"
is not our pitch. The enhanced deduction under IRC §170(e)(3) is the incentive,
and the platform computes it.

**The bottleneck is absorption, not supply.** Twenty restaurants offering 80 lb
each is 1,600 lb. Downtown's measured daily demand across sites open on a
Wednesday afternoon is ~310 lb. The engine places what fits and **refuses the
rest** — refusing is the correct answer.

**Ethics — unprompted.**
- Routes to organisations and block-level outreach points; never publishes where
  individuals sleep.
- Hubspots are agency-facing only, enforced at the API layer.
- Block aggregation was chosen so the output can't be operationalised for
  enforcement.
- A 24.8% drop is roughly 300 people whose location changed. Say it in human
  terms once.

**Limitations — volunteer them.**
- Monthly visual street sweep: a known undercount measuring visibility as much
  as prevalence.
- Straight-line × road factor, not routed. Fine for triage, not dispatch.
- Cost constants are estimates, cited and tunable per organisation.
- A mobile pantry collecting at 15:00 may not run until the next day. The engine
  reports the deferral rather than pretending it is instant.
- **Agency data is simulated** until the real roster lands.
- The app proposes matches. It does not create the written agreement or
  food-safety compliance the law requires.

---

## 11. Definition of done

- [x] Three role-scoped views, hubspots served only to agencies
- [x] Leg 1 ranks agencies by need-weighted value net of the agency's real cost
- [x] Leg 2 allocates to hubspots against demand budgets and a ledger
- [x] Fixed pantries capped by walk-in demand, mobile uncapped, hubspots capped
- [x] Freshness model routes perishable restaurant food to mobile pantries
- [x] Agencies can declare a daily intake limit; it only tightens
- [x] Scheduled pickups visible to the collecting agency, filterable by distance
- [x] Public pantry finder that never reveals a hubspot
- [x] Tax deduction estimated and labelled
- [x] Every rejection returns a reason, ordered by what it explains
- [x] Need scores from `Panel261` + block grid with §3.4 respected
- [x] A forecast exists and is visible
- [ ] **Real agency roster** — replace `data/agencies.json` per `AGENCY_SCHEMA.md`
- [ ] Verify Tier 1 items in `verify.py`
- [ ] Deck, ethics slide, limitations slide, rehearse twice timed

---

## 12. What changed from the original spec, and why

| Was | Now | Why |
|---|---|---|
| Nonprofits deliver to food-bank partner sites | Agencies with pantries, fixed or mobile | Food Bank contact: they are not the intermediary |
| Donors = SB 1383 Tier One/Two generators | Small independent restaurants | That is who has unmoved surplus |
| Mandate is the incentive | Tax deduction is the incentive | Small restaurants are exempt from SB 1383 |
| One leg, donation × nonprofit × destination | Two legs, collection + distribution | The agency both collects and distributes |
| `SERVICE_RADIUS_M` 800 m everywhere | 300 m attribution, 800 m walk-in | 800 m inverts the ranking on this grid |
| Single donation, static world | Demand budgets + ledger + splitting | 20 donations all went to one 350 lb site |
| No freshness concept | Time-to-people model | Restaurant food spoils; pantries hold it 18 h |
| `sd_foodbank_sites.csv` = destinations | = candidate **agencies** | They are partner pantries, not food banks |

`matching.py` is superseded by `collection.py` + `distribution.py` and is no
longer imported. `nonprofits.json` is unused.
