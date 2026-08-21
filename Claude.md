# BellyUp — Build Spec

*Food supply coordination for nonprofits serving unsheltered San Diegans.*

**Read this whole file before writing code.** Written to be implemented directly.

---

## 0. What we are building

Restaurants report end-of-day surplus. The platform matches each donation to the best **(nonprofit, destination)** pair — or rejects it with a specific reason — by maximizing need-weighted food value net of the nonprofit's real transport cost.

**The match is a triple, not a pair:** `donation × nonprofit × destination`. Many nonprofits are spread across the city; for a given restaurant, one org's HQ may be far closer than another's. Choosing the org is half the optimization.

**Ranking is not by distance.** It routes toward destinations serving blocks where unsheltered need is **highest and rising**, using the DSDP historical counts, net of what the run actually costs to operate.

**Pitch line:** *The supply is mandated, the demand is measured, and the matching layer is what's missing.*

### Parties

| Party | Role |
|---|---|
| Restaurant / donor | Reports surplus: location, quantity, food type, condition, ready time, expiry |
| Nonprofit | Owns HQ location, staff wage, vehicle. **Performs pickup and delivery.** |
| Destination | Where food is delivered: partner site, meal service, day center, or an outreach **hubspot** |
| People served | Not app users. Represented by block-level need scores from the count data |

### Why restaurants can't self-distribute — state this on stage

Food-safety regulation carries liability restaurants won't take, and dedicating a worker to non-restaurant operations is a cost they won't absorb. SB 1383 is built on the same assumption: it routes mandated donations through *recovery organizations* precisely because they carry the compliance and the staff.

⚠️ **Hubspot handling.** Hubspots are aggregated to **block level**, never precise encampment coordinates, and are visible only on the **nonprofit-facing** side. The donor-facing view confirms that a pickup was accepted — never where it is going. Keep this; it's both the safe design and the defensible one in Q&A.

---

## 1. Stack

No build step, no auth, no database.

- **Backend:** Python 3.11 + FastAPI + uvicorn
- **Data:** pandas, geopandas, shapely
- **Frontend:** one static `index.html` — vanilla JS + Leaflet (OpenStreetMap tiles, no key)
- **Storage:** in-memory + CSV/GeoJSON on disk
- **No accounts, no login.** Pre-seeded personas only.

```
bellyup/
  app.py                 # FastAPI routes only
  economics.py           # cost + benefit model   ← core IP
  matching.py            # feasibility + ranking  ← core IP
  needs.py               # block need + forecast from provided data
  seed.py                # builds nonprofits / destinations / donors
  data/
    BlockLevel_Counts_Panel261.csv
    Downtown_BlockGrid.geojson
    DowntownCounts_Monthly.csv
    sd_foodbank_sites.csv
    nonprofits.json          # generated / hand-curated
    destinations.json        # generated / hand-curated
  static/index.html
  README.md
```

---

## 2. Data models

```python
# donation
{
  "donor_name": str, "lat": float, "lon": float, "address": str,
  "food_type": "prepared_hot"|"prepared_cold"|"produce"|"packaged_dry"|"bakery"|"dairy",
  "quantity_lbs": float,
  "condition": "hot"|"refrigerated"|"frozen"|"ambient",
  "ready_at": iso8601, "expires_at": iso8601,
  "sb1383_tier": 1|2|None
}

# nonprofit
{
  "org_id": str, "name": str,
  "hq_lat": float, "hq_lon": float,
  "wage_per_hour": float,       # loaded driver cost
  "staff_per_run": int,         # default 1
  "cost_per_km": float,         # fuel + wear
  "has_refrigerated_vehicle": bool,
  "operating_windows": [{"dow": 0-6, "start": "08:00", "end": "17:00"}]
}

# destination
{
  "dest_id": str, "name": str, "lat": float, "lon": float,
  "dest_type": "food_bank_partner"|"meal_service"|"day_center"|"hubspot",
  "accepts": ["produce", "packaged_dry", ...],
  "storage": {"refrigerated": bool, "frozen": bool, "hot_holding": bool},
  "open_windows": [{"dow": 0-6, "start": "09:00", "end": "11:00"}],
  "capacity_lbs_per_visit": float,
  "served_block_ids": [str],
  "need_now": float, "need_trend": float
}

# match result  (one per feasible triple)
{
  "org_id": str, "org_name": str,
  "dest_id": str, "dest_name": str,
  "route_km": {"to_pickup": float, "to_dropoff": float, "return": float, "total": float},
  "total_min": float, "arrival_at": iso8601,
  "meals": float,
  "food_value": float,
  "fuel_cost": float, "personnel_cost": float, "transport_cost": float,
  "need_multiplier": float,
  "net_value": float,
  "cost_per_meal": float,
  "explanation": str
}

# rejection
{"org_id": str, "dest_id": str, "reason_code": str, "reason": str}
```

