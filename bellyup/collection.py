"""Leg 1 -- which agency collects a small restaurant's surplus.

    restaurant  --collected by-->  AGENCY PANTRY

The restaurant pays nothing and carries no transport liability; it donates for
the enhanced tax deduction. The agency absorbs the whole cost of the run, so
cost efficiency is still the thing being optimised -- just on the agency's
books, not the donor's.

The agency's pantry IS its depot, so the run is a round trip:

    pantry -> restaurant -> pantry

Ranking weights the food by how much good it does where it lands:

  * a FIXED pantry serves whoever walks in, so its need is the counted
    population within walking distance
  * a MOBILE pantry travels, so its need is the hubspot demand it can reach
"""

from __future__ import annotations

from datetime import datetime, timedelta

import agencies as ag_mod
import economics as ec
import rules
import schedule as sched
from economics import CONFIG
from needs import haversine_km


def _round_trip(agency, donation, now, c):
    """pantry -> restaurant -> pantry, with the clock."""
    rf = c["ROAD_FACTOR"]
    one_way = haversine_km(agency["lat"], agency["lon"],
                           donation["lat"], donation["lon"]) * rf
    total_km = one_way * 2
    drive_min = total_km / c["AVG_SPEED_KMH"] * 60.0
    total_min = drive_min + c["LOAD_MIN"] + c["UNLOAD_MIN"]

    start = max(donation["ready_at"], now)
    pickup_at = start + timedelta(minutes=one_way / c["AVG_SPEED_KMH"] * 60.0)
    back_at = pickup_at + timedelta(
        minutes=c["LOAD_MIN"] + one_way / c["AVG_SPEED_KMH"] * 60.0)
    return {"one_way_km": one_way, "total_km": total_km, "drive_min": drive_min,
            "total_min": total_min, "pickup_at": pickup_at, "back_at": back_at}


def reaches_people_at(agency, hubspots, back_at: datetime, c):
    """When food collected on this run actually gets into someone's hands.

    MOBILE  next scheduled mobile run, plus the drive to its nearest hubspot,
            plus handout time. Food goes out to people.
    FIXED   next time the pantry is open, plus average dwell -- the wait for
            walk-in traffic to arrive. People come to food.

    Returns (datetime, description) or (None, why not).
    """
    if agency["has_mobile_pantry"]:
        start = rules.next_window_start(back_at, agency["mobile_windows"])
        if start is None:
            return None, "no scheduled mobile run"
        if hubspots:
            nearest = min(haversine_km(agency["lat"], agency["lon"], h["lat"], h["lon"])
                          for h in hubspots)
        else:
            nearest = 0.0
        mins = nearest * c["ROAD_FACTOR"] / c["AVG_SPEED_KMH"] * 60.0 + c["MOBILE_HANDOUT_MIN"]
        return start + timedelta(minutes=mins), "mobile run out to a hubspot"

    start = rules.next_window_start(back_at, agency["operating_windows"])
    if start is None:
        return None, "no published opening hours"
    return (start + timedelta(hours=c["FIXED_PANTRY_DWELL_HOURS"]),
            f"walk-in traffic (~{c['FIXED_PANTRY_DWELL_HOURS']:.0f}h dwell)")


def service_need(agencies, budgets, hubspots, cfg, idx=None):
    """How much good food does at each agency, and why.

    Fixed pantries are scored on walk-in population. Mobile pantries are scored
    on the hubspot need within MOBILE_RANGE_KM of their base -- a travelling
    pantry is not limited to its own doorstep.
    """
    c = cfg
    out = {}
    for a in agencies:
        aid = a["agency_id"]
        if a["has_mobile_pantry"]:
            # Distance-weighted. Every mobile pantry can technically reach every
            # hubspot, so an unweighted sum scores them all identically and the
            # need term stops doing any work. Weighting by remaining range also
            # stops an agency 20 km out from being ranked as though its onward
            # leg were free -- that leg is real cost the agency pays.
            reach, now_w, trend_w = [], 0.0, 0.0
            for h in hubspots:
                km = haversine_km(a["lat"], a["lon"], h["lat"], h["lon"])
                if km > c["MOBILE_RANGE_KM"]:
                    continue
                w = 1.0 - km / c["MOBILE_RANGE_KM"]
                reach.append(h)
                now_w += h["need_now"] * w
                trend_w += h["need_trend"] * w
            out[aid] = {
                "need_now": now_w,
                "need_trend": trend_w,
                "basis": f"{len(reach)} hubspots within {c['MOBILE_RANGE_KM']:.0f} km, "
                         f"distance-weighted",
                "n_units": len(reach),
            }
        else:
            b = budgets[aid]
            out[aid] = {
                "need_now": b.get("walk_in_people", 0.0) or 0.0,
                # persons/year, matching the hubspot trend basis
                "need_trend": b.get("walk_in_trend_per_year", 0.0) or 0.0,
                "basis": f"{b['n_blocks']} blocks within walking distance",
                "n_units": b["n_blocks"],
            }
    return out


