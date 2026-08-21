"""Advisory hybrid ranking for one live food-pickup request."""

from __future__ import annotations

import os

import dispatch


WEIGHTS = {
    "pickup_distance": float(os.getenv("MATCH_W_PICKUP_DISTANCE", ".22")),
    "pickup_transit": float(os.getenv("MATCH_W_PICKUP_TRANSIT", ".14")),
    "meal_distance": float(os.getenv("MATCH_W_MEAL_DISTANCE", ".18")),
    "capacity": float(os.getenv("MATCH_W_CAPACITY", ".18")),
    "unmet_demand": float(os.getenv("MATCH_W_UNMET_DEMAND", ".18")),
    "network_balance": float(os.getenv("MATCH_W_NETWORK_BALANCE", ".10")),
}


def _norm(value: float, values: list[float]) -> float:
    lo, hi = min(values), max(values)
    return 0.0 if hi <= lo else (value - lo) / (hi - lo)


def rank(supplier: dict, agencies: list[dict], pantries: list[dict],
         hotspots: list[dict], now, accepted_lbs: dict[str, float] | None = None) -> list[dict]:
    """Return one best downstream plan per feasible collector, lowest score first."""
    accepted_lbs = accepted_lbs or {}
    result = dispatch.compute(supplier, agencies, pantries, hotspots, now)
    meals = float(result["meals"])
    candidates = []
    for pair in result["pairs"]:
        collector = pair["collector"]
        capacity = float(collector.get("capacityLbs") or supplier["report"]["lbs"])
        load_ratio = accepted_lbs.get(collector["id"], 0.0) / max(capacity, 1.0)
        candidates.append({
            "agency_id": collector["id"], "agency_name": collector["name"],
            "agency_kind": collector.get("kind", "agency"),
            "pickup_distance_miles": float(pair["leg1"]),
            "pickup_transit_minutes": float(pair["leg1"]) / dispatch.C["AVG_SPEED_MPH"] * 60,
            "meal_distance_miles": float(pair["leg2"]),
            "expected_meal_transit_minutes": float(pair["minutes"]),
            "capacity_lbs": capacity,
            "capacity_penalty": max(0.0, float(supplier["report"]["lbs"]) - capacity) / max(float(supplier["report"]["lbs"]), 1.0),
            "unmet_demand_served": float(pair["served"]),
            "unmet_demand_penalty": max(0.0, meals - float(pair["served"])) / max(meals, 1.0),
            "network_balance_penalty": load_ratio,
            "hotspot_id": pair["hotspot"]["id"] if pair.get("hotspot") else None,
            "hotspot_name": pair["hotspot"]["location"] if pair.get("hotspot") else collector["name"],
            "agency_lat": collector.get("lat"), "agency_lon": collector.get("lon"),
            "supplier_lat": supplier["lat"], "supplier_lon": supplier["lon"],
            "hotspot_lat": pair["hotspot"]["lat"] if pair.get("hotspot") else collector.get("lat"),
            "hotspot_lon": pair["hotspot"]["lon"] if pair.get("hotspot") else collector.get("lon"),
        })
    if not candidates:
        return []

    fields = ["pickup_distance_miles", "pickup_transit_minutes", "meal_distance_miles"]
    pools = {field: [row[field] for row in candidates] for field in fields}
    for row in candidates:
        components = {
            "pickup_distance": _norm(row["pickup_distance_miles"], pools["pickup_distance_miles"]),
            "pickup_transit": _norm(row["pickup_transit_minutes"], pools["pickup_transit_minutes"]),
            "meal_distance": _norm(row["meal_distance_miles"], pools["meal_distance_miles"]),
            "capacity": row["capacity_penalty"],
            "unmet_demand": row["unmet_demand_penalty"],
            "network_balance": min(row["network_balance_penalty"], 1.0),
        }
        row["score"] = sum(WEIGHTS[key] * components[key] for key in WEIGHTS)
        row["match_score"] = round(100 * (1 - min(row["score"], 1.0)), 1)
        row["score_components"] = components

    best_by_agency = {}
    for row in sorted(candidates, key=lambda item: item["score"]):
        best_by_agency.setdefault(row["agency_id"], row)
    ranked = list(best_by_agency.values())
    ranked.sort(key=lambda item: item["score"])
    for index, row in enumerate(ranked, 1):
        row["rank"] = index
        row["why"] = [
            f'{row["pickup_distance_miles"]:.1f} mi pickup distance',
            f'{row["unmet_demand_served"]:.0f} meals of unmet demand served',
            f'{row["capacity_lbs"]:.0f} lb collection capacity',
            f'{row["expected_meal_transit_minutes"]:.0f} min expected meal transit',
        ]
    return ranked
