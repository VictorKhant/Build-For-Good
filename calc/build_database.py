#!/usr/bin/env python3
"""Load current CSV snapshots into the BellyUp SQLite database."""

from pathlib import Path
import shutil
import pandas as pd

from database import PROJECT_ROOT, DB_PATH, write_table

SOURCES = {
    "businesses": "calc/optimization_data/businesses.csv",
    "agencies": "calc/optimization_data/agencies.csv",
    "hotspots": "calc/optimization_data/hotspots.csv",
    "mobile_pantries": "calc/optimization_data/mobile_pantries.csv",
    "donation_reports": "calc/optimization_data/demo_donation_reports.csv",
    "route_matrix": "calc/route_cache/route_matrix.csv",
    "supplier_supply": "calc/simulation_data/supplier_supply.csv",
    "agency_capacity": "calc/simulation_data/agency_capacity.csv",
    "simulation_entities": "calc/simulation_data/simulation_dataset.csv",
}

def main() -> None:
    archive = PROJECT_ROOT / "calc" / "original_files"
    for table, relative in SOURCES.items():
        source = PROJECT_ROOT / relative
        if not source.exists():
            raise FileNotFoundError(source)
        frame = pd.read_csv(source)
        write_table(table, frame)
        destination = archive / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        print(f"{table}: {len(frame)} rows")
    print(f"database={DB_PATH}")
    print(f"original CSV snapshots={archive}")

if __name__ == "__main__":
    main()
