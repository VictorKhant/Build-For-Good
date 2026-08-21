# Agency data — what we need from the real roster

Current `data/agencies.json` is **simulated placeholder data**. Every record in
it is invented. Replace it with a file of the same shape and nothing in the code
changes.

An **agency** is the middle layer:

```
small restaurant  --collected by-->  AGENCY PANTRY  --mobile pantry-->  hubspot
```

The restaurant pays nothing and donates for the tax deduction. The agency
absorbs both runs.

## File shape

```json
{
  "simulated": false,
  "count": 24,
  "agencies": [ { ...one object per agency... } ]
}
```

## Fields

### Required — without these an agency cannot be matched at all

| field | type | notes |
|---|---|---|
| `agency_id` | string | any stable unique id |
| `name` | string | the real organisation name |
| `lat`, `lon` | float | pantry location. If you only have an address, give `address` and we geocode it |
| `accepts` | list | any of `prepared_hot`, `prepared_cold`, `produce`, `packaged_dry`, `bakery`, `dairy` |
| `operating_windows` | list | when they can collect — see below |

### Strongly wanted — these drive the whole leg-2 story

| field | type | notes |
|---|---|---|
| `has_mobile_pantry` | bool | **the single most important field.** Only agencies with a mobile pantry can serve a hubspot. A fixed pantry cannot bring food to people sleeping on a block |
| `mobile_capacity_lbs` | float | what the mobile unit carries in one run |
| `mobile_windows` | list | when the mobile pantry actually goes out — often different days from the pantry's own hours |
| `max_hubspot_stops` | int | stops they will make in one distribution run. 2–3 is typical |

### Useful — sensible defaults are applied if missing

| field | type | default | notes |
|---|---|---|---|
| `storage` | object | `{refrigerated, frozen, hot_holding}` all false | what they can hold |
| `intake_capacity_lbs_per_day` | float | 600 | how much the pantry can take in per day |
| `has_refrigerated_vehicle` | bool | false | needed for refrigerated/frozen pickups |
| `wage_per_hour` | float | CONFIG value | loaded driver cost |
| `staff_per_run` | int | 1 | |
| `cost_per_km` | float | CONFIG value | |
| `collects_donations` | bool | true | set false for an agency that only distributes |
| `address` | string | — | geocoded via the Census API if lat/lon absent |

### Window format

```json
{"dow": 3, "start": "10:00", "end": "12:00", "weeks_of_month": [1, 3]}
```

`dow` is 0 = Monday … 6 = Sunday. `weeks_of_month` is **optional** — include it
only for a monthly cadence like "1st and 3rd Thursday". `-1` means "last". Leave
it out for a normal weekly schedule.

## What to do about unknowns

**Do not guess.** Omit the field instead — a default gets applied and the
agency is flagged `verified: false`, which shows up in `verify.py`. A guessed
operating hour produces a confidently wrong match, which is worse than a
defaulted one.

If `has_mobile_pantry` is unknown, omit it: we treat it as `false`, so the
agency can still receive donations but will not be routed to a hubspot. That is
the safe direction to be wrong in.

## Minimum viable record

```json
{
  "agency_id": "sdfb_042",
  "name": "Example Community Pantry",
  "address": "1234 Main St, San Diego, CA 92101",
  "accepts": ["produce", "packaged_dry"],
  "operating_windows": [{"dow": 2, "start": "09:00", "end": "12:00"}],
  "has_mobile_pantry": true,
  "mobile_capacity_lbs": 250,
  "mobile_windows": [{"dow": 4, "start": "07:00", "end": "12:00"}]
}
```

## How many

Coverage near the downtown grid matters more than total count. The hubspots sit
in East Village, City Center and Cortez; an agency 25 km away in Fallbrook can
receive donations but will never viably serve a hubspot. **10–15 agencies with
mobile pantries within ~10 km of downtown** would fully exercise the model.

For reference, only 4 of the 70 San Diego Food Bank partner sites in
`sd_foodbank_sites.csv` are within 5 km of downtown, and most of those are EFAP
income-tested or seniors-only.
