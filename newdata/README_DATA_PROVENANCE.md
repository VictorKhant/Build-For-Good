# The four datasets, and where each one came from

Connection model: **business → agency → hotspot.** No routing, no siting.
The agency is the actor; the data tells it who to collect from and where to go.

| File | Rows | What it answers | Origin |
|---|---|---|---|
| `hotspots.csv` | 382 blocks × 35 cols | Where unsheltered people are, **and what's already near them** | **EVENT DATA + EXTERNAL merged** |
| `businesses.csv` | 31 | Who has surplus food | External |
| `agencies.csv` | 5 | Who collects and redistributes | External |
| `mobile_pantries.csv` | 0 — **needs pull** | Where agencies already distribute | External |

---

## EVENT-PROVIDED DATA (organizer bundle) — unmodified

We did not alter a single count. These files are read, never written.

| File | Rows | Used for |
|---|---|---|
| `Downtown_BlockGrid.geojson` / `.csv` | 382 | The geographic spine — every other layer joins to this |
| `BlockLevel_Counts_Panel261.csv` | 3,132 | Longitudinal need (balanced 261-block panel, 12 dates) |
| `BlockLevel_Counts.csv` | 3,737 | Latest snapshot across all 382 blocks |
| `Methodology_Periods.csv` | 4 | Tent/vehicle multipliers by date |
| `DowntownCounts_Monthly.csv` | 2,880 | Monthly neighbourhood series (validation + trend) |
| `Area_Crosswalk.csv` | 24 | Canonical area names |

**The 18 need columns in `hotspots.csv` are derived entirely from these.**
Nothing external contributes to the need figures — external data adds 17
*separate* columns alongside them, never mixed in.
`COLUMN_PROVENANCE.csv` marks every column EVENT or EXTERNAL, one row per
column. That file is the direct answer to "what did you merge?". `need` = mean person-equivalents per block using the
publisher's own multiplier schedule (individuals + tent_mult × tents +
vehicle_mult × vehicles) applied at the multiplier in force on each count date.

---

## EXTERNAL DATA WE ADDED

