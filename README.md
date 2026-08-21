# BellyUp

Food supply coordination for San Diego nonprofits: restaurants and other food
businesses voluntarily report end-of-day surplus, and BellyUp matches each
report to the best **agency → hotspot** pair — the agency that should collect
the food, and the homeless hotspot block where it should be distributed —
using a reward-cost function over food value, block need, food-access gaps,
distance, and personnel deployment cost.

## Repo layout

```
newdata/  the datasets (see newdata/README_DATA_PROVENANCE.md for full provenance)
  hotspots.csv          382 downtown blocks with need (person-equivalents), the core dataset
  businesses.csv        31 food businesses with surplus potential (suppliers)
  agencies.csv          5 collection/redistribution agencies
  mobile_pantries.csv   15 scheduled distribution sites (context for the access-gap boost)
scripts/
  build_demo_data.py    converts the CSVs into demo/data.js; simulates tonight's reports
demo/     the demo app (static, no build step)
```

## Running the demo

```bash
python3 scripts/build_demo_data.py   # regenerate demo/data.js (already checked in)
open demo/index.html                 # or: python3 -m http.server → http://localhost:8000/demo/
```

Internet access is needed for map tiles (CARTO/OSM), Leaflet, and fonts.

Click a surplus report in the left feed (or an orange marker on the map): the
app animates a triangulation pass over 4 agencies × 161 candidate blocks, then
locks the best agency → hotspot dispatch and shows the full score breakdown —
people fed, route, deployment cost, tax-deduction estimate, and runners-up.

## The matching model

```
net = reward − cost
reward = min(meals, block need) × $/meal × accessBoost + overflow meals × $/meal × 0.5
cost   = (drive + handling minutes) × $17.75/hr + road miles × $0.76/mi
```

- Meals = lbs ÷ 1.2 (Feeding America); wage and mileage rates are the 2026 San
  Diego minimum wage and IRS standard mileage rate (see data provenance README).
- `accessBoost` weights up blocks whose scheduled food access is rare — a block
  with a monthly pantry is not a served block.
- Prepared food (hotels, venues) only matches agencies that accept prepared meals.
- Surplus **reports are simulated** (seeded in `build_demo_data.py`); hotspot
  need, businesses, and agencies are real data.
