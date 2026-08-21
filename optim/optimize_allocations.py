#!/usr/bin/env python3
"""Compare deterministic greedy dispatch with a two-stage global LP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from scipy.sparse import coo_matrix, vstack


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from calc.database import read_table, write_table
DATA = PROJECT_ROOT / "calc" / "optimization_data"
ROUTES = PROJECT_ROOT / "calc" / "route_cache" / "route_matrix.csv"
OUTPUT_BASE = PROJECT_ROOT / "optim" / "output"
SIMULATION_DATA = PROJECT_ROOT / "calc" / "simulation_data"
LBS_PER_MEAL = 1.2
COST_PER_MILE = 0.76
WAGE_PER_HOUR = 17.75
ALLOCATION_EPS = 1e-7


ALLOCATION_COLUMNS = [
    "donation_id", "business_id", "agency_id", "hotspot_block_id", "meals_allocated",
    "agency_to_supplier_miles", "supplier_to_hotspot_miles", "route_total_miles",
    "route_duration_minutes", "transport_cost", "distance_method",
]


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--simulation", action="store_true",
        help="Use calc/simulation_data supplier quantities and agency capacities.",
    )
    return parser.parse_args()


def load_inputs(simulation: bool = False) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    donations = read_table("donation_reports")
    agencies = read_table("agencies")
    if simulation:
        supply = read_table("supplier_supply")
        capacity = read_table("agency_capacity")
        supply_by_donation = supply.set_index("donation_id")
        missing_supply = set(donations.donation_id) - set(supply_by_donation.index)
        if missing_supply:
            raise ValueError(f"simulation supply is missing donations: {sorted(missing_supply)}")
        donations["quantity_lbs"] = donations.donation_id.map(
            supply_by_donation.available_food_lbs
        )
        agencies = agencies.drop(columns=["capacity_meals"], errors="ignore").merge(
            capacity[["agency_id", "capacity_meals_per_day"]],
            on="agency_id", how="left", validate="one_to_one",
        ).rename(columns={"capacity_meals_per_day": "capacity_meals"})
    donations = donations[donations.status.eq("active")].copy()
    donations["available_meals"] = donations.quantity_lbs / LBS_PER_MEAL
    donations["window_minutes"] = (
        pd.to_datetime(donations.expires_at, utc=True)
        - pd.to_datetime(donations.ready_at, utc=True)
    ).dt.total_seconds() / 60
    hotspots = read_table("hotspots")
    hotspots = hotspots[hotspots.historical_demand.ge(1)].copy()
    routes = read_table("route_matrix")
    routes = routes[
        routes.route_available.fillna(0).astype(bool)
        & routes.food_compatible.fillna(0).astype(bool)
    ].copy()
    routes = routes.merge(
        donations[["donation_id", "available_meals", "window_minutes"]], on="donation_id"
    )
    routes = routes[routes.route_duration_minutes.le(routes.window_minutes)]
    return donations, hotspots, agencies, routes


def allocation_record(route: pd.Series, meals: float) -> dict:
    cost = route.route_total_miles * COST_PER_MILE + route.route_duration_minutes / 60 * WAGE_PER_HOUR
    return {
        "donation_id": route.donation_id,
        "business_id": route.business_id,
        "agency_id": route.agency_id,
        "hotspot_block_id": route.hotspot_block_id,
        "meals_allocated": meals,
        "agency_to_supplier_miles": route.agency_to_supplier_miles,
        "supplier_to_hotspot_miles": route.supplier_to_hotspot_miles,
        "route_total_miles": route.route_total_miles,
        "route_duration_minutes": route.route_duration_minutes,
        "transport_cost": cost,
        "distance_method": route.distance_method,
    }


def greedy_baseline(
    donations: pd.DataFrame, hotspots: pd.DataFrame, agencies: pd.DataFrame,
    routes: pd.DataFrame, enforce_capacity: bool = False,
) -> pd.DataFrame:
    remaining_demand = hotspots.set_index("block_id").historical_demand.to_dict()
    remaining_capacity = {
        row.agency_id: (
            float(row.capacity_meals)
            if enforce_capacity and pd.notna(row.capacity_meals)
            else float("inf")
        )
        for row in agencies.itertuples()
    }
    allocations = []
    for donation in donations.sort_values(["reported_at", "donation_id"]).itertuples():
        candidates = routes[routes.donation_id.eq(donation.donation_id)].copy()
        if candidates.empty:
            continue
        agency_pickup = candidates.groupby("agency_id").agency_to_supplier_miles.min()
        remaining_meals = donation.available_meals
        for selected_agency in agency_pickup.sort_values(kind="stable").index:
            agency_remaining = remaining_capacity.get(selected_agency, float("inf"))
            if agency_remaining <= ALLOCATION_EPS:
                continue
            agency_candidates = candidates[candidates.agency_id.eq(selected_agency)].sort_values(
                ["supplier_to_hotspot_miles", "hotspot_block_id"], kind="stable"
            )
            for _, route in agency_candidates.iterrows():
                unmet = remaining_demand.get(route.hotspot_block_id, 0.0)
                allocated = min(remaining_meals, agency_remaining, unmet)
                if allocated <= ALLOCATION_EPS:
                    continue
                allocations.append(allocation_record(route, allocated))
                remaining_meals -= allocated
                agency_remaining -= allocated
                remaining_capacity[selected_agency] -= allocated
                remaining_demand[route.hotspot_block_id] -= allocated
                if remaining_meals <= ALLOCATION_EPS or agency_remaining <= ALLOCATION_EPS:
                    break
            if remaining_meals <= ALLOCATION_EPS:
                break
    return pd.DataFrame(allocations, columns=ALLOCATION_COLUMNS)


def global_optimization(
    donations: pd.DataFrame, hotspots: pd.DataFrame, agencies: pd.DataFrame,
    routes: pd.DataFrame, enforce_capacity: bool = False,
) -> tuple[pd.DataFrame, dict]:
    routes = routes.reset_index(drop=True)
    donation_ids = list(donations.donation_id)
    hotspot_ids = list(hotspots.block_id)
    donation_index = {value: i for i, value in enumerate(donation_ids)}
    hotspot_index = {value: i + len(donation_ids) for i, value in enumerate(hotspot_ids)}
    row_indexes, column_indexes, values = [], [], []
    for column, route in routes.iterrows():
        row_indexes.extend([donation_index[route.donation_id], hotspot_index[route.hotspot_block_id]])
        column_indexes.extend([column, column])
        values.extend([1.0, 1.0])
    agency_capacity = agencies.dropna(subset=["capacity_meals"]) if enforce_capacity else agencies.iloc[0:0]
    agency_ids = list(agency_capacity.agency_id)
    agency_index = {
        value: i + len(donation_ids) + len(hotspot_ids)
        for i, value in enumerate(agency_ids)
    }
    if agency_ids:
        for column, route in routes.iterrows():
            if route.agency_id in agency_index:
                row_indexes.append(agency_index[route.agency_id])
                column_indexes.append(column)
                values.append(1.0)
    constraint_count = len(donation_ids) + len(hotspot_ids) + len(agency_ids)
    matrix = coo_matrix(
        (values, (row_indexes, column_indexes)), shape=(constraint_count, len(routes))
    ).tocsr()
    upper_bounds = np.concatenate(
        [
            donations.set_index("donation_id").loc[donation_ids].available_meals.to_numpy(),
            hotspots.set_index("block_id").loc[hotspot_ids].historical_demand.to_numpy(),
            agency_capacity.set_index("agency_id").loc[agency_ids].capacity_meals.to_numpy(),
        ]
    )

    stage1 = linprog(
        -np.ones(len(routes)), A_ub=matrix, b_ub=upper_bounds,
        bounds=(0, None), method="highs",
    )
    if not stage1.success:
        raise RuntimeError(f"Stage 1 optimization failed: {stage1.message}")
    maximum_served = float(stage1.x.sum())

    served_floor = maximum_served - 1e-8
    stage2_matrix = vstack([matrix, -np.ones((1, len(routes)))], format="csr")
    stage2_bounds = np.append(upper_bounds, -served_floor)
    objective = routes.route_total_miles.to_numpy()
    stage2 = linprog(
        objective, A_ub=stage2_matrix, b_ub=stage2_bounds,
        bounds=(0, None), method="highs",
    )
    if not stage2.success:
        raise RuntimeError(f"Stage 2 optimization failed: {stage2.message}")

    allocations = []
    for index in np.flatnonzero(stage2.x > ALLOCATION_EPS):
        allocations.append(allocation_record(routes.iloc[index], float(stage2.x[index])))
    diagnostics = {
        "solver": "scipy.optimize.linprog(method='highs')",
        "stage1_maximum_people_fed": maximum_served,
        "stage2_people_fed": float(stage2.x.sum()),
        "stage2_objective_meal_miles": float(np.sum(objective * stage2.x)),
        "stage1_status": stage1.message,
        "stage2_status": stage2.message,
        "agency_capacity_enforced": enforce_capacity,
    }
    return pd.DataFrame(allocations, columns=ALLOCATION_COLUMNS), diagnostics


def metrics(
    allocations: pd.DataFrame, donations: pd.DataFrame, hotspots: pd.DataFrame
) -> dict:
    available = float(donations.available_meals.sum())
    demand = float(hotspots.historical_demand.sum())
    delivered = float(allocations.meals_allocated.sum())
    if abs(delivered - demand) < 1e-5:
        delivered = demand
    if abs(delivered - available) < 1e-5:
        delivered = available
    route_miles = float(allocations.route_total_miles.sum())
    meal_miles = float((allocations.meals_allocated * allocations.route_total_miles).sum())
    duration = float(allocations.route_duration_minutes.sum())
    transport_cost = float(allocations.transport_cost.sum())
    return {
        "total_meals_available": available,
        "total_demand": demand,
        "meals_delivered": delivered,
        "people_fed": delivered,
        "demand_served": delivered,
        "demand_coverage_pct": delivered / demand * 100 if demand else None,
        "unmet_demand": max(0.0, demand - delivered),
        "food_utilization_pct": delivered / available * 100 if available else None,
        "hotspots_served": int(allocations.hotspot_block_id.nunique()),
        "total_route_distance": route_miles,
        "average_route_distance": route_miles / len(allocations) if len(allocations) else None,
        "meals_per_mile": delivered / route_miles if route_miles else None,
        "total_meal_miles": meal_miles,
        "allocation_route_count": int(len(allocations)),
        "total_route_duration_minutes": duration,
        "transport_cost": transport_cost,
        "food_value": None,
        "net_benefit": None,
        "route_distance_definition": "sum of full candidate-route miles for allocation arcs; no VRP consolidation",
    }


def pct_change(after: float | None, before: float | None) -> float | None:
    if before in (None, 0) or after is None:
        return None
    return (after - before) / before * 100


def zero_if_noise(value: float, tolerance: float = 1e-5) -> float:
    return 0.0 if abs(value) < tolerance else value


def write_summaries(
    baseline: pd.DataFrame, optimized: pd.DataFrame, agencies: pd.DataFrame,
    hotspots: pd.DataFrame, output: Path,
) -> None:
    agency_rows = []
    for agency in agencies.itertuples():
        rows = optimized[optimized.agency_id.eq(agency.agency_id)]
        capacity = getattr(agency, "capacity_meals", None)
        agency_rows.append(
            {
                "agency_id": agency.agency_id,
                "donations_handled": rows.donation_id.nunique(),
                "suppliers_handled": rows.business_id.nunique(),
                "hotspots_served": rows.hotspot_block_id.nunique(),
                "meals_handled": rows.meals_allocated.sum(),
                "route_miles": rows.route_total_miles.sum(),
                "capacity": capacity,
                "utilization": rows.meals_allocated.sum() / capacity if pd.notna(capacity) else None,
            }
        )
    pd.DataFrame(agency_rows).to_csv(output / "agency_summary.csv", index=False)

    baseline_served = baseline.groupby("hotspot_block_id").meals_allocated.sum()
    optimized_served = optimized.groupby("hotspot_block_id").meals_allocated.sum()
    summary = hotspots[
        ["block_id", "historical_demand", "rank", "persistence", "food_access_days_per_week"]
    ].rename(columns={"historical_demand": "original_demand"})
    summary["baseline_served"] = summary.block_id.map(baseline_served).fillna(0)
    summary["optimized_served"] = summary.block_id.map(optimized_served).fillna(0)
    summary["baseline_unmet"] = (summary.original_demand - summary.baseline_served).clip(lower=0)
    summary["optimized_unmet"] = (summary.original_demand - summary.optimized_served).clip(lower=0)
    summary.to_csv(output / "hotspot_summary.csv", index=False)


def main() -> None:
    args = arguments()
    output = OUTPUT_BASE / "simulation" if args.simulation else OUTPUT_BASE
    output.mkdir(parents=True, exist_ok=True)
    donations, hotspots, agencies, routes = load_inputs(args.simulation)
    baseline = greedy_baseline(donations, hotspots, agencies, routes, args.simulation)
    optimized, diagnostics = global_optimization(
        donations, hotspots, agencies, routes, args.simulation
    )
    baseline.to_csv(output / "baseline_allocations.csv", index=False)
    optimized.to_csv(output / "optimized_allocations.csv", index=False)
    suffix = "simulation" if args.simulation else "standard"
    write_table(f"{suffix}_baseline_allocations", baseline)
    write_table(f"{suffix}_optimized_allocations", optimized)

    before = metrics(baseline, donations, hotspots)
    after = metrics(optimized, donations, hotspots)
    improvement = {
        "additional_people_fed": zero_if_noise(after["people_fed"] - before["people_fed"]),
        "coverage_percentage_point_change": zero_if_noise(after["demand_coverage_pct"] - before["demand_coverage_pct"]),
        "unmet_demand_reduction_pct": (
            (before["unmet_demand"] - after["unmet_demand"]) / before["unmet_demand"] * 100
            if before["unmet_demand"] else None
        ),
        "food_utilization_change": zero_if_noise(after["food_utilization_pct"] - before["food_utilization_pct"]),
        "route_distance_change_pct": pct_change(after["total_route_distance"], before["total_route_distance"]),
        "meals_per_mile_change_pct": pct_change(after["meals_per_mile"], before["meals_per_mile"]),
        "meal_miles_change_pct": pct_change(after["total_meal_miles"], before["total_meal_miles"]),
        "transport_cost_change_pct": pct_change(after["transport_cost"], before["transport_cost"]),
    }
    comparison = {
        "before": before,
        "after": after,
        "improvement": improvement,
        "optimization": diagnostics,
        "assumptions": {
            "demand_field": "historical_demand (main hotspots.csv need)",
            "lbs_per_meal": LBS_PER_MEAL,
            "cost_per_mile": COST_PER_MILE,
            "wage_per_hour": WAGE_PER_HOUR,
            "donations": "synthetic reports tied to real main business IDs",
            "candidate_hotspot_threshold": "historical_demand >= 1, aligned with Oscar UI",
            "agency_demo_geocodes": "three missing main coordinates use explicitly synthetic Oscar UI geocodes",
            "simulation_mode": args.simulation,
            "agency_capacity": (
                "enforced from calc/simulation_data/agency_capacity.csv"
                if args.simulation else "not enforced; authoritative capacity absent"
            ),
        },
    }
    (output / "comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    write_summaries(baseline, optimized, agencies, hotspots, output)
    write_table(f"{suffix}_agency_summary", pd.read_csv(output / "agency_summary.csv"))
    write_table(f"{suffix}_hotspot_summary", pd.read_csv(output / "hotspot_summary.csv"))
    print(json.dumps(comparison, indent=2))


if __name__ == "__main__":
    main()
