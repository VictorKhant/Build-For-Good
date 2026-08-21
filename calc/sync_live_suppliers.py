"""Synchronize main-board surplus reports into the optimization SQL tables."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pandas as pd

from calc.database import read_table, write_table
from calc.road_routing import road_metrics_many_to_one, road_metrics_one_to_many


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "supplier"


def sync_live_suppliers(suppliers: list[dict]) -> dict:
    agencies = read_table("agencies")
    agencies = agencies[agencies[["lat", "lon"]].notna().all(axis=1)]
    hotspots = read_table("hotspots")
    hotspots = hotspots[hotspots.historical_demand.ge(1) & hotspots[["lat", "lon"]].notna().all(axis=1)]
    businesses = read_table("businesses")
    business_rows, donation_rows, supply_rows, route_frames = [], [], [], []
    now = datetime.now(timezone.utc)

    for supplier in suppliers:
        report = supplier.get("report") or {}
        if not report or float(report.get("lbs", 0)) <= 0:
            continue
        supplier_id = f"live_{slug(str(supplier.get('id') or supplier['name']))}"
        donation_id = f"live_donation_{slug(str(supplier.get('id') or supplier['name']))}"
        prepared = supplier.get("surplus") == "prepared"
        ready = now
        expiry = now + timedelta(hours=float(report.get("expiresInHours") or (4 if prepared else 12)))
        business_rows.append({
            "business_id": supplier_id, "business_name": supplier["name"],
            "facility_type": supplier.get("type", "restaurant"), "address": supplier.get("address", ""),
            "lon": supplier["lon"], "lat": supplier["lat"], "sb1383_tier": None,
            "size_metric": "live board registration", "source_url": "main board live report",
            "surplus_type": supplier.get("surplus", "prepared"),
        })
        donation_rows.append({
            "donation_id": donation_id, "business_id": supplier_id,
            "reported_at": now.isoformat(), "food_type": "prepared" if prepared else "packaged/produce",
            "condition": report.get("freshness", "fresh"), "quantity_lbs": float(report["lbs"]),
            "ready_at": ready.isoformat(), "expires_at": expiry.isoformat(), "status": "active",
            "is_synthetic": False, "description": report.get("items", "live surplus report"),
            "synthetic_source": "main board user report",
        })
        supply_rows.append({
            "scenario_id": "live_board", "donation_id": donation_id, "supplier_id": supplier_id,
            "supplier_name": supplier["name"], "facility_type": supplier.get("type", "restaurant"),
            "address": supplier.get("address", ""), "lat": supplier["lat"], "lon": supplier["lon"],
            "surplus_type": supplier.get("surplus", "prepared"),
            "food_type": "prepared" if prepared else "packaged/produce",
            "condition": report.get("freshness", "fresh"), "available_food_lbs": float(report["lbs"]),
            "available_meals": float(report["lbs"]) / 1.2, "reported_at": now.isoformat(),
            "ready_at": ready.isoformat(), "expires_at": expiry.isoformat(), "status": "active",
            "is_synthetic": False,
        })

        # Deliberately rebuild on every simulation run. Supplier reports,
        # coordinates and road conditions may have changed since the previous
        # click, so an earlier route matrix must never determine this result.
        point = (float(supplier["lat"]), float(supplier["lon"]))
        pickups = road_metrics_many_to_one(list(zip(agencies.lat, agencies.lon)), point)
        deliveries = road_metrics_one_to_many(point, list(zip(hotspots.lat, hotspots.lon)))
        rows = []
        for agency, pickup in zip(agencies.itertuples(index=False), pickups):
            for hotspot, delivery in zip(hotspots.itertuples(index=False), deliveries):
                available = pickup["available"] and delivery["available"]
                rows.append({
                    "agency_id": agency.agency_id, "donation_id": donation_id,
                    "business_id": supplier_id, "hotspot_block_id": hotspot.block_id,
                    "agency_to_supplier_miles": pickup["miles"],
                    "supplier_to_hotspot_miles": delivery["miles"],
                    "route_total_miles": pickup["miles"] + delivery["miles"] if available else None,
                    "route_duration_minutes": pickup["minutes"] + delivery["minutes"] if available else None,
                    "route_available": available,
                    "food_compatible": not prepared or str(agency.accepts_prepared).lower() == "yes",
                    "route_error": pickup["error"] or delivery["error"],
                    "distance_method": "osrm_openstreetmap_driving",
                })
        route_frames.append(pd.DataFrame(rows))

    live_businesses = pd.DataFrame(business_rows)
    if not live_businesses.empty:
        businesses = businesses[~businesses.business_id.isin(live_businesses.business_id)]
        businesses = pd.concat([businesses, live_businesses], ignore_index=True)
    write_table("businesses", businesses)
    write_table("donation_reports", pd.DataFrame(donation_rows))
    write_table("supplier_supply", pd.DataFrame(supply_rows))
    empty_routes = read_table("route_matrix").iloc[0:0]
    write_table("route_matrix", pd.concat(route_frames, ignore_index=True) if route_frames else empty_routes)
    return {"suppliers": len(supply_rows), "meals": sum(x["available_meals"] for x in supply_rows), "route_rows": sum(len(x) for x in route_frames)}
