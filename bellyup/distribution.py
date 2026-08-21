"""Leg 2 -- a mobile pantry's run out to the hubspots.

    AGENCY PANTRY  --mobile pantry-->  hubspot, hubspot, hubspot

Only agencies with a mobile pantry appear here. A fixed pantry cannot bring
food to someone sleeping on a block; its people come to it, and that is leg 1's
problem.

The vehicle leaves the pantry loaded and returns empty:

    pantry -> hub A -> hub B -> pantry

Hubspots keep hard demand budgets and a ledger even though the agency's own
intake is uncapped. The reason is different at each end: an agency that
travels can always find somewhere to take food, but a single block only holds
so many people, and food dropped beyond that is food left on a pavement.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import permutations

import demand as demand_mod
import economics as ec
import rules
from economics import CONFIG
from needs import haversine_km


def _route(agency, ordered_hubs, start: datetime, c):
    """pantry -> hubs in order -> pantry, with an arrival time per stop."""
    rf, speed = c["ROAD_FACTOR"], c["AVG_SPEED_KMH"]
    legs, arrivals = [], []
    lat, lon = agency["lat"], agency["lon"]
    t = 0.0
    for h in ordered_hubs:
        leg = haversine_km(lat, lon, h["lat"], h["lon"]) * rf
        legs.append(leg)
        t += leg / speed * 60.0
        arrivals.append(start + timedelta(minutes=t))
        t += c["UNLOAD_MIN"]
        lat, lon = h["lat"], h["lon"]
    back = haversine_km(lat, lon, agency["lat"], agency["lon"]) * rf
    total_km = sum(legs) + back
    drive_min = total_km / speed * 60.0
    total_min = drive_min + c["UNLOAD_MIN"] * len(ordered_hubs)
    return {"legs": legs, "return_km": back, "total_km": total_km,
            "drive_min": drive_min, "total_min": total_min, "arrivals": arrivals}


def _best_order(agency, hubs, start, c):
    """Exhaustive over <= max_hubspot_stops, so no TSP heuristic to defend."""
    best, best_order = None, None
    for order in permutations(range(len(hubs))):
        r = _route(agency, [hubs[i] for i in order], start, c)
        if best is None or r["total_km"] < best["total_km"]:
            best, best_order = r, list(order)
    return best, best_order


def plan(agency: dict, hubspots: list[dict], available_lbs: float,
         now: datetime | None = None, cfg: dict | None = None,
         ledger=None, commit: bool = False) -> dict:
    """Best mobile-pantry run for one agency, given food on hand."""
    c = cfg or CONFIG
    now = now or datetime.now()
    ledger = demand_mod.LEDGER if ledger is None else ledger

    name = agency["name"]
    if not agency.get("has_mobile_pantry"):
        return _empty(agency, "NO_MOBILE_PANTRY",
                      f"{name} has no mobile pantry — it cannot serve a hubspot.")

    start = _departure(agency, now, c)
    if start is None:
        return _empty(agency, "AGENCY_CLOSED",
                      f"{name}'s mobile pantry is not scheduled to run at "
                      f"{now.strftime('%a %H:%M')}. "
                      f"Next: {rules.next_open(now, agency['mobile_windows'])}.")

    load = min(available_lbs, agency["mobile_capacity_lbs"])
    budgets = demand_mod.daily_demand(hubspots, c)

    rejections = []
    candidates = []
    for h in hubspots:
        b = budgets[h["dest_id"]]
        left = ledger.remaining(h["dest_id"], now, b["daily_demand_lbs"])
        if b["daily_demand_lbs"] <= 0:
            rejections.append(rules.reject(h["dest_id"], h["name"], "NO_MEASURED_DEMAND",
                f"{h['name']} has no counted population."))
            continue
        if left < min(c["MIN_STOP_LBS"], load):
            rejections.append(rules.reject(h["dest_id"], h["name"], "DEMAND_SATURATED",
                f"{h['name']} has taken {b['daily_demand_lbs'] - left:.0f} lb of its "
                f"{b['daily_demand_lbs']:.0f} lb — only {left:.0f} lb left today."))
            continue
        candidates.append({"hub": h, "headroom": left, "budget": b["daily_demand_lbs"]})

    if not candidates:
        out = _empty(agency, "DEMAND_SATURATED",
                     "Every hubspot in range has met its demand for today.")
        out["rejections"] = rejections
        out["rejection_summary"] = rules.summarise(rejections)
        return out

    # need multiplier across the hubspots still open to us
    nows = [x["hub"]["need_now"] for x in candidates]
    trends = [x["hub"]["need_trend"] for x in candidates]
    lo_n, hi_n, lo_t, hi_t = min(nows), max(nows), min(trends), max(trends)
    mult = {x["hub"]["dest_id"]: ec.need_multiplier(
        ec.normalise(x["hub"]["need_now"], lo_n, hi_n),
        ec.normalise(x["hub"]["need_trend"], lo_t, hi_t), c) for x in candidates}

    max_stops = max(1, int(agency.get("max_hubspot_stops", 1)))
    chosen: list[dict] = []
    remaining = load
    best_state = None

    # greedy on marginal value: what is one more stop worth, net of the detour
    while remaining >= min(c["MIN_STOP_LBS"], load) and len(chosen) < max_stops:
        best = None
        for cand in candidates:
            hid = cand["hub"]["dest_id"]
            if any(x["cand"]["hub"]["dest_id"] == hid for x in chosen):
                continue
            take = min(remaining, cand["headroom"])
            if take < min(c["MIN_STOP_LBS"], remaining):
                continue
            trial = chosen + [{"cand": cand, "lbs": take}]
            state = _price(agency, trial, mult, start, c)
            if state is None:
                continue
            if best is None or state["net_value"] > best[1]["net_value"]:
                best = (trial, state, take)
        if best is None:
            break
        prev = best_state["net_value"] if best_state else 0.0
        if best[1]["net_value"] <= prev:
            break                       # the extra stop does not pay for itself
        chosen, best_state, took = best[0], best[1], best[2]
        remaining -= took

    if best_state is None:
        out = _empty(agency, "NET_NEGATIVE",
                     f"No hubspot run from {name} covers its own cost.")
        out["rejections"] = rejections
        out["rejection_summary"] = rules.summarise(rejections)
        return out

    if commit:
        for s in best_state["stops"]:
            ledger.commit(s["dest_id"], datetime.fromisoformat(s["arrival_at"]),
                          s["lbs"], name, agency["agency_id"])

    best_state["rejections"] = rejections
    best_state["rejection_summary"] = rules.summarise(rejections)
    best_state["available_lbs"] = round(available_lbs, 1)
    best_state["loaded_lbs"] = round(load, 1)
    best_state["left_at_pantry_lbs"] = round(available_lbs - best_state["delivered_lbs"], 1)
    best_state["explanation"] = _explain(best_state, agency)
    return best_state


def _departure(agency, now, c):
    """When the mobile pantry can next leave, if it runs today."""
    for w in agency.get("mobile_windows", []):
        if w["dow"] != now.weekday():
            continue
        if rules.window_matches(now, w):
            return now
        open_at = now.replace(hour=rules.hhmm(w["start"]) // 60,
                              minute=rules.hhmm(w["start"]) % 60,
                              second=0, microsecond=0)
        if open_at >= now and rules.window_matches(open_at, w):
            return open_at
    return None


def _price(agency, trial, mult, start, c):
    hubs = [x["cand"]["hub"] for x in trial]
    route, order = _best_order(agency, hubs, start, c)
    ordered = [trial[i] for i in order]

    for stop, arrival in zip(ordered, route["arrivals"]):
        if not rules.in_windows(arrival, stop["cand"]["hub"].get("open_windows", [])):
            return None

    fuel = route["total_km"] * agency["cost_per_km"]
    staff = route["total_min"] / 60.0 * agency["wage_per_hour"] * agency["staff_per_run"]
    cost = fuel + staff

    meals = value = weighted = 0.0
    stops = []
    for stop, arrival in zip(ordered, route["arrivals"]):
        h = stop["cand"]["hub"]
        m = stop["lbs"] / c["LBS_PER_MEAL"]
        v = m * c["VALUE_PER_MEAL"]
        meals += m; value += v
        weighted += v * mult[h["dest_id"]]
        stops.append({
            "dest_id": h["dest_id"], "name": h["name"],
            "lat": h["lat"], "lon": h["lon"],
            "block_id": h.get("block_id"),
            "lbs": round(stop["lbs"], 1), "meals": round(m, 1),
            "need_now": round(h["need_now"], 1), "need_trend": round(h["need_trend"], 1),
            "daily_demand_lbs": stop["cand"]["budget"],
            "need_multiplier": round(mult[h["dest_id"]], 3),
            "arrival_at": arrival.isoformat(timespec="minutes"),
        })

    delivered = sum(x["lbs"] for x in trial)
    return {
        "leg": "distribution",
        "agency_id": agency["agency_id"], "agency_name": agency["name"],
        "simulated": agency.get("simulated", False),
        "lat": agency["lat"], "lon": agency["lon"],
        "feasible": True,
        "stops": stops, "n_stops": len(stops),
        "delivered_lbs": round(delivered, 1),
        "route_km": {"legs": [round(x, 2) for x in route["legs"]],
                     "return": round(route["return_km"], 2),
                     "total": round(route["total_km"], 2)},
        "total_min": round(route["total_min"], 1),
        "departs_at": start.isoformat(timespec="minutes"),
        "meals": round(meals, 1),
        "food_value": round(value, 2),
        "fuel_cost": round(fuel, 2), "personnel_cost": round(staff, 2),
        "transport_cost": round(cost, 2),
        "cost_per_meal": round(cost / meals, 2) if meals else float("inf"),
        "net_value": round(weighted - cost, 2),
    }


def _empty(agency, code, msg):
    return {"leg": "distribution", "agency_id": agency["agency_id"],
            "agency_name": agency["name"], "feasible": False,
            "reason_code": code, "reason": msg, "stops": [], "n_stops": 0,
            "delivered_lbs": 0.0, "rejections": [], "rejection_summary": []}


def _explain(p, agency):
    legs = " -> ".join(f"{s['name'].replace('Outreach hubspot — ', '')} ({s['lbs']:.0f} lb)"
                       for s in p["stops"])
    parts = [f"{agency['name']}'s mobile pantry runs {p['route_km']['total']:.1f} km "
             f"from its base: {legs}, then back."]
    parts.append(f"{p['total_min']:.0f} min, ${p['transport_cost']:.2f} in fuel and "
                 f"staff time, {p['meals']:.0f} meals at ${p['cost_per_meal']:.2f}/meal.")
    top = max(p["stops"], key=lambda s: s["lbs"])
    word = "rising" if top["need_trend"] > 0 else "falling"
    parts.append(f"The largest drop goes to {top['name'].replace('Outreach hubspot — ', '')}, "
                 f"{top['need_now']:.0f} people counted and {word} "
                 f"{abs(top['need_trend']):.0f}/yr, against a {top['daily_demand_lbs']:.0f} lb "
                 f"daily demand.")
    if p["left_at_pantry_lbs"] > 0:
        parts.append(f"{p['left_at_pantry_lbs']:.0f} lb stays at the pantry — the "
                     f"hubspots in range cannot absorb more today.")
    return " ".join(parts)
