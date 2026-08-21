# Before vs After estimate

Generated from the checked optimization outputs. Both methods use the same 14
synthetic donation reports, 161 candidate hotspots, four routable agencies,
and the same cached OSRM road matrix.

## Executive estimate

Global optimization preserves full candidate-demand coverage while reducing
the modelled allocation-route mileage by **14.53%**.
Under the sourced mileage and wage assumptions, estimated transport cost falls
from **$1,260.36** to **$1,062.66**,
an estimated saving of **$197.70 per modeled dispatch set**.

This is a planning estimate, not an operating budget quote. The model is a
continuous allocation LP and does not consolidate stops into vehicle tours.

## Metric comparison

| Metric | Before: greedy | After: global LP | Change |
|---|---:|---:|---:|
| Meals available | 1,744.17 | 1,744.17 | 0.00 |
| People fed / demand served | 938.60 | 938.60 | 0.00 |
| Demand coverage | 100.00% | 100.00% | 0.00 pp |
| Unmet demand | 0.00 | 0.00 | 0.00 |
| Food utilization | 53.81% | 53.81% | 0.00 pp |
| Hotspots served | 161 | 161 | 0 |
| Allocation-route miles | 939.96 | 803.41 | **-136.54 (14.53%)** |
| Average allocation route | 5.66 mi | 4.78 mi | -0.88 mi |
| Meal-miles | 5,087.54 | 4,383.84 | **-703.71 (13.83%)** |
| Meals per route mile | 0.999 | 1.168 | **+17.00%** |
| Summed route duration | 1,845.60 min | 1,528.09 min | **-317.51 min** |
| Transport cost proxy | $1,260.36 | $1,062.66 | **-$197.70 (15.69%)** |

## Interpretation

- People fed does not increase because the synthetic supply is about 1,744
  meals while candidate demand is only 938.6. Greedy already covers 100%.
- The LP therefore creates value through efficiency: it preserves Stage 1's
  maximum service and minimizes meal-miles in Stage 2.
- Food utilization remains 53.81% because the test scenario has substantially
  more food than candidate hotspot demand.
- `total_route_distance` sums one full Agency → Supplier → Hotspot route for
  each positive allocation arc. It is suitable for a controlled Before/After
  comparison, but it is not a consolidated multi-stop vehicle route.

## Agency workload comparison

| Agency | Baseline suppliers | Baseline hotspots | Optimized suppliers | Optimized hotspots |
|---|---:|---:|---:|---:|
| Jacobs & Cushman San Diego Food Bank | 0 | 0 | 0 | 0 |
| Feeding San Diego | 0 | 0 | 0 | 0 |
| Feeding San Diego (South Bay) | 5 | 140 | 9 | 161 |
| A.B. Jones & Co. | 0 | 0 | 0 | 0 |
| Catholic Charities Diocese of San Diego | 1 | 22 | 0 | 0 |

The LP is not instructed to balance agencies and no authoritative capacity is
available. Work may therefore concentrate at the lowest-distance eligible
agency. Capacity and utilization remain blank rather than invented.

## Estimate assumptions

- Pounds per meal: 1.2
- Vehicle cost: $0.76 per mile
- Labor: $17.75 per hour
- Road distance/time: cached OSRM/OpenStreetMap driving matrix
- Demand: main `hotspots.csv` historical demand, candidate threshold >= 1
- Donations: synthetic, reproducible Oscar UI reports tied to real businesses
- Three agency locations: explicitly marked Oscar UI demo geocodes
- Agency capacity: unavailable and not constrained
- Food value and net benefit: not estimated because no reliable production
  value is available