---

## 3. `needs.py` — need from the historical record

**This is what makes it data-driven and forward-looking. Do not skip it.**

### 3.1 Block need

1. Load `Downtown_BlockGrid.geojson` (382 polygons, EPSG:4326) and `BlockLevel_Counts_Panel261.csv`.
   **Use Panel261, not the full file** — the full footprint grows by 121 blocks in 2022 and isn't comparable over time.
2. Per block, series over the 12 count dates = `individuals + tents_structures + vehicles` (raw observed basis).
3. `need_now(block)` = latest count date value.
4. `need_trend(block)` = OLS slope over the last 5 dates, persons/year.

### 3.2 Area forecast

From `DowntownCounts_Monthly.csv`:

- Filter `area_type == "neighborhood"`, `component == "total"`.
- **Exclude `Outside Perimeter` before 2021-04** — null-not-zero; joining it mid-series fabricates a jump.
- Fit linear trend + month-of-year seasonality, plus a boolean regressor for `fellowship_month` (extra volunteers inflate counts; drops from 10 months/yr in 2017 to zero after 2020, confounding any long trend).
- Forecast 12 months per area. `forecast_delta(area)` = projected month 12 − latest actual.

### 3.3 Destination need

```python
SERVICE_RADIUS_M = 800          # ~½ mile / 10-min walk

served = blocks within SERVICE_RADIUS_M of the destination
need_now(dest)   = sum(need_now(b) for b in served)
need_trend(dest) = sum(need_trend(b) for b in served) + forecast_delta(area of dest)
```

Destinations outside the block grid get `need_now = 0` and rank last — correct, they don't serve downtown.

### 3.4 Data rules that must not be broken

1. **Never sum `total` with `individual`/`tent`/`vehicle`.** `total` is already adjusted (`≈ individual + 1.75×tent + 2.03×vehicle`). Summing double-counts.
2. **Use `Panel261`** for anything across time.
3. **Join on `area`**, not raw labels — naive East Village joins undercount 60–75%.
4. `Outside Perimeter` is **null before April 2021**, not zero.
5. **2025 is missing Jul, Aug, Oct, Nov.** Do not interpolate silently.
6. Use **polygons, not centroids**, for point-in-block joins.
7. Exclude **PRE2017** rows from component work — they carry `total` only.

---

## 4. `economics.py` — the cost/benefit model

Every constant lives in one `CONFIG` dict so it's tunable live. **Each one needs a cited source before the demo** — a judge will ask where the numbers came from, and "we picked it" is a bad answer.

```python
CONFIG = {
  # --- routing ---
  "AVG_SPEED_KMH": 25,
  "ROAD_FACTOR": 1.35,            # haversine -> road distance
  "LOAD_MIN": 15,
  "UNLOAD_MIN": 10,
  "INCLUDE_RETURN_LEG": True,     # crew returns to HQ

  # --- cost  [CITE: IRS standard mileage rate; SD nonprofit driver wage] ---
  "COST_PER_KM": 0.43,            # fuel + wear
  "WAGE_PER_HOUR": 22.00,         # loaded

  # --- benefit  [CITE: Feeding America lbs-per-meal; USDA meal cost] ---
  "LBS_PER_MEAL": 1.2,
  "VALUE_PER_MEAL": 3.50,

  # --- need weighting ---
  "ALPHA_NEED": 0.6,              # weight on current need
  "BETA_TREND": 0.4,              # weight on rising need  <- the forward-looking term

  # --- viability guards ---
  "MAX_COST_PER_MEAL": 2.00,
  "MIN_QUANTITY_LBS": 10.0,

  # --- safety ---
  "MAX_TRANSIT_MIN": {"hot": 120, "refrigerated": 240, "frozen": 240, "ambient": 480},
  "SAFETY_MARGIN_MIN": 30,
}
```