def match(donation: dict, agencies: list[dict], hubspots: list[dict],
          now: datetime | None = None, cfg: dict | None = None,
          ledger=None, commit: bool = False) -> dict:
    """Rank the agencies that could collect this donation."""
    import demand as demand_mod

    c = cfg or CONFIG
    now = now or datetime.now()
    ledger = demand_mod.LEDGER if ledger is None else ledger

    budgets = ag_mod.intake_demand(agencies, c)
    needs_by_agency = service_need(agencies, budgets, hubspots, c)

    qty = donation["quantity_lbs"]
    cond = donation["condition"]
    ftype = donation["food_type"]

    rejections, survivors = [], []
    for a in agencies:
        aid, name = a["agency_id"], a["name"]
        R = lambda code, msg: rules.reject(aid, name, code, msg)

        if not a.get("collects_donations", True):
            rejections.append(R("NOT_COLLECTING", f"{name} does not collect donations."))
            continue
        if qty < c["MIN_QUANTITY_LBS"]:
            rejections.append(R("QTY_TOO_SMALL",
                f"Below the {c['MIN_QUANTITY_LBS']:.0f} lb minimum for a dedicated run."))
            continue
        if ftype not in a["accepts"]:
            rejections.append(R("TYPE_NOT_ACCEPTED",
                f"{name} does not accept {ftype.replace('_', ' ')}."))
            continue
        need_storage = rules.STORAGE_FOR.get(cond)
        if need_storage and not a["storage"].get(need_storage, False):
            rejections.append(R("NO_STORAGE", f"{name} has no {cond} storage."))
            continue
        if cond in rules.COLD_CONDITIONS and not a["has_refrigerated_vehicle"]:
            rejections.append(R("NO_COLD_VEHICLE", f"{name} has no refrigerated vehicle."))
            continue

        b = dict(budgets[aid])
        # An agency can declare its own cap for the day -- staff off, fridge
        # full, van in the shop. It only ever tightens the demand-derived
        # budget, and for a mobile pantry (uncapped by demand) it is the only
        # ceiling there is.
        declared = sched.LIMITS.get(aid, now)
        if declared is not None:
            taken = sched.SCHEDULE.committed_lbs(aid, now)
            room = max(0.0, declared - taken)
            if room < min(c["MIN_STOP_LBS"], qty):
                rejections.append(R("LIMIT_REACHED",
                    f"{name} set a {declared:.0f} lb limit for today and has "
                    f"{taken:.0f} lb scheduled — only {room:.0f} lb left."))
                continue
            b["demand_lbs"] = min(b["demand_lbs"], room) if not b["uncapped_by_demand"] else room
            b["uncapped_by_demand"] = False
            b["capped_by"] = "agency-declared limit"

        if not b["uncapped_by_demand"]:
            if b["demand_lbs"] <= 0:
                rejections.append(R("NO_WALK_IN_DEMAND",
                    f"{name} has no counted population within walking distance — "
                    f"nobody would come for it."))
                continue
            left = ledger.remaining(aid, now, b["demand_lbs"])
            if left < min(c["MIN_STOP_LBS"], qty):
                rejections.append(R("INTAKE_SATURATED",
                    f"{name} has taken {b['demand_lbs'] - left:.0f} lb of its "
                    f"{b['demand_lbs']:.0f} lb walk-in demand — only {left:.0f} lb "
                    f"left today."))
                continue

        route = _round_trip(a, donation, now, c)

        limit = c["MAX_TRANSIT_MIN"][cond]
        if route["total_min"] > limit:
            rejections.append(R("COLD_CHAIN",
                f"{route['total_min']:.0f} min round trip exceeds the {limit} min "
                f"safe window for {cond} food."))
            continue
        margin = (donation["expires_at"] - route["back_at"]).total_seconds() / 60.0
        if margin < c["SAFETY_MARGIN_MIN"]:
            rejections.append(R("EXPIRES_IN_TRANSIT",
                f"Back at the pantry {abs(margin):.0f} min "
                f"{'after' if margin < 0 else 'before'} expiry — under the "
                f"{c['SAFETY_MARGIN_MIN']:.0f} min margin."))
            continue
        if not rules.in_windows(route["pickup_at"], a["operating_windows"]):
            rejections.append(R("AGENCY_CLOSED",
                f"{name} is not operating at the "
                f"{route['pickup_at'].strftime('%a %H:%M')} pickup. "
                f"Next open: {rules.next_open(route['pickup_at'], a['operating_windows'])}."))
            continue

        reach_at, how = reaches_people_at(a, hubspots, route["back_at"], c)
        if reach_at is None:
            rejections.append(R("AGENCY_CLOSED",
                f"{name} has {how} — no way to get this to anyone."))
            continue
        if reach_at > donation["expires_at"]:
            late_h = (reach_at - donation["expires_at"]).total_seconds() / 3600.0
            rejections.append(R("SPOILS_BEFORE_REACHED",
                f"{name} would only reach people via {how} — "
                f"{late_h:.0f}h after this food expires."))
            continue

        survivors.append({"agency": a, "route": route, "budget": b,
                          "need": needs_by_agency[aid],
                          "reach_at": reach_at, "reach_how": how})

    # --- need multiplier, normalised across the surviving agencies ----------
    if survivors:
        nows = [s["need"]["need_now"] for s in survivors]
        trends = [s["need"]["need_trend"] for s in survivors]
        lo_n, hi_n, lo_t, hi_t = min(nows), max(nows), min(trends), max(trends)
    else:
        lo_n = hi_n = lo_t = hi_t = 0.0

    matches = []
    for s in survivors:
        a, route, b = s["agency"], s["route"], s["budget"]
        mult = ec.need_multiplier(ec.normalise(s["need"]["need_now"], lo_n, hi_n),
                                  ec.normalise(s["need"]["need_trend"], lo_t, hi_t), c)

        takeable = qty if b["uncapped_by_demand"] else min(
            qty, ledger.remaining(a["agency_id"], now, b["demand_lbs"]))
        meals = takeable / c["LBS_PER_MEAL"]
        gross_value = meals * c["VALUE_PER_MEAL"]
        fresh = ec.freshness_factor(donation["ready_at"], donation["expires_at"],
                                    s["reach_at"], c)
        food_value = gross_value * fresh
        if a["has_mobile_pantry"] and c["MOBILE_PRIORITY_BONUS"]:
            mult += c["MOBILE_PRIORITY_BONUS"]
        fuel = route["total_km"] * a["cost_per_km"]
        staff = route["total_min"] / 60.0 * a["wage_per_hour"] * a["staff_per_run"]
        cost = fuel + staff
        net = food_value * mult - cost

        if net <= 0:
            rejections.append(rules.reject(a["agency_id"], a["name"], "NET_NEGATIVE",
                f"Run costs ${cost:.2f} to collect ${food_value:.2f} of food — "
                f"not viable."))
            continue
        cpm = cost / meals if meals else float("inf")
        if cpm > c["MAX_COST_PER_MEAL"]:
            rejections.append(rules.reject(a["agency_id"], a["name"], "INEFFICIENT",
                f"${cpm:.2f}/meal exceeds the ${c['MAX_COST_PER_MEAL']:.2f} ceiling."))
            continue

        matches.append({
            "agency_id": a["agency_id"], "agency_name": a["name"],
            "simulated": a.get("simulated", False),
            "kind": "mobile" if a["has_mobile_pantry"] else "fixed",
            "lat": a["lat"], "lon": a["lon"],
            "accepts_lbs": round(takeable, 1),
            "declined_lbs": round(qty - takeable, 1),
            "one_way_km": round(route["one_way_km"], 2),
            "round_trip_km": round(route["total_km"], 2),
            "total_min": round(route["total_min"], 1),
            "pickup_at": route["pickup_at"].isoformat(timespec="minutes"),
            "meals": round(meals, 1),
            "gross_food_value": round(gross_value, 2),
            "food_value": round(food_value, 2),
            "freshness": round(fresh, 3),
            "reaches_people_at": s["reach_at"].isoformat(timespec="minutes"),
            "reaches_people_via": s["reach_how"],
            "hours_to_people": round(
                (s["reach_at"] - donation["ready_at"]).total_seconds() / 3600.0, 1),
            "fuel_cost": round(fuel, 2), "personnel_cost": round(staff, 2),
            "transport_cost": round(cost, 2),
            "cost_per_meal": round(cpm, 2),
            "need_now": round(s["need"]["need_now"], 1),
            "need_basis": s["need"]["basis"],
            "need_multiplier": round(mult, 3),
            "net_value": round(net, 2),
            "intake_budget_lbs": b["demand_lbs"],
            "intake_uncapped": b["uncapped_by_demand"],
            "serves_hubspots": a["has_mobile_pantry"],
        })

    matches.sort(key=lambda m: m["net_value"], reverse=True)
    for m in matches:
        m["explanation"] = _explain(m, donation)

    if commit and matches:
        w = matches[0]
        if not w["intake_uncapped"]:
            ledger.commit(w["agency_id"], now, w["accepts_lbs"],
                          donation.get("donor_name", ""), w["agency_id"])
        w["pickup"] = sched.SCHEDULE.add(donation, w, now)

    tax = ec.tax_deduction(qty, c)
    priority = {m["agency_id"] for m in matches}
    result = {
        "leg": "collection",
        "evaluated": len(agencies),
        "feasible": len(matches),
        "quantity_lbs": qty,
        "matches": matches,
        "rejections": rejections,
        "rejection_summary": rules.summarise(rejections, priority),
        "tax": tax,
        "simulated_agency_data": ag_mod.is_simulated(agencies),
    }
    result["headline"] = "" if matches else _no_match_headline(result)
    return result


