#!/usr/bin/env python3
"""Validate baseline and optimized allocations against supply, demand, and routes."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA = PROJECT_ROOT / "calc" / "optimization_data"
OUTPUT = PROJECT_ROOT / "optim" / "output"
ROUTES = PROJECT_ROOT / "calc" / "route_cache" / "route_matrix.csv"
TOLERANCE = 1e-5


def main() -> None:
    donations = pd.read_csv(DATA / "demo_donation_reports.csv")
    donations["available_meals"] = donations.quantity_lbs / 1.2
    supply = donations.set_index("donation_id").available_meals
    hotspots = pd.read_csv(DATA / "hotspots.csv").set_index("block_id")
    routes = pd.read_csv(ROUTES)
    route_keys = set(
        map(
            tuple,
            routes.loc[
                routes.route_available.fillna(False) & routes.food_compatible.fillna(False),
                ["donation_id", "agency_id", "hotspot_block_id"],
            ].to_numpy(),
        )
    )
    errors = []

    for name in ["baseline", "optimized"]:
        allocations = pd.read_csv(OUTPUT / f"{name}_allocations.csv")
        if allocations.meals_allocated.lt(-TOLERANCE).any():
            errors.append(f"{name}: negative allocations")
        by_donation = allocations.groupby("donation_id").meals_allocated.sum()
        excess_supply = by_donation - by_donation.index.map(supply)
        if excess_supply.gt(TOLERANCE).any():
            errors.append(f"{name}: donation supply exceeded")
        by_hotspot = allocations.groupby("hotspot_block_id").meals_allocated.sum()
        demand = hotspots.loc[by_hotspot.index, "historical_demand"]
        if (by_hotspot - demand).gt(TOLERANCE).any():
            errors.append(f"{name}: hotspot demand exceeded")
        used_keys = set(
            map(
                tuple,
                allocations[["donation_id", "agency_id", "hotspot_block_id"]].to_numpy(),
            )
        )
        if not used_keys.issubset(route_keys):
            errors.append(f"{name}: allocation uses unavailable/incompatible route")
        print(
            f"{name}: allocations={len(allocations)}, meals={allocations.meals_allocated.sum():.2f}, "
            f"donations={allocations.donation_id.nunique()}, hotspots={allocations.hotspot_block_id.nunique()}"
        )

    print(f"output validation: errors={len(errors)}")
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