### 4.1 Route — three legs, per (nonprofit, destination) pair

```python
leg_to_pickup  = haversine(org.hq, donor)        * ROAD_FACTOR
leg_to_dropoff = haversine(donor, destination)   * ROAD_FACTOR
leg_return     = haversine(destination, org.hq)  * ROAD_FACTOR if INCLUDE_RETURN_LEG else 0

route_km   = leg_to_pickup + leg_to_dropoff + leg_return
drive_min  = route_km / AVG_SPEED_KMH * 60
total_min  = drive_min + LOAD_MIN + UNLOAD_MIN
arrival_at = max(ready_at, now) + (leg_to_pickup+leg_to_dropoff)/AVG_SPEED_KMH*60 + LOAD_MIN
```

No routing API. Offline, deterministic, no key, no rate limit, nothing to fail on stage.

### 4.2 Cost

```python
fuel_cost      = route_km * COST_PER_KM
personnel_cost = (total_min / 60) * WAGE_PER_HOUR * org.staff_per_run
transport_cost = fuel_cost + personnel_cost
```

### 4.3 Benefit

```python
meals      = quantity_lbs / LBS_PER_MEAL
food_value = meals * VALUE_PER_MEAL

need_multiplier = 1 + ALPHA_NEED * norm(dest.need_now) + BETA_TREND * norm(dest.need_trend)
# norm() = min-max across all candidate destinations for this donation

weighted_value = food_value * need_multiplier
net_value      = weighted_value - transport_cost
cost_per_meal  = transport_cost / meals
```

**`need_multiplier` is the merge.** Pure cost minimization sends food wherever is cheapest to reach — systematically the destinations nearest nonprofit HQs, regardless of who needs it. The multiplier makes the objective *cost-effectiveness* rather than cost, and `BETA_TREND` is where the forecast enters. Two destinations equidistant, one with 40 people and rising, one with 8 and falling: raw cost is indifferent, this is not.

**Say this in the demo.** It is the analytical argument of the whole project.

---

## 5. `matching.py` — constraints and ranking

For each donation, evaluate **every (nonprofit × destination) pair**. With ~8 orgs and ~20 destinations that's 160 combinations — brute force, no optimization needed.

### 5.1 Hard constraints, in order. First failure rejects the pair.

| # | Constraint | `reason_code` | Message |
|---|---|---|---|
| 1 | `quantity_lbs >= MIN_QUANTITY_LBS` | `QTY_TOO_SMALL` | "Below the 10 lb minimum for a dedicated run." |
| 2 | `food_type in dest.accepts` | `TYPE_NOT_ACCEPTED` | "{Dest} does not accept {food_type}." |
| 3 | Storage matches condition | `NO_STORAGE` | "{Dest} has no {condition} storage." |
| 4 | Refrigerated/frozen needs `org.has_refrigerated_vehicle` | `NO_COLD_VEHICLE` | "{Org} has no refrigerated vehicle." |
| 5 | `total_min <= MAX_TRANSIT_MIN[condition]` | `COLD_CHAIN` | "{n} min run exceeds the {limit} min safe window for {condition} food." |
| 6 | `arrival_at + SAFETY_MARGIN_MIN <= expires_at` | `EXPIRES_IN_TRANSIT` | "Arrives {n} min before expiry — under the 30 min margin." |
| 7 | `arrival_at` inside a `dest.open_window` | `DEST_CLOSED` | "{Dest} is closed at the {time} arrival. Next open: {next}." |
| 8 | Run falls inside `org.operating_windows` | `ORG_CLOSED` | "{Org} is not operating at {time}." |
| 9 | `quantity_lbs <= dest.capacity_lbs_per_visit` | `OVER_CAPACITY` | "Exceeds {Dest}'s {n} lb capacity for this window." |
| 10 | `net_value > 0` | `NET_NEGATIVE` | "Run costs ${cost} to deliver ${value} of food — not viable." |
| 11 | `cost_per_meal <= MAX_COST_PER_MEAL` | `INEFFICIENT` | "${x}/meal exceeds the ${limit} ceiling." |

