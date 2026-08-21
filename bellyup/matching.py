"""Feasibility, allocation and ranking.

Every (nonprofit x destination) pair is screened, then each nonprofit is given
its best RUN: one pickup, up to MAX_STOPS dropoffs, with the load split across
them.

Splitting is not a flourish. Scored one donation at a time against a static
world, the engine sends every donation to whichever site scores best -- twenty
downtown restaurants reporting 80 lb each put 1,600 lb into one 350 lb site.
So a destination carries a daily demand budget (see demand.py), a ledger
records what it has already been committed today, and the allocator fills the
highest-value site first, then moves down. Food stops piling up where it would
rot.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import demand as demand_mod
import economics as ec
import needs as needs_mod
from economics import CONFIG

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# condition -> the storage flag a destination must have
STORAGE_FOR = {
    "hot": "hot_holding",
    "refrigerated": "refrigerated",
    "frozen": "frozen",
    "ambient": None,   # dry goods need no special storage
}
COLD_CONDITIONS = {"refrigerated", "frozen"}


def _hhmm(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _week_of_month(d: datetime) -> int:
    return (d.day - 1) // 7 + 1


def _is_last_dow(d: datetime) -> bool:
    """True if this is the last occurrence of its weekday in the month."""
    return (d + timedelta(days=7)).month != d.month


def _window_matches(when: datetime, w: dict) -> bool:
    """Weekly window, optionally restricted to certain weeks of the month.

    Most San Diego Food Bank sites distribute on a monthly cadence -- "1st and
    3rd Thursday" -- not weekly. Flattening that to "every Thursday" would have
    the engine confidently propose runs to sites that are closed three weeks in
    four, so `weeks_of_month` carries the cadence. -1 means "last".
    """
    if w["dow"] != when.weekday():
        return False
    minutes = when.hour * 60 + when.minute
    if not (_hhmm(w["start"]) <= minutes <= _hhmm(w["end"])):
        return False
    weeks = w.get("weeks_of_month")
    if not weeks:
        return True
    return _week_of_month(when) in weeks or (-1 in weeks and _is_last_dow(when))


def _in_windows(when: datetime, windows: list[dict]) -> bool:
    return any(_window_matches(when, w) for w in windows)


def _next_open(when: datetime, windows: list[dict]) -> str:
    """Human-readable next opening, searched over the following week."""
    if not windows:
        return "no published hours"
    for day_offset in range(40):  # monthly cadences can be weeks out
        day = when + timedelta(days=day_offset)
        dow = day.weekday()
        for w in sorted([x for x in windows if x["dow"] == dow], key=lambda x: x["start"]):
            start = day.replace(hour=_hhmm(w["start"]) // 60,
                                minute=_hhmm(w["start"]) % 60,
                                second=0, microsecond=0)
            if start >= when and _window_matches(start, w):
                label = "today" if day_offset == 0 else day.strftime("%a %-d %b")
                return f"{label} {w['start']}"
    return "no published hours"


def _reject(org, dest, code: str, msg: str) -> dict:
    return {
        "org_id": org["org_id"], "org_name": org["name"],
        "dest_id": dest["dest_id"], "dest_name": dest["name"],
        "reason_code": code, "reason": msg,
    }


def evaluate(donation: dict, orgs: list[dict], dests: list[dict],
             now: datetime | None = None, cfg: dict | None = None,
             rescore: bool = True, ledger=None, commit: bool = False) -> dict:
    """Rank one run per nonprofit, each splitting the load across its best stops.

    `ledger` defaults to the process-wide one so repeated donations deplete the
    same budgets. Pass `commit=True` to book the winning run against it.
    """
    c = cfg or CONFIG
    now = now or datetime.now()
    ledger = demand_mod.LEDGER if ledger is None else ledger

    if rescore:
        dests = needs_mod.rescore(dests, c["SERVICE_RADIUS_M"])

    budgets = demand_mod.daily_demand(dests, c)

    # --- screen every pair -------------------------------------------------
    rejections: list[dict] = []
    survivors_by_org: dict[str, list[dict]] = {}
    n_pairs = 0
    for org in orgs:
        keep = []
        for dest in dests:
            n_pairs += 1
            r = _screen(donation, org, dest, now, c, budgets, ledger)
            if isinstance(r, dict) and "reason_code" in r:
                rejections.append(r)
            else:
                keep.append(r)
        survivors_by_org[org["org_id"]] = keep

    # --- need multiplier, normalised across every surviving destination ----
    seen: dict[str, dict] = {}
    for keep in survivors_by_org.values():
        for s in keep:
            seen.setdefault(s["dest"]["dest_id"], s)
    if seen:
        nows = [s["need_now"] for s in seen.values()]
        trends = [s["need_trend"] for s in seen.values()]
        lo_n, hi_n, lo_t, hi_t = min(nows), max(nows), min(trends), max(trends)
    else:
        lo_n = hi_n = lo_t = hi_t = 0.0

    mult = {
        did: ec.need_multiplier(ec.normalise(s["need_now"], lo_n, hi_n),
                                ec.normalise(s["need_trend"], lo_t, hi_t), c)
        for did, s in seen.items()
    }

    # --- one best run per nonprofit ---------------------------------------
    plans = []
    for org in orgs:
        plan = _build_plan(org, donation, survivors_by_org[org["org_id"]],
                           mult, budgets, ledger, now, c)
        if plan is None:
            continue
        if plan["net_value"] <= 0:
            rejections.append(_reject_run(org, "NET_NEGATIVE",
                f"Run costs ${plan['transport_cost']:.2f} to deliver "
                f"${plan['food_value']:.2f} of food — not viable."))
            continue
        if plan["cost_per_meal"] > c["MAX_COST_PER_MEAL"]:
            rejections.append(_reject_run(org, "INEFFICIENT",
                f"${plan['cost_per_meal']:.2f}/meal exceeds the "
                f"${c['MAX_COST_PER_MEAL']:.2f} ceiling."))
            continue
        plans.append(plan)

    plans.sort(key=lambda p: p["net_value"], reverse=True)
    for p in plans:
        p["explanation"] = explain(p, donation, plans)

    if commit and plans:
        ledger.commit_match(plans[0], donation)

    placed = plans[0]["allocated_lbs"] if plans else 0.0
    result = {
        "evaluated": n_pairs,
        "feasible": len(plans),
        "service_radius_m": c["SERVICE_RADIUS_M"],
        "quantity_lbs": donation["quantity_lbs"],
        "allocated_lbs": round(placed, 1),
        "unallocated_lbs": round(donation["quantity_lbs"] - placed, 1),
        "matches": plans,
        "rejections": rejections,
        "rejection_summary": _summarise(
            rejections, {d["dest_id"] for d in dests if d.get("need_now", 0) > 0}),
        "demand": {d["dest_id"]: {**budgets[d["dest_id"]],
                                  "committed_today": round(
                                      ledger.committed(d["dest_id"], now), 1)}
                   for d in dests if budgets[d["dest_id"]]["daily_demand_lbs"] > 0},
    }
    result["headline"] = headline(result)
    return result


def _screen(donation, org, dest, now, c, budgets, ledger):
    """Static + single-stop feasibility. Rejection dict, or a surviving candidate.

    Route-dependent checks here use a single-stop route, which is what the
    rejection list reports. The real multi-stop route is re-validated during
    allocation, so a destination that clears screening can still be left out of
    a three-stop plan because the detour pushed a later arrival past its window.
    """
    qty = donation["quantity_lbs"]
    cond = donation["condition"]
    ftype = donation["food_type"]

    # 1 -- minimum quantity
    if qty < c["MIN_QUANTITY_LBS"]:
        return _reject(org, dest, "QTY_TOO_SMALL",
                       f"Below the {c['MIN_QUANTITY_LBS']:.0f} lb minimum for a dedicated run.")

    # 2 -- destination accepts this food type
    if ftype not in dest.get("accepts", []):
        return _reject(org, dest, "TYPE_NOT_ACCEPTED",
                       f"{dest['name']} does not accept {ftype.replace('_', ' ')}.")

    # 12 -- eligibility. Evaluated here, with the other "does this destination
    # take this?" checks, rather than last: a seniors-only or income-tested site
    # is categorically unavailable to the unsheltered population this routes
    # toward, and saying so is more useful to an operator than reporting
    # whichever downstream distance check it also failed.
    if not dest.get("eligibility_open", True):
        return _reject(org, dest, "ELIGIBILITY_MISMATCH",
                       f"{dest['name']} is restricted to "
                       f"{dest.get('eligibility_label', 'a qualifying group')} — "
                       f"not open intake for unsheltered clients.")

    # 3 -- destination has storage for this condition
    need_storage = STORAGE_FOR.get(cond)
    if need_storage and not dest.get("storage", {}).get(need_storage, False):
        return _reject(org, dest, "NO_STORAGE",
                       f"{dest['name']} has no {cond} storage.")

    # 4 -- cold food needs a cold vehicle
    if cond in COLD_CONDITIONS and not org.get("has_refrigerated_vehicle", False):
        return _reject(org, dest, "NO_COLD_VEHICLE",
                       f"{org['name']} has no refrigerated vehicle.")

    route = ec.build_route(org, donation, dest, now, c)

    # 5 -- cold chain / safe transit window
    limit = c["MAX_TRANSIT_MIN"][cond]
    if route.total_min > limit:
        return _reject(org, dest, "COLD_CHAIN",
                       f"{route.total_min:.0f} min run exceeds the {limit} min safe "
                       f"window for {cond} food.")

    # 6 -- arrives with margin before expiry
    margin_min = (donation["expires_at"] - route.arrival_at).total_seconds() / 60.0
    if margin_min < c["SAFETY_MARGIN_MIN"]:
        if margin_min < 0:
            return _reject(org, dest, "EXPIRES_IN_TRANSIT",
                           f"Arrives {abs(margin_min):.0f} min after expiry.")
        return _reject(org, dest, "EXPIRES_IN_TRANSIT",
                       f"Arrives {margin_min:.0f} min before expiry — under the "
                       f"{c['SAFETY_MARGIN_MIN']:.0f} min margin.")

    # 7 -- destination open on arrival
    if not _in_windows(route.arrival_at, dest.get("open_windows", [])):
        return _reject(org, dest, "DEST_CLOSED",
                       f"{dest['name']} is closed at the "
                       f"{route.arrival_at.strftime('%a %H:%M')} arrival. "
                       f"Next open: {_next_open(route.arrival_at, dest.get('open_windows', []))}.")

    # 8 -- org operating when the run starts
    start = max(donation["ready_at"], now)
    if not _in_windows(start, org.get("operating_windows", [])):
        return _reject(org, dest, "ORG_CLOSED",
                       f"{org['name']} is not operating at {start.strftime('%a %H:%M')}.")

    # 9 -- destination can take a meaningful drop at all. With splitting, a load
    # larger than one site's capacity is divided rather than rejected, so this
    # now catches only the site too small to be worth a stop.
    cap = dest.get("capacity_lbs_per_visit", 0)
    if cap < min(c["MIN_STOP_LBS"], qty):
        return _reject(org, dest, "OVER_CAPACITY",
                       f"{dest['name']}'s {cap:.0f} lb capacity is below the "
                       f"{min(c['MIN_STOP_LBS'], qty):.0f} lb this run would drop.")

    # 13 -- daily demand already met. This is the guard that stops twenty
    # restaurants all routing to the same site and wasting the surplus.
    b = budgets.get(dest["dest_id"], {})
    budget = b.get("daily_demand_lbs", 0.0)
    used = ledger.committed(dest["dest_id"], now)
    if budget <= 0:
        return _reject(org, dest, "NO_MEASURED_DEMAND",
                       f"{dest['name']} serves no blocks in the count grid — "
                       f"no measured demand to route against.")
    floor = min(c["MIN_STOP_LBS"], qty)
    if budget < floor:
        return _reject(org, dest, "DEMAND_TOO_SMALL",
                       f"{dest['name']}'s measured demand is {budget:.0f} lb/day — "
                       f"below the {floor:.0f} lb this run would drop.")
    if budget - used < floor:
        return _reject(org, dest, "DEMAND_SATURATED",
                       f"{dest['name']} has taken {used:.0f} lb of its {budget:.0f} lb "
                       f"daily demand — only {budget - used:.0f} lb left today.")

    return {
        "org": org, "dest": dest, "route": route,
        "need_now": dest.get("need_now", 0.0),
        "need_trend": dest.get("need_trend", 0.0),
        "n_blocks": dest.get("n_blocks", len(dest.get("served_block_ids", []))),
        "served_block_ids": dest.get("served_block_ids", []),
    }


def _build_plan(org, donation, survivors, mult, budgets, ledger, now, c):
    """Greedy marginal-value allocation across up to MAX_STOPS destinations.

    At each step it asks what one more stop is worth: the extra food delivered,
    need-weighted, minus the extra distance and unload time. The moment another
    stop stops paying for itself, the run closes. That is the same question a
    dispatcher asks, and it is why a 400 lb load spreads while an 80 lb load
    stays a single drop.
    """
    if not survivors:
        return None

    by_id = {s["dest"]["dest_id"]: s for s in survivors}
    chosen: list[dict] = []          # [{cand, lbs}]
    remaining = float(donation["quantity_lbs"])
    best_state = None

    # MIN_STOP_LBS governs whether an EXTRA detour is worth making. It must not
    # govern the first stop, or a donation between MIN_QUANTITY_LBS and
    # MIN_STOP_LBS would pass screening and then be silently unallocatable.
    floor = lambda: c["MIN_QUANTITY_LBS"] if not chosen else c["MIN_STOP_LBS"]

    while remaining >= floor() and len(chosen) < c["MAX_STOPS"]:
        best = None
        for did, s in by_id.items():
            if any(x["cand"]["dest"]["dest_id"] == did for x in chosen):
                continue
            avail = min(remaining, _headroom(s["dest"], budgets, ledger, now, c))
            if avail < floor():
                continue

            trial = chosen + [{"cand": s, "lbs": avail}]
            state = _price(org, donation, trial, mult, now, c)
            if state is None:
                continue
            gain = state["net_value"] - (best_state["net_value"] if best_state else 0.0)
            if gain > 0 and (best is None or state["net_value"] > best[1]["net_value"]):
                best = (trial, state, avail)

        if best is None:
            break
        chosen, best_state, took = best[0], best[1], best[2]
        remaining -= took

    if best_state is None:
        return None
    return best_state


def _headroom(dest, budgets, ledger, now, c) -> float:
    """Pounds this destination can still absorb today."""
    b = budgets.get(dest["dest_id"], {})
    budget = b.get("daily_demand_lbs", 0.0)
    return min(ledger.remaining(dest["dest_id"], now, budget),
               dest.get("capacity_lbs_per_visit", budget))


def _price(org, donation, trial, mult, now, c):
    """Cost and value the whole run, validating the real multi-stop route."""
    dests = [x["cand"]["dest"] for x in trial]
    route, order = ec.build_multi_route(org, donation, dests, now, c)
    if route is None:
        return None

    ordered = [trial[i] for i in order]

    # route-dependent safety checks, re-run against the ACTUAL visit order --
    # adding a stop delays every stop after it
    if route.total_min > c["MAX_TRANSIT_MIN"][donation["condition"]]:
        return None
    for stop, arrival in zip(ordered, route.arrivals):
        d = stop["cand"]["dest"]
        if (donation["expires_at"] - arrival).total_seconds() / 60.0 < c["SAFETY_MARGIN_MIN"]:
            return None
        if not _in_windows(arrival, d.get("open_windows", [])):
            return None

    cost = ec.multi_transport_cost(route, org, c)
    meals = food = weighted = 0.0
    stops = []
    for stop, arrival in zip(ordered, route.arrivals):
        d = stop["cand"]["dest"]
        m = stop["lbs"] / c["LBS_PER_MEAL"]
        v = m * c["VALUE_PER_MEAL"]
        w = v * mult.get(d["dest_id"], 1.0)
        meals += m; food += v; weighted += w
        stops.append({
            "dest_id": d["dest_id"], "dest_name": d["name"], "dest_type": d["dest_type"],
            "dest_lat": d["lat"], "dest_lon": d["lon"],
            "lbs": round(stop["lbs"], 1), "meals": round(m, 1),
            "need_now": round(stop["cand"]["need_now"], 1),
            "need_trend": round(stop["cand"]["need_trend"], 1),
            "n_blocks": stop["cand"]["n_blocks"],
            "need_multiplier": round(mult.get(d["dest_id"], 1.0), 3),
            "arrival_at": arrival.isoformat(timespec="minutes"),
        })

    allocated = sum(x["lbs"] for x in trial)
    return {
        "org_id": org["org_id"], "org_name": org["name"],
        "hq_lat": org["hq_lat"], "hq_lon": org["hq_lon"],
        "stops": stops, "n_stops": len(stops),
        "allocated_lbs": round(allocated, 1),
        "route_km": route.as_dict(),
        "total_min": round(route.total_min, 1),
        "drive_min": round(route.drive_min, 1),
        "arrival_at": stops[0]["arrival_at"],
        "meals": round(meals, 1),
        "food_value": round(food, 2),
        "weighted_value": round(weighted, 2),
        "fuel_cost": round(cost["fuel_cost"], 2),
        "personnel_cost": round(cost["personnel_cost"], 2),
        "transport_cost": round(cost["transport_cost"], 2),
        "net_value": round(weighted - cost["transport_cost"], 2),
        "cost_per_meal": round(cost["transport_cost"] / meals, 2) if meals else float("inf"),
    }


def _reject_run(org, code, msg) -> dict:
    return {"org_id": org["org_id"], "org_name": org["name"],
            "dest_id": None, "dest_name": "(whole run)",
            "reason_code": code, "reason": msg}


def explain(p: dict, donation: dict, all_plans: list[dict]) -> str:
    km = p["route_km"]
    parts = [f"{p['org_name']} picks up {p['allocated_lbs']:.0f} lb from "
             f"{donation['donor_name']} — {km['to_pickup']:.1f} km out."]

    if p["n_stops"] == 1:
        s = p["stops"][0]
        parts.append(f"One drop at {s['dest_name']}, {km['legs'][0]:.1f} km on.")
    else:
        legs = " → ".join(f"{s['dest_name']} ({s['lbs']:.0f} lb)" for s in p["stops"])
        parts.append(f"Split across {p['n_stops']} stops: {legs}.")

    parts.append(f"{p['total_min']:.0f} min, ${p['transport_cost']:.2f} in fuel and "
                 f"staff time. {p['meals']:.0f} meals at ${p['cost_per_meal']:.2f}/meal.")

    top = max(p["stops"], key=lambda s: s["lbs"])
    if top["n_blocks"]:
        word = "rising" if top["need_trend"] > 0 else "falling"
        parts.append(f"The largest share goes to {top['dest_name']}, serving "
                     f"{top['n_blocks']} blocks with {top['need_now']:.0f} people "
                     f"and need {word} {abs(top['need_trend']):.0f}/yr.")

    if p["n_stops"] > 1:
        parts.append("Split rather than dropped in one place because no single site's "
                     "daily demand could absorb the load without waste.")
    if all_plans and p is all_plans[0] and len(all_plans) > 1:
        r = all_plans[1]
        parts.append(f"Chosen over {r['org_name']} (${r['net_value']:.2f} net, "
                     f"{r['route_km']['total']:.1f} km).")
    return " ".join(parts)


# How much a reason tells the person holding the food, most useful first.
# Deliberately NOT frequency order: 416 county food banks being seniors-only is
# the commonest reason and the least informative one. What a donor needs to hear
# is "every site that could take this has already had its day's food".
INFORMATIVENESS = [
    "DEMAND_SATURATED", "NET_NEGATIVE", "INEFFICIENT", "EXPIRES_IN_TRANSIT",
    "COLD_CHAIN", "DEST_CLOSED", "ORG_CLOSED", "NO_STORAGE", "NO_COLD_VEHICLE",
    "OVER_CAPACITY", "DEMAND_TOO_SMALL", "QTY_TOO_SMALL", "TYPE_NOT_ACCEPTED",
    "ELIGIBILITY_MISMATCH", "NO_MEASURED_DEMAND",
]


def _summarise(rejections: list[dict], in_grid: set[str] | None = None) -> list[dict]:
    """Grouped by reason code.

    Ordered by how much the reason explains, not by how often it fired, and
    tagged with whether it concerns a destination inside the measured downtown
    grid or one of the county sites that can never win anyway.
    """
    in_grid = in_grid or set()
    by_code: dict[str, list[dict]] = {}
    for r in rejections:
        by_code.setdefault(r["reason_code"], []).append(r)

    rank = {c: i for i, c in enumerate(INFORMATIVENESS)}
    out = []
    for k, v in by_code.items():
        downtown = [x for x in v if x.get("dest_id") in in_grid]
        out.append({
            "reason_code": k,
            "count": len(v),
            "downtown_count": len(downtown),
            "example": (downtown or v)[0]["reason"],
        })
    return sorted(out, key=lambda x: (rank.get(x["reason_code"], 99), -x["count"]))


def headline(result: dict) -> str:
    """One sentence for the donor when nothing could be matched."""
    if result["matches"]:
        return ""
    for g in result["rejection_summary"]:
        if g["downtown_count"]:
            if g["reason_code"] == "DEMAND_SATURATED":
                return ("Every downtown site that could take this has already met its "
                        "measured demand for today. Holding it over is better than "
                        "sending it somewhere it would be thrown away.")
            return g["example"]
    return result["rejection_summary"][0]["example"] if result["rejection_summary"] else ""
