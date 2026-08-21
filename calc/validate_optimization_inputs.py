#!/usr/bin/env python3
"""Validate normalized optimization inputs and the cached OSRM route matrix."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA = PROJECT_ROOT / "calc" / "optimization_data"
ROUTES = PROJECT_ROOT / "calc" / "route_cache" / "route_matrix.csv"


def main() -> None:
    errors: list[str] = []
    warnings: list[str] = []
    businesses = pd.read_csv(DATA / "businesses.csv")
    agencies = pd.read_csv(DATA / "agencies.csv")
    hotspots = pd.read_csv(DATA / "hotspots.csv")
    donations = pd.read_csv(DATA / "demo_donation_reports.csv")

    for label, frame, identifier in [
        ("business", businesses, "business_id"),
        ("agency", agencies, "agency_id"),
        ("hotspot", hotspots, "block_id"),
        ("donation", donations, "donation_id"),
    ]:
        duplicates = frame[identifier].duplicated().sum()
        if duplicates:
            errors.append(f"{label}: {duplicates} duplicate {identifier} values")

    missing_businesses = set(donations.business_id) - set(businesses.business_id)
    if missing_businesses:
        errors.append(f"donations reference missing business IDs: {sorted(missing_businesses)}")

    for label, frame in [("business", businesses), ("agency", agencies), ("hotspot", hotspots)]:
        missing = int(frame[["lat", "lon"]].isna().any(axis=1).sum())
        complete = frame[["lat", "lon"]].notna().all(axis=1)
        invalid = int(
            (complete & (~frame.lat.between(-90, 90) | ~frame.lon.between(-180, 180))).sum()
        )
        if label == "agency" and missing:
            warnings.append(f"agency: {missing} rows lack coordinates and will be excluded")
        elif missing:
            errors.append(f"{label}: {missing} rows lack coordinates")
        if invalid:
            errors.append(f"{label}: {invalid} rows have invalid coordinates")

    if hotspots.historical_demand.lt(0).any():
        errors.append("hotspots contain negative historical demand")
    if donations.quantity_lbs.lt(0).any():
        errors.append("donations contain negative quantity_lbs")

    for column in ["reported_at", "ready_at", "expires_at"]:
        parsed = pd.to_datetime(donations[column], errors="coerce", utc=True)
        if parsed.isna().any():
            errors.append(f"donations contain invalid {column}")
    ready = pd.to_datetime(donations.ready_at, errors="coerce", utc=True)
    expiry = pd.to_datetime(donations.expires_at, errors="coerce", utc=True)
    if (expiry <= ready).any():
        errors.append("donations contain expires_at <= ready_at")

    expected_donations = set(donations.loc[donations.status.eq("active"), "donation_id"])
    positive_hotspots = set(hotspots.loc[hotspots.historical_demand.ge(1), "block_id"])
    usable_agencies = set(agencies.loc[agencies[["lat", "lon"]].notna().all(axis=1), "agency_id"])
    if not ROUTES.exists():
        errors.append("calc/route_cache/route_matrix.csv is missing")
        route_count = available_count = 0
    else:
        routes = pd.read_csv(ROUTES)
        route_count = len(routes)
        available_count = int(routes.route_available.fillna(False).sum())
        if routes[["agency_id", "donation_id", "hotspot_block_id"]].duplicated().any():
            errors.append("route matrix contains duplicate combination IDs")
        if not set(routes.donation_id).issubset(expected_donations):
            errors.append("route matrix contains unknown/inactive donation IDs")
        if not set(routes.agency_id).issubset(usable_agencies):
            errors.append("route matrix contains agencies without usable coordinates")
        if not set(routes.hotspot_block_id).issubset(positive_hotspots):
            errors.append("route matrix contains unknown/non-positive hotspots")
        expected = len(expected_donations) * len(usable_agencies) * len(positive_hotspots)
        if route_count != expected:
            errors.append(f"route matrix has {route_count} rows; expected {expected}")
        unavailable = route_count - available_count
        if unavailable:
            warnings.append(f"route matrix: {unavailable} combinations explicitly unavailable")
        if "food_compatible" not in routes:
            errors.append("route matrix lacks food_compatible")

    print("Optimization input validation")
    print(f"  businesses={len(businesses)} donations={len(donations)} agencies={len(agencies)} hotspots={len(hotspots)}")
    print(f"  routes={route_count} available_routes={available_count}")
    print(f"  errors={len(errors)} warnings={len(warnings)}")
    for message in errors:
        print(f"  ERROR: {message}")
    for message in warnings:
        print(f"  WARNING: {message}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