**Constraints 10 and 11 are two independent guards.** Net value catches "not worth the trip." Cost-per-meal catches the run that's absurdly inefficient but scrapes positive because the donation is large. Both are needed.

**Always return every rejected pair with its reason.** Never drop silently. "160 combinations evaluated, 12 feasible, here's why the rest failed" is what makes the engine look intelligent rather than look like a distance sort.

### 5.2 Ranking

Sort feasible matches by `net_value` descending. Show `cost_per_meal` alongside — nonprofits think in that metric and it lands with anyone who's run an operation.

### 5.3 Explanation string

```
"Father Joe's Villages picks up from Nolita Hall — 3.1 km out, 1.4 km to drop.
 42 min, $18.40 in fuel and staff time. 58 meals delivered at $0.32/meal.
 Destination serves 14 blocks with 47 people and need rising 12% over the last
 three counts. Chosen over Site X, which is 0.6 km closer but serves blocks
 with falling need."
```

---

## 6. `seed.py`

1. **Nonprofits** — hand-curate 6–10 real San Diego orgs with HQ coordinates. Father Joe's Villages, San Diego Food Bank, Feeding San Diego, Salvation Army, St. Vincent de Paul. Geocode HQs via the **Census Geocoder** (`geocoding.geo.census.gov`) — free, no key, federal. Wage and vehicle cost from CONFIG unless you have better.
2. **Destinations** — parse `sd_foodbank_sites.csv` (72 sites, already scraped: name, address, days, type, free-text hours). Then hand-add downtown meal services and day centers — **none of the 72 are in 92101**, so these are the ones that actually serve downtown. Neil Good Day Center, Father Joe's, God's Extended Hand, Ladle Fellowship. **Verify each is currently operating and get real hours.**
3. **Hubspots** — top-N blocks by `need_now` from `needs.py`, block centroid as the delivery point, labeled by cross-streets from `Downtown_BlockGrid.csv`.
4. **Donors** — downtown restaurants via `sb1383_donors.py`, bbox narrowed to the grid: **lon −117.171…−117.134, lat 32.695…32.724**.

**Seed quality beats seed volume.** 15 correct downtown destinations with real hours demos better than 72 with guessed ones.

---

## 7. API

```
GET  /api/nonprofits
GET  /api/destinations          -> incl. need_now, need_trend
GET  /api/blocks                -> GeoJSON + need per block
GET  /api/forecast              -> monthly actual + 12-month projection
POST /api/match                 -> {donation} -> {matches:[...], rejections:[...]}
GET  /api/config
POST /api/config                -> live weight/parameter tuning
```

---

## 8. UI — one page, three panes

**Left — donor form.** Address or map click, food type, quantity, condition, ready time, expiry. "Load example" button with the three scenarios.

**Center — Leaflet map.** Block polygons shaded by `need_now` (sequential ramp, legend, colour-blind safe). Donor pin. The winning three-leg route drawn HQ → pickup → dropoff. Feasible destinations green, rejected grey.

**Right — results.** Ranked matches, each showing net value, cost per meal, and the cost breakdown (fuel vs. personnel). Below, collapsed "N rejected" with reasons grouped by code. Parameter sliders at the bottom if time allows.

One screen. No routing, no tabs, no modals.

---

## 9. Demo scenarios — seed and rehearse these

