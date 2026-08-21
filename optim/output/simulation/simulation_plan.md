# Capacity-constrained simulation plan

This scenario uses the supplier quantities and synthetic daily agency
capacities in `calc/simulation_data/`. Capacity is enforced in both the greedy
baseline and the two-stage LP.

## Service result

- Available supply: **1744.17 meals**
- Candidate demand: **938.60 meals**
- People/demand served: **938.60**
- Coverage: **100.00%**
- Unmet demand: **0.00**

## Optimized agency plan

| Agency | Capacity meals/day | Assigned meals | Utilization | Suppliers | Hotspots |
|---|---:|---:|---:|---:|---:|
| Jacobs & Cushman San Diego Food Bank | 650 | 0.00 | 0.00% | 0 | 0 |
| Feeding San Diego | 750 | 138.60 | 18.48% | 4 | 37 |
| Feeding San Diego (South Bay) | 500 | 500.00 | 100.00% | 4 | 72 |
| A.B. Jones & Co. | 350 | 0.00 | 0.00% | 0 | 0 |
| Catholic Charities Diocese of San Diego | 300 | 300.00 | 100.00% | 3 | 57 |

## Agency-to-supplier plan

| Agency | Supplier | Meals | Hotspots |
|---|---|---:|---:|
| Catholic Charities Diocese of San Diego | Ralphs - Hillcrest | 146.88 | 23 |
| Catholic Charities Diocese of San Diego | Scripps Mercy Hospital San Diego | 107.50 | 26 |
| Catholic Charities Diocese of San Diego | Smart & Final | 45.62 | 9 |
| Feeding San Diego | Courtyard by Marriott San Diego Downtown | 40.83 | 11 |
| Feeding San Diego | Ralphs - Hillcrest | 48.47 | 12 |
| Feeding San Diego | The US Grant, a Luxury Collection Hotel | 40.83 | 10 |
| Feeding San Diego | The Westin San Diego Bayview | 8.46 | 4 |
| Feeding San Diego (South Bay) | Albertsons - 14th & Market | 221.67 | 25 |
| Feeding San Diego (South Bay) | Hard Rock Hotel San Diego | 50.83 | 17 |
| Feeding San Diego (South Bay) | Northgate Gonzalez Market | 58.12 | 10 |
| Feeding San Diego (South Bay) | Smart & Final | 169.38 | 22 |

## Greedy vs global LP under capacity

| Metric | Greedy | Global LP | Change |
|---|---:|---:|---:|
| People served | 938.60 | 938.60 | 0.00 |
| Allocation-arc miles | 1345.54 | 1384.53 | +2.90% |
| Meal-miles | 7581.69 | 6810.65 | -10.17% |

The LP objective is meal-miles, so it reduces distance weighted by the number
of meals. The unweighted sum of full route miles across positive allocation
arcs can rise when the LP splits flow across more arcs. Neither value is a
consolidated multi-stop vehicle tour; vehicle routing remains future work.

All agency capacities are synthetic planning assumptions, not confirmed
operational capacities.