def _explain(m: dict, donation: dict) -> str:
    parts = [f"{m['agency_name']} collects {m['accepts_lbs']:.0f} lb from "
             f"{donation['donor_name']} — {m['one_way_km']:.1f} km each way, "
             f"{m['total_min']:.0f} min round trip."]
    parts.append(f"Costs the agency ${m['transport_cost']:.2f} "
                 f"(${m['fuel_cost']:.2f} fuel + ${m['personnel_cost']:.2f} staff) "
                 f"for {m['meals']:.0f} meals at ${m['cost_per_meal']:.2f}/meal.")
    if m["kind"] == "fixed":
        parts.append(f"Walk-in pantry serving {m['need_now']:.0f} counted people "
                     f"across {m['need_basis']}.")
    else:
        parts.append(f"Mobile pantry — carries it onward to {m['need_basis']}, "
                     f"{m['need_now']:.0f} people counted.")
    parts.append(f"Reaches someone in {m['hours_to_people']:.0f}h via "
                 f"{m['reaches_people_via']}, retaining {m['freshness'] * 100:.0f}% "
                 f"of its value.")
    if m["declined_lbs"] > 0:
        parts.append(f"Takes {m['accepts_lbs']:.0f} lb of the {donation['quantity_lbs']:.0f} lb "
                     f"offered; the rest would exceed what its walk-in population "
                     f"can eat.")
    parts.append("The restaurant pays nothing.")
    return " ".join(parts)


def _no_match_headline(result: dict) -> str:
    for g in result["rejection_summary"]:
        if g["reason_code"] == "INTAKE_SATURATED":
            return ("Every pantry that could take this has already met its walk-in "
                    "demand for today. Holding it over beats sending it somewhere "
                    "it would be thrown away.")
        if g["reason_code"] == "SPOILS_BEFORE_REACHED":
            return ("This food spoils before any available pantry could get it to "
                    "a person. A mobile pantry running sooner is what it needs.")
        if g["reason_code"] == "NO_WALK_IN_DEMAND":
            return g["example"]
    return result["rejection_summary"][0]["example"] if result["rejection_summary"] else ""