1. **The good match.** 60 lb packaged dry goods, ambient, expires in 5 days, donor in Gaslamp. Routes to a downtown meal service — and the winning nonprofit is *not* the one with the nearest HQ, because the total three-leg route is shorter overall.
2. **The rejection.** 12 lb hot prepared food, expires in 90 minutes, nearest accepting destination 40 min away and closed. Fires `COLD_CHAIN`, `DEST_CLOSED`, and `NET_NEGATIVE` at once. **This is the slide people remember.**
3. **The thesis.** Two destinations nearly equidistant; the app picks the farther one because its blocks carry higher and rising need. Drag `BETA_TREND` to zero and watch the answer flip to the cheaper, lower-need site. That single interaction is the argument.

---

## 10. Build order — freeze is noon Friday

**Tonight**
- [ ] `needs.py`: block need + destination need scores printing to console
- [ ] Baseline forecast (constant footprint, fellowship regressor)
- [ ] `economics.py` + `matching.py`: all 11 constraints and the net-value model, tested from a plain script — **no UI yet**
- [ ] `seed.py`: 6–10 nonprofits, 15–20 downtown destinations, real hours

**Friday 09:00–12:00**
- [ ] FastAPI wiring
- [ ] Leaflet map, block shading, three-leg route drawing
- [ ] Form → results incl. rejection list
- [ ] Three scenarios end to end

**Friday 12:00–14:00 — FREEZE**
- [ ] Deck, ethics slide, limitations slide
- [ ] Rehearse twice, out loud, timed

**Cut order:** sliders → route drawing → block shading → forecast (fall back to trend only, **never to nothing** — forward-looking is required by the prompt) → extra scenarios.

---

## 11. What to say on stage

**The finding.** Downtown counts, constant footprint: 1,289 average in the 12 months to Jul 2023, 970 in the 12 after — **−24.8%**. The Unsafe Camping Ordinance took effect 31 Jul 2023; the break lands three to four months later. A falling street count is what housing people looks like *and* what moving people looks like. Present both; don't editorialise.

**The waste.** The US discards 30–40% of its food supply *(USDA — have the citation ready)*. Meanwhile people two blocks from full kitchens go hungry.

**The mandate.** SB 1383 *legally requires* large food businesses to donate surplus edible food. Tier One since Jan 2022 — supermarkets over $2M, grocery over 10,000 sq ft, distributors. Tier Two since Jan 2024 — **hotels with 200+ rooms and on-site food**, restaurants with 250+ seats or 5,000 sq ft, venues over 2,000/day. Downtown's Gaslamp and Convention Center district is dense with Tier Two generators blocks from the highest-need cells.

**The gap.** Supply is mandated. Demand is measured. The matching layer is missing — and matching is hard because transport cost is real and nonprofits are scattered. That's the product.

**Ethics — unprompted.**
- Routes to organizations and block-level outreach points; never publishes where individuals sleep.
- Hubspots are nonprofit-facing only. Donors see that a pickup was accepted, not where it goes.
- Block aggregation was chosen deliberately so the output can't be operationalised for enforcement.
- A 24.8% drop is roughly 300 people whose location changed. Say it in human terms once.

**Limitations — volunteer them.**
- Monthly visual street sweep: a known undercount measuring visibility as much as prevalence.
- Straight-line × road factor, not routed. Fine for triage, not dispatch.
- Cost constants are estimates — cite them, and say they're tunable per organization.
- SB 1383 tiers are OSM approximations: no sales figures, floor areas, or seat counts.
- The app proposes matches. It does not create the written agreement or food-safety compliance SB 1383 requires.

**"Who uses this Monday?"** Name the organization and the decision it changes.

---

## 12. Definition of done

- [ ] Posting a donation returns ranked matches **and** rejections with reasons
- [ ] Matching evaluates (nonprofit × destination) pairs, not destinations alone
- [ ] Cost model includes all three legs and both fuel and personnel
- [ ] Ranking demonstrably uses need + trend, not just cost — scenario 3 proves it
- [ ] Need scores derive from `Panel261` + block grid with §3.4 respected
- [ ] A forecast exists and is visible
- [ ] All three scenarios run end to end with no code change
- [ ] Every CONFIG constant has a cited source
- [ ] Ethics and limitations slides written
