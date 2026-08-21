#!/usr/bin/env python3
"""Build editable supplier-supply and agency-capacity simulation inputs."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT_ROOT / "calc" / "optimization_data"
OUTPUT = PROJECT_ROOT / "calc" / "simulation_data"
LBS_PER_MEAL = 1.2
SCENARIO_ID = "planning_demo_v1"

# Planning assumptions only; no authoritative agency capacities are available.
AGENCY_CAPACITY = {
    "agency_jacobs_cushman_san_diego_food_bank": (650, 2, 4),
    "agency_feeding_san_diego": (750, 3, 6),
    "agency_feeding_san_diego_south_bay": (500, 2, 4),
    "agency_a_b_jones_co": (350, 2, 4),
    "agency_catholic_charities_diocese_of_san_diego": (300, 1, 3),
}


def build_supplier_supply() -> pd.DataFrame:
    businesses = pd.read_csv(SOURCE / "businesses.csv")
    donations = pd.read_csv(SOURCE / "demo_donation_reports.csv")
    columns = [
        "business_id", "business_name", "facility_type", "address", "lat", "lon",
        "surplus_type",
    ]
    result = donations.merge(businesses[columns], on="business_id", validate="many_to_one")
    result = result.rename(
        columns={
            "business_id": "supplier_id",
            "business_name": "supplier_name",
            "quantity_lbs": "available_food_lbs",
        }
    )
    result["available_meals"] = result.available_food_lbs / LBS_PER_MEAL
    result["scenario_id"] = SCENARIO_ID
    result["is_synthetic"] = True
    return result[
        [
            "scenario_id", "donation_id", "supplier_id", "supplier_name",
            "facility_type", "address", "lat", "lon", "surplus_type", "food_type",
            "condition", "available_food_lbs", "available_meals", "reported_at",
            "ready_at", "expires_at", "status", "is_synthetic",
        ]
    ].sort_values(["reported_at", "donation_id"])


def build_agency_capacity() -> pd.DataFrame:
    agencies = pd.read_csv(SOURCE / "agencies.csv")
    capacity = pd.DataFrame.from_dict(
        AGENCY_CAPACITY,
        orient="index",
        columns=["capacity_meals_per_day", "vehicle_count", "max_routes_per_day"],
    ).rename_axis("agency_id").reset_index()
    result = agencies.merge(capacity, on="agency_id", how="left", validate="one_to_one")
    result["capacity_food_lbs_per_day"] = result.capacity_meals_per_day * LBS_PER_MEAL
    result["scenario_id"] = SCENARIO_ID
    result["capacity_period"] = "day"
    result["is_synthetic"] = True
    result["capacity_source"] = "planning assumption; replace with agency-confirmed capacity"
    return result[
        [
            "scenario_id", "agency_id", "agency_name", "address", "lat", "lon",
            "usable_for_routing", "accepts_prepared", "capacity_meals_per_day",
            "capacity_food_lbs_per_day", "vehicle_count", "max_routes_per_day",
            "capacity_period", "is_synthetic", "capacity_source",
        ]
    ]


def build_unified(suppliers: pd.DataFrame, agencies: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for supplier in suppliers.itertuples(index=False):
        rows.append(
            {
                "scenario_id": supplier.scenario_id,
                "entity_type": "supplier",
                "entity_id": supplier.supplier_id,
                "entity_name": supplier.supplier_name,
                "lat": supplier.lat,
                "lon": supplier.lon,
                "available_food_lbs": supplier.available_food_lbs,
                "available_meals": supplier.available_meals,
                "capacity_food_lbs_per_day": None,
                "capacity_meals_per_day": None,
                "vehicle_count": None,
                "max_routes_per_day": None,
                "is_synthetic": supplier.is_synthetic,
            }
        )
    for agency in agencies.itertuples(index=False):
        rows.append(
            {
                "scenario_id": agency.scenario_id,
                "entity_type": "agency",
                "entity_id": agency.agency_id,
                "entity_name": agency.agency_name,
                "lat": agency.lat,
                "lon": agency.lon,
                "available_food_lbs": None,
                "available_meals": None,
                "capacity_food_lbs_per_day": agency.capacity_food_lbs_per_day,
                "capacity_meals_per_day": agency.capacity_meals_per_day,
                "vehicle_count": agency.vehicle_count,
                "max_routes_per_day": agency.max_routes_per_day,
                "is_synthetic": agency.is_synthetic,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    suppliers = build_supplier_supply()
    agencies = build_agency_capacity()
    unified = build_unified(suppliers, agencies)
    suppliers.to_csv(OUTPUT / "supplier_supply.csv", index=False)
    agencies.to_csv(OUTPUT / "agency_capacity.csv", index=False)
    unified.to_csv(OUTPUT / "simulation_dataset.csv", index=False)
    print(f"supplier rows={len(suppliers)}, meals={suppliers.available_meals.sum():.2f}")
    print(f"agency rows={len(agencies)}, capacity={agencies.capacity_meals_per_day.sum():.2f} meals/day")
    print(f"simulation rows={len(unified)}, output={OUTPUT}")


if __name__ == "__main__":
    main()
