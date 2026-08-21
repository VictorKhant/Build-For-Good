#!/usr/bin/env python3
"""Choose the lower-mile allocation seed for sequential route improvement."""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from calc.database import read_table, write_table


def main() -> None:
    candidates = {}
    for method in ("greedy", "optimized"):
        routes = read_table(f"simulation_{method}_vehicle_routes")
        candidates[method] = float(routes.distance_miles.sum())
    seed = min(candidates, key=candidates.get)
    allocation_table = (
        "simulation_baseline_allocations" if seed == "greedy"
        else "simulation_optimized_allocations"
    )
    allocations = read_table(allocation_table)
    write_table("simulation_route_optimized_allocations", allocations)
    print(
        f"route optimization seed={seed}, seed miles={candidates[seed]:.2f}, "
        f"allocations={len(allocations)}"
    )


if __name__ == "__main__":
    main()