| Layer | Source | Why |
|---|---|---|
| SB 1383 generator thresholds | [CalRecycle](https://www2.calrecycle.ca.gov/Docs/Web/118917) | Defines which businesses are legally obligated to donate |
| Business sizes (rooms / floor area) | Cvent venue listings, store-format pages | To test against those thresholds |
| Food recovery organisations | [City of San Diego Edible Food Recovery Directory](https://www.sandiego.gov/sites/default/files/2024-03/food-recovery-organization-list.pdf) | Official list of orgs set up to receive donations |
| Operator programme detail | Provider sites (SD Food Bank, Feeding San Diego) | Which operators run mobile distribution |
| Vehicle cost $0.76/mi | [IRS, eff. 2026-07-01](https://www.journalofaccountancy.com/news/2026/jul/irs-raises-standard-mileage-rates-for-remainder-of-2026/) | Collection cost |
| Labor $17.75/hr | [City of San Diego minimum wage, eff. 2026-01-01](https://www.insidesandiego.org/hourly-minimum-wage-san-diego-will-increase-1775-effective-jan-1-2026-0) | Collection cost |
| Meal conversion 1.2 lb | [Feeding America](https://www.hacap.org/download_file/view/115/244) | Pounds → meals |

**Still to pull** (blocked from our environment, see `PULL_MOBILE_PANTRY_DATA.md`):
restaurant locations (OpenStreetMap / City business certificates) and mobile
pantry sites with schedules (San Diego Food Bank GPS Food Locator).

---

## How the merge works

**One join key: `block_id`.** External point layers are spatially joined to the
event-provided block polygons — never averaged into the counts.

```
EVENT:     Downtown_BlockGrid (382 polygons)
              |
              +-- BlockLevel counts  --> need per block   [hotspots.csv]
              |
EXTERNAL:     +-- businesses (point-in-polygon + radius)
              +-- agencies   (point-in-polygon + radius)
              +-- mobile pantries (point + schedule)
```

### What we deliberately did NOT merge

**San Diego Hunger Coalition district dashboards.** Reported at Council
District level; downtown sits almost entirely in District 3. Joining it would
give every one of the 382 blocks an identical value — no discriminating power,
and it would treat a district average as a property of a street corner. Kept
as narrative context only.

**311 reports and fire-incident data.** These measure complaints and dispatch
behaviour, not people. Blending them into a physical street count would
corrupt a good measurement with a biased one. Available as independent
validation signals if wanted.

---

## Scope

San Diego only. One out-of-city record (the Food Bank's Vista/North County
branch) was found and removed during audit.

---

## Field notes for `hotspots.csv`

| Field | Meaning |
|---|---|
| `need` | Mean person-equivalents across observed count dates |
| `need_rank` | 1 = highest need |
| `priority` | high (>15) / medium (5–15) / low (0.5–5) / none |
| `persistence` | Share of count dates the block was non-zero. **Read alongside `need`** — a block non-zero on all 12 dates is a different case from one that spiked once |
| `longitudinal_data` | False for the 121 blocks added in 2022 (Barrio Logan, Golden Hill, Sherman Heights) — those carry `latest_*` only |

## Caveats

- Person-equivalents use DSDP's adjusted basis. **Not comparable to RTFH or
  PIT figures from 2020 onward** — RTFH dropped the multipliers in Jan 2020,
  DSDP kept them.
- Business coordinates are hand-placed to ~±150 m. Not survey grade.
- SB 1383 tier flags mean a business *meets the published size threshold*.
  **Not a regulatory list, and it says nothing about compliance.**
- Block counts are a monthly visual street count and undercount.

Validation: `python3 src/analyze/validate_data.py` — 14 checks, 0 failures.


---

## Column-level provenance — `hotspots.csv` (35 columns)

`COLUMN_PROVENANCE.csv` has this machine-readable.

### 18 columns — EVENT DATA
`block_id`, `location`, `area`, `lon`, `lat`, `need`, `need_rank`, `priority`,
`persistence`, `months_nonzero`, `months_observed`, `avg_persons`,
`peak_persons`, `latest_persons`, `latest_individuals`, `latest_tents`,
`latest_vehicles`, `longitudinal_data`

Source: organizer bundle only.

### 17 columns — EXTERNAL, merged onto the event block grid

| Columns | External source |
|---|---|
| `business_in_block`, `nearest_business_m`, `business_within_200m/400m/800m` | SB 1383 inventory — CalRecycle thresholds + venue listings |
| `restaurant_in_block`, `nearest_restaurant_m`, `restaurant_within_200m/400m/800m` | OpenStreetMap / City business certificates (pending pull) |
| `foodsite_in_block`, `nearest_foodsite_m`, `foodsite_within_200m/400m/800m` | City of San Diego Edible Food Recovery Directory + provider sites |
| `unserved_need`, `supply_within_400m` | Derived: event need × external coverage |

**The separation is the point.** Need is never adjusted by external data. The
external columns sit beside it, so any figure can be traced to one side or the
other and the merge can be undone by dropping columns.

`unserved_need` is the one derived column that combines both: a block's event
need, returned only when no external food site sits within 400 m. Rank 5
(Park Blvd & J, 23.2 persons, nearest food site 509 m) carries its full need
into `unserved_need`; ranks 1–4 have food service within range and return 0.

---

## `mobile_pantries.csv` — 15 scheduled food distributions

**Sources (both primary, both current):**

1. [San Diego Food Bank — Live EFAP Distribution Roster, 18 Mar 2026](https://www.sandiegofoodbank.org/wp-content/uploads/2026/03/Live-EFAP-Roster-3.18.26.pdf)
   — the Food Bank's own operational roster, with day, time and closure dates
2. [San Diego City College — Community Food and Meal Distribution List, Central](https://www.sdcity.edu/community/docs/food_distributions.pdf)
   — consolidated central-region list including hot meal services

13 of 15 sites are downtown-relevant (92101, 92102, 92113, 92103).

**Naming honesty:** most of these are *fixed sites running on a schedule*
rather than trucks that move. `site_type` distinguishes
`fixed_site_daily` / `fixed_site_weekly` / `fixed_site_scheduled`. The
operationally important variable is not whether the site moves — it is **how
many days a week food is actually available there.**

### The finding this produced

Counting distribution points on a map says downtown is reasonably covered.
Counting *days* says otherwise.

| Coverage standard (400 m walk) | Need covered | Unserved |
|---|---|---|
| Any scheduled distribution nearby | 563 (57.1%) | 424 |
| A site open **5+ days a week** nearby | 319 (**32.4%**) | 667 |

**85 blocks holding 243 person-equivalents have food access only monthly or
less.** Rank 2 in the whole dataset — 9th & E, 29.8 persons, non-zero on all
12 count dates — has a distribution 279 m away that runs on the **third Friday
of the month**.

Measured coverage nearly halves once schedule is accounted for. Eight of the
fifteen sites run monthly or less. Any analysis that treats a monthly pantry
as equivalent to a daily meal service overstates access by roughly 2×.

`hotspots.csv` therefore carries both figures — `unserved_need_any` and
`unserved_need_daily` — so the standard being applied is always explicit.

### New columns in `hotspots.csv` (all EXTERNAL)

`pantry_within_400m`, `nearest_pantry_m`, `pantry_days_per_week`,
`pantry_daily_within_400m`, `food_access_days_per_week`,
`unserved_need_any`, `unserved_need_daily`

### Caveats

- Coordinates are hand-placed to ~±150 m. One site (Chollas View) not
  geocoded and excluded from the spatial join.
- `days_per_week` converts monthly schedules at 1/4.33 (a 1st-Thursday site
  scores 0.23). That is a modelling choice, stated.
- Published closure dates are recorded in the source but not modelled.
- Rosters change. Both sources are dated; re-pull before operational use.
