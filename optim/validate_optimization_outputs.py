#!/usr/bin/env python3
"""Validate baseline and optimized allocations against supply, demand, and routes."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA = PROJECT_ROOT / "calc" / "optimization_data"
OUTPUT = PROJECT_ROOT / "optim" / "output"
ROUTES = PROJECT_ROOT / "calc" / "route_cache" / "route_matrix.csv"
TOLERANCE = 1e-5


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulation", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = arguments()
    output = OUTPUT / "simulation" if args.simulation else OUTPUT
    donations = pd.read_csv(DATA / "demo_donation_reports.csv")
    if args.simulation:
        simulation_supply = pd.read_csv(
            PROJECT_ROOT / "calc" / "simulation_data" / "supplier_supply.csv"
        ).set_index("donation_id")
        donations["quantity_lbs"] = donations.donation_id.map(
            simulation_supply.available_food_lbs
        )
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
    capacities = None
    if args.simulation:
        capacities = pd.read_csv(
            PROJECT_ROOT / "calc" / "simulation_data" / "agency_capacity.csv"
        ).set_index("agency_id").capacity_meals_per_day

    for name in ["baseline", "optimized"]:
        allocations = pd.read_csv(output / f"{name}_allocations.csv")
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
        if capacities is not None:
            by_agency = allocations.groupby("agency_id").meals_allocated.sum()
            agency_capacity = capacities.reindex(by_agency.index)
            if agency_capacity.isna().any():
                errors.append(f"{name}: allocation references agency without capacity")
            elif (by_agency - agency_capacity).gt(TOLERANCE).any():
                errors.append(f"{name}: agency capacity exceeded")
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
