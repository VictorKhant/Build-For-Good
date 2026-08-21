"""Evaluate and commit occasional donations as insertions into active routes."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta

import pandas as pd

from calc.database import read_table, write_table
from calc.road_routing import road_metrics_many_to_one, road_metrics_one_to_many, road_route

LBS_PER_MEAL = 1.2
WEIGHTS = {"miles": .22, "minutes": .12, "meal_transit": .12,
           "capacity": .12, "demand": .16, "efficiency": .16,
           "freshness": .10}
MAX_MARGINAL_MILES = 10.0
MAX_MARGINAL_MINUTES = 35.0


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _norm(value: float, values: list[float]) -> float:
    lo, hi = min(values), max(values)
    return 0.0 if hi <= lo else (value - lo) / (hi - lo)


def evaluate(supplier: dict, collectors: list[dict]) -> list[dict]:
    """Rank all feasible route/position insertions by marginal hybrid cost."""
    routes = read_table("simulation_route_optimized_vehicle_routes")
    stops = read_table("simulation_route_optimized_vehicle_route_stops")
    capacity = read_table("agency_capacity").set_index("agency_id")
    hotspots = read_table("hotspots").set_index("block_id")
    collector_by_name = {_key(c["name"]): c for c in collectors}
    donation_lbs = float(supplier["report"]["lbs"])
    donation_meals = donation_lbs / LBS_PER_MEAL
    now = datetime.now()

    def deadline(value: str | None, fallback_hours: float) -> datetime:
        if not value:
            return now + timedelta(hours=fallback_hours)
        hour, minute = (int(x) for x in value.split(":"))
        result = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return result + timedelta(days=1) if result < now else result

    pickup_deadline = deadline(supplier["report"].get("pickupTo"), 8)
    expiry_deadline = deadline(supplier["report"].get("expiresAt"),
                               float(supplier["report"].get("expiresInHours") or 12))
    agency_load = routes.groupby("agency_id").meals_loaded.sum().to_dict()
    new_point = (float(supplier["lat"]), float(supplier["lon"]))
    candidates = []

    for route in routes.itertuples(index=False):
        collector = collector_by_name.get(_key(route.agency_name))
        if not collector or route.agency_id not in capacity.index:
            continue
        cap_meals = float(capacity.loc[route.agency_id].capacity_meals_per_day)
        remaining_meals = max(0.0, cap_meals - float(agency_load.get(route.agency_id, 0)))
        if remaining_meals + 1e-7 < donation_meals:
            continue
        ordered = stops[stops.route_id.eq(route.route_id)].sort_values("stop_sequence").to_dict("records")
        if len(ordered) < 2:
            continue
        positions = list(range(1, len(ordered)))
        prev_points = [(float(ordered[i-1]["lat"]), float(ordered[i-1]["lon"])) for i in positions]
        next_points = [(float(ordered[i]["lat"]), float(ordered[i]["lon"])) for i in positions]
        into_new = road_metrics_many_to_one(prev_points, new_point)
        out_new = road_metrics_one_to_many(new_point, next_points)
        for i, incoming, outgoing in zip(positions, into_new, out_new):
            if not incoming["available"] or not outgoing["available"]:
                continue
            replaced_miles = float(ordered[i].get("leg_miles_from_previous") or 0)
            replaced_minutes = float(ordered[i].get("leg_minutes_from_previous") or 0)
            marginal_miles = incoming["miles"] + outgoing["miles"] - replaced_miles
            marginal_minutes = incoming["minutes"] + outgoing["minutes"] - replaced_minutes
            # Detour thresholds are ranking/advisory thresholds, not physical
            # feasibility constraints. Keeping a worse-but-possible insertion
            # lets an autonomous collector receive the offer after every better
            # option declines. Capacity and time/freshness checks below remain
            # hard constraints.
            efficient_insertion = (marginal_miles <= MAX_MARGINAL_MILES
                                   and marginal_minutes <= MAX_MARGINAL_MINUTES)
            arrival_minutes = sum(float(x.get("leg_minutes_from_previous") or 0)
                                  for x in ordered[:i]) + incoming["minutes"]
            pickup_arrival = now + timedelta(minutes=arrival_minutes)
            expected_served = pickup_arrival + timedelta(minutes=outgoing["minutes"])
            if pickup_arrival > pickup_deadline or expected_served > expiry_deadline:
                continue
            downstream = [x for x in ordered[i:] if x["stop_type"] == "hotspot"]
            if not downstream:
                continue
            demand_left, distribution = donation_meals, []
            for stop in downstream:
                demand = float(hotspots.loc[stop["entity_id"]].historical_demand) if stop["entity_id"] in hotspots.index else 0.0
                served = min(demand_left, demand)
                if served > 0:
                    distribution.append({"hotspot_id": stop["entity_id"], "meals": served})
                    demand_left -= served
                if demand_left <= 1e-7:
                    break
            rescued = min(donation_meals - demand_left, remaining_meals)
            if rescued < 1:
                continue
            expected_transit = outgoing["minutes"]
            efficiency = rescued / max(marginal_miles, .1)
            candidates.append({
                "agency_id": collector["id"], "agency_name": collector["name"],
                "network_agency_id": route.agency_id, "route_id": route.route_id,
                "insertion_position": i, "insertion_after": ordered[i-1]["stop_name"],
                "insertion_before": ordered[i]["stop_name"],
                "baseline_miles": float(route.distance_miles),
                "proposed_miles": float(route.distance_miles) + marginal_miles,
                "marginal_miles": marginal_miles, "marginal_minutes": marginal_minutes,
                "additional_meals": rescued,
                "meals_per_marginal_mile": efficiency,
                "remaining_capacity_meals": remaining_meals,
                "expected_meal_transit_minutes": expected_transit,
                "freshness_slack_minutes": max(0.0, (expiry_deadline - expected_served).total_seconds() / 60),
                "efficient_insertion": efficient_insertion,
                "hotspot_name": downstream[0]["stop_name"],
                "hotspot_lat": downstream[0]["lat"], "hotspot_lon": downstream[0]["lon"],
                "pickup_distance_miles": marginal_miles,
                "unmet_demand_served": rescued,
                "capacity_lbs": remaining_meals * LBS_PER_MEAL,
                "distribution": distribution,
                "supplier_lat": new_point[0], "supplier_lon": new_point[1],
            })
    if not candidates:
        return []
    pools = {k: [x[k] for x in candidates] for k in
             ("marginal_miles", "marginal_minutes", "expected_meal_transit_minutes",
              "meals_per_marginal_mile", "remaining_capacity_meals", "additional_meals",
              "freshness_slack_minutes")}
    for row in candidates:
        components = {
            "miles": _norm(row["marginal_miles"], pools["marginal_miles"]),
            "minutes": _norm(row["marginal_minutes"], pools["marginal_minutes"]),
            "meal_transit": _norm(row["expected_meal_transit_minutes"], pools["expected_meal_transit_minutes"]),
            "capacity": 1 - _norm(row["remaining_capacity_meals"], pools["remaining_capacity_meals"]),
            "demand": 1 - _norm(row["additional_meals"], pools["additional_meals"]),
            "efficiency": 1 - _norm(row["meals_per_marginal_mile"], pools["meals_per_marginal_mile"]),
            "freshness": 1 - _norm(row["freshness_slack_minutes"], pools["freshness_slack_minutes"]),
        }
        row["score"] = sum(WEIGHTS[k] * components[k] for k in WEIGHTS)
        row["match_score"] = round(100 * (1 - min(1.0, row["score"])), 1)
        row["score_components"] = components
        row["why"] = [
            f'+{row["marginal_miles"]:.1f} marginal road miles',
            f'+{row["marginal_minutes"]:.0f} marginal minutes',
            f'{row["additional_meals"]:.0f} additional meals rescued',
            f'{row["meals_per_marginal_mile"]:.1f} meals per marginal mile',
        ]
        if not row["efficient_insertion"]:
            row["why"].append("fallback: exceeds preferred detour threshold")
    # One offer per agency: fallback must move to a different autonomous
    # collector, not offer the same collector a second route after it declined.
    best_by_agency = {}
    for row in sorted(candidates, key=lambda x: x["score"]):
        best_by_agency.setdefault(row["agency_id"], row)
    ranked = sorted(best_by_agency.values(), key=lambda x: x["score"])
    for rank, row in enumerate(ranked, 1):
        row["rank"] = rank
    return ranked


def apply(candidate: dict, supplier: dict) -> dict:
    """Commit an accepted insertion as a new active version of the route."""
    routes = read_table("simulation_route_optimized_vehicle_routes")
    stops = read_table("simulation_route_optimized_vehicle_route_stops")
    index = routes.index[routes.route_id.eq(candidate["route_id"])]
    if len(index) != 1:
        raise ValueError("baseline route changed; insertion must be recomputed")
    route_index = index[0]
    if abs(float(routes.loc[route_index, "distance_miles"]) - float(candidate["baseline_miles"])) > .05:
        raise ValueError("baseline route changed; insertion must be recomputed")
    current = stops[stops.route_id.eq(candidate["route_id"])].sort_values("stop_sequence").copy()
    position = int(candidate["insertion_position"])
    before = current[current.stop_sequence.lt(position)].copy()
    after = current[current.stop_sequence.ge(position)].copy()
    after["stop_sequence"] += 1
    inserted = pd.DataFrame([{
        "route_id": candidate["route_id"], "stop_sequence": position,
        "stop_type": "supplier", "entity_id": supplier["id"], "stop_name": supplier["name"],
        "lat": supplier["lat"], "lon": supplier["lon"], "meals_delivered": 0.0,
        "leg_miles_from_previous": None, "leg_minutes_from_previous": None,
        "meal_distance_from_pickup_miles": 0.0, "meal_transit_from_pickup_minutes": 0.0,
    }])
    updated = pd.concat([before, inserted, after], ignore_index=True).sort_values("stop_sequence")
    points = list(zip(updated.lat.astype(float), updated.lon.astype(float)))
    road = road_route(points)
    for offset, row_index in enumerate(updated.index):
        updated.loc[row_index, "leg_miles_from_previous"] = 0.0 if offset == 0 else road["leg_miles"][offset-1]
        updated.loc[row_index, "leg_minutes_from_previous"] = 0.0 if offset == 0 else road["leg_minutes"][offset-1]
    additions = {item["hotspot_id"]: float(item["meals"])
                 for item in candidate.get("distribution", [])}
    old_supplier_positions = updated.index[
        updated.entity_id.astype(str).eq(str(routes.loc[route_index, "supplier_id"]))
    ].tolist()
    old_supplier_position = old_supplier_positions[0] if old_supplier_positions else 1
    inserted_position = updated.index[updated.entity_id.astype(str).eq(str(supplier["id"]))][0]
    for row_index in updated.index[updated.stop_type.eq("hotspot")]:
        old_meals = float(updated.loc[row_index, "meals_delivered"] or 0)
        added = additions.get(updated.loc[row_index, "entity_id"], 0.0)
        old_miles = sum(road["leg_miles"][old_supplier_position:row_index])
        old_minutes = sum(road["leg_minutes"][old_supplier_position:row_index])
        new_miles = sum(road["leg_miles"][inserted_position:row_index]) if added else 0.0
        new_minutes = sum(road["leg_minutes"][inserted_position:row_index]) if added else 0.0
        total = old_meals + added
        updated.loc[row_index, "meal_distance_from_pickup_miles"] = (
            (old_meals * old_miles + added * new_miles) / total if total else None)
        updated.loc[row_index, "meal_transit_from_pickup_minutes"] = (
            (old_meals * old_minutes + added * new_minutes) / total if total else None)
    for item in candidate.get("distribution", []):
        mask = updated.entity_id.eq(item["hotspot_id"])
        updated.loc[mask, "meals_delivered"] = updated.loc[mask, "meals_delivered"].astype(float) + float(item["meals"])
    routes.loc[route_index, "distance_miles"] = road["distance_miles"]
    routes.loc[route_index, "duration_minutes"] = road["duration_minutes"]
    routes.loc[route_index, "meals_loaded"] = float(routes.loc[route_index, "meals_loaded"]) + float(candidate["additional_meals"])
    routes.loc[route_index, "meals_per_truck_mile"] = routes.loc[route_index, "meals_loaded"] / road["distance_miles"]
    delivery = updated[updated.stop_type.eq("hotspot")]
    weights = delivery.meals_delivered.astype(float)
    total_meals = float(weights.sum())
    routes.loc[route_index, "average_meal_distance_miles"] = float(
        (delivery.meal_distance_from_pickup_miles.astype(float) * weights).sum() / total_meals)
    routes.loc[route_index, "average_meal_transit_minutes"] = float(
        (delivery.meal_transit_from_pickup_minutes.astype(float) * weights).sum() / total_meals)
    routes.loc[route_index, "p95_meal_transit_minutes"] = float(
        delivery.meal_transit_from_pickup_minutes.astype(float).quantile(.95))
    routes.loc[route_index, "max_meal_transit_minutes"] = float(
        delivery.meal_transit_from_pickup_minutes.astype(float).max())
    routes.loc[route_index, "geometry_geojson"] = json.dumps(road["geometry"])
    stops = stops[~stops.route_id.eq(candidate["route_id"])]
    write_table("simulation_route_optimized_vehicle_routes", routes)
    write_table("simulation_route_optimized_vehicle_route_stops", pd.concat([stops, updated], ignore_index=True))
    return {"route_id": candidate["route_id"], "distance_miles": road["distance_miles"],
            "duration_minutes": road["duration_minutes"],
            "additional_meals": candidate["additional_meals"], "geometry": road["geometry"]}
