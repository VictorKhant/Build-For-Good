"""The merged dispatch engine: collector x hotspot, scored on reward minus cost.

Two models met here.

**The reward/cost model** answers *who needs it most*: a block's reward is
weighted up when its scheduled food access is poor, and meals beyond what the
block can absorb still earn half credit because they stock the collector's own
network. Cost is labour plus mileage over base -> pickup -> hotspot.

**The freshness and window model** answers *can it still be eaten when it
arrives*. A restaurant now states an expiry and a pickup window, so time is
part of the problem: a hotel tray with four hours of life cannot wait for a
collector that arrives after it, and value decays with the share of the food's
life already spent.

    net = reward - cost
    reward = served x MEAL_VALUE x accessBoost x freshness
             + surplus x MEAL_VALUE x 0.5
    cost   = (drive + handling) x WAGE_PER_HR + road miles x COST_PER_MILE

Hard constraints reject a pair outright, each with a reason the donor can act
on. A hotspot also carries a demand ledger: a block only holds so many people,
so once tonight's need is met further deliveries there are refused rather than
left on a pavement.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import routing
from demo_data import CONSTANTS as C

R_EARTH_MI = 3958.76


def haversine_mi(a: dict, b: dict) -> float:
    rad = math.pi / 180
    dlat = (b["lat"] - a["lat"]) * rad
    dlon = (b["lon"] - a["lon"]) * rad
    s = (math.sin(dlat / 2) ** 2
         + math.cos(a["lat"] * rad) * math.cos(b["lat"] * rad) * math.sin(dlon / 2) ** 2)
    return 2 * R_EARTH_MI * math.asin(math.sqrt(s))


def road_mi(a: dict, b: dict) -> float:
    """Road miles from a to b -- *directed*, because downtown is.

    Front and First are a one-way pair; a route out and the same route back
    are not the same distance, and a straight line cannot express that. This
    reads the precomputed OSRM matrix (see routing.py) and falls back to the
    old haversine x ROAD_FACTOR estimate only when the graph has nothing to
    say, so every caller keeps its shape and its speed.
    """
    return routing.mi(a, b)


def road_min(a: dict, b: dict) -> float:
    """Driving minutes from a to b, at the speeds OSRM assigns those streets
    rather than one flat city average."""
    return routing.minutes(a, b)


def road_leg(a: dict, b: dict) -> tuple[float, float]:
    """(miles, minutes) in a single lookup.

    Time is no longer distance / AVG_SPEED_MPH: a mile of Harbor Drive and a
    mile of Gaslamp are not the same mile, and the pickup-window and expiry
    checks are decided in minutes.
    """
    return routing.leg(a, b)


def run_cost(kind: str, miles: float, minutes: float, cfg: dict | None = None) -> dict:
    """What a run actually costs, in its three real parts.

      fuel     miles / mpg x price per gallon -- what goes in the tank
      vehicle  miles x wear rate -- maintenance, tyres, insurance, depreciation
      labour   minutes x wage x crew, because a 2,000 lb box truck run needs
               two people and a pantry van needs one

    Kept in one place so a routed run and a drop-off cannot be costed on
    different assumptions.
    """
    c = cfg or C
    mpg = c["MPG"].get(kind, c["MPG"]["pantry"])
    wear = c["WEAR_PER_MILE"].get(kind, c["WEAR_PER_MILE"]["pantry"])
    crew = c["STAFF_PER_RUN"].get(kind, 1)

    fuel = miles / mpg * c["FUEL_PRICE_PER_GAL"]
    vehicle = miles * wear
    labour = minutes / 60 * c["WAGE_PER_HR"] * crew
    return {"fuel": fuel, "vehicle": vehicle, "labor": labour, "crew": crew,
            "mileage": fuel + vehicle,          # what the old field meant
            "total": fuel + vehicle + labour}


def _clock(t: str, base: datetime) -> datetime:
    """'19:25' on the evening `base` belongs to, rolling past midnight."""
    h, m = (int(x) for x in t.split(":"))
    out = base.replace(hour=h, minute=m, second=0, microsecond=0)
    if out < base - timedelta(hours=6):
        out += timedelta(days=1)
    return out


def freshness_factor(reported: datetime, expires: datetime, arrives: datetime) -> float:
    """Share of the food's value still intact when it reaches a person.

    1.0 at the moment it is reported, falling toward FRESHNESS_FLOOR as its
    usable life is consumed. This is what makes a four-hour hotel tray behave
    differently from bakery goods with two days on them, without hard-coding a
    preference for either.
    """
    life = (expires - reported).total_seconds()
    if life <= 0:
        return C["FRESHNESS_FLOOR"]
    used = max(0.0, (arrives - reported).total_seconds())
    left = max(0.0, min(1.0, 1.0 - used / life))
    return C["FRESHNESS_FLOOR"] + (1.0 - C["FRESHNESS_FLOOR"]) * left


class Ledger:
    """Tonight's confirmed deliveries.

    Two limits come out of this, and they are different questions:

      meals   a block holds a finite number of people, so once its need is met
              further food there is food left on a pavement
      drops   MAX_DROPS_PER_NIGHT -- nobody sends five separate vans to one
              corner in an evening, however much need is left

    A dispatch is a recommendation until it is CONFIRMED. Only confirmed runs
    enter the ledger, which is why the board shows a proposal first and books
    it second.
    """

    def __init__(self) -> None:
        self.deliveries: list[dict] = []

    # -- serving limits -------------------------------------------------
    def served_meals(self, hotspot_id: str) -> float:
        if hotspot_id is None:
            return 0.0
        return sum(d["servedMeals"] for d in self.deliveries
                   if d["hotspotId"] == hotspot_id)

    def drops(self, hotspot_id: str) -> int:
        # a drop-off has no hotspot, so it must never count against a block
        if hotspot_id is None:
            return 0
        return sum(1 for d in self.deliveries if d["hotspotId"] == hotspot_id)

    def remaining(self, hotspot: dict) -> float:
        return max(0.0, hotspot["need"] - self.served_meals(hotspot["id"]))

    def is_closed(self, hotspot: dict, cfg: dict) -> tuple[bool, str]:
        if self.drops(hotspot["id"]) >= cfg["MAX_DROPS_PER_NIGHT"]:
            return True, "drops"
        if self.remaining(hotspot) < 1:
            return True, "need_met"
        return False, ""

    def dispatched_supplier_ids(self) -> set[str]:
        return {d["supplierId"] for d in self.deliveries}

    # -- booking --------------------------------------------------------
    def confirm(self, supplier: dict, pair: dict, cfg: dict, when) -> dict:
        """Book a run. A drop-off has no hotspot, so it consumes no block's
        nightly capacity -- it stocks a pantry instead."""
        n = len(self.deliveries) + 1
        hs = pair.get("hotspot")
        rec = {
            "receipt": f"BU-{cfg['DEMO_DATE'].replace('-', '')}-T{n:02d}",
            "date": cfg["DEMO_DATE"],
            "time": when.strftime("%H:%M"),
            "supplierId": supplier["id"], "supplier": supplier["name"],
            "lbs": supplier["report"]["lbs"],
            "collectedLbs": round(pair["collectedLbs"]),
            "servedMeals": round(pair["served"]),
            "surplusMeals": round(pair["surplus"]),
            "collector": pair["collector"]["name"],
            "kind": pair["collector"]["kind"],
            "hotspotId": hs["id"] if hs else None,
            "hotspot": hs["location"] if hs else pair["collector"]["name"],
            "dropoff": hs is None,
            "fmv": round(supplier["report"]["lbs"] * cfg["FMV_PER_LB"], 2),
            "net": round(pair["net"], 2),
        }
        rec["deferred"] = bool(pair.get("deferred"))
        rec["deliversAt"] = pair.get("deliversAt")
        if rec["deferred"]:
            # Delivered on the next run, not tonight, so it must not consume
            # tonight's capacity for this block -- the block still has its full
            # need available to other donors this evening. (The next night's
            # ledger is out of scope: this one is per-evening.)
            rec["hotspotId"] = None
        self.deliveries.append(rec)
        return rec

    def confirm_run(self, plan: dict, suppliers: list[dict], cfg: dict,
                    when) -> list[dict]:
        """Book a combined run: one receipt per donor, so each keeps its own
        deduction record, with the served meals split by the share of the load
        each one contributed.

        Blocks are credited on the first stop each donor's food reaches. That
        is an attribution choice, not a measurement -- a combined run mixes
        pallets, and no one pallet belongs to one block.
        """
        # Only what actually went on the vehicle. A donor whose pallet was
        # left behind for capacity has not donated anything yet, and must not
        # be handed a receipt for it.
        taken = {p["id"]: p["lbs"] for p in plan["pickups"]}
        collected = [s for s in suppliers if s["id"] in taken]
        total_lbs = sum(taken.values()) or 1.0
        served = plan["servedMeals"]
        stop = plan["stops"][0] if plan["stops"] else None
        out = []
        for s in collected:
            share = taken[s["id"]] / total_lbs
            n = len(self.deliveries) + 1
            rec = {
                "receipt": f"BU-{cfg['DEMO_DATE'].replace('-', '')}-T{n:02d}",
                "date": cfg["DEMO_DATE"], "time": when.strftime("%H:%M"),
                "supplierId": s["id"], "supplier": s["name"],
                "lbs": s["report"]["lbs"],
                "collectedLbs": round(taken[s["id"]]),
                "servedMeals": round(served * share),
                "surplusMeals": round(plan["leftoverMeals"] * share),
                "collector": plan["collector"]["name"],
                "kind": plan["collector"].get("kind", "agency"),
                "hotspotId": stop["id"] if stop else None,
                "hotspot": stop["location"] if stop else plan["collector"]["name"],
                "fmv": round(taken[s["id"]] * cfg["FMV_PER_LB"], 2),
                "net": round(plan["net"] * share, 2),
                "deferred": False, "deliversAt": None,
                "combined": True, "runSize": len(collected),
            }
            self.deliveries.append(rec)
            out.append(rec)
        return out

    def snapshot(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for d in self.deliveries:
            out[d["hotspotId"]] = out.get(d["hotspotId"], 0.0) + d["servedMeals"]
        return out

    def reset(self) -> None:
        self.deliveries.clear()


LEDGER = Ledger()


def tonight_close(collector: dict, now: datetime) -> datetime:
    """When this crew stands down tonight.

    Every collector in the pool is already flagged as running tonight, so the
    bound is the evening cutoff -- outreach hands food out into the evening,
    but not indefinitely. A published window that ends LATER than the cutoff
    wins, since that site is demonstrably still open.

    This is what makes a late report defer rather than fail: a hotel reporting
    at 22:24 cannot have food carried to a block the same night, so it goes
    back to the agency and out on the next scheduled run instead.
    """
    hh, mm = (int(x) for x in C["EVENING_CUTOFF"].split(":"))
    close = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    end = collector.get("endTime") if collector.get("kind") == "pantry" else None
    if end:
        try:
            eh, em = (int(x) for x in end.split(":"))
            published = now.replace(hour=eh, minute=em, second=0, microsecond=0)
            if published > close:
                close = published
        except ValueError:
            pass
    return close


def collectors(agencies: list[dict], pantries: list[dict]) -> list[dict]:
    """Agency box trucks plus mobile pantry units with staff on site tonight.

    Capacity is what separates them: a pantry van handles the nearby long tail,
    an agency truck handles bulk.
    """
    # mobile_capable = no means a fixed drop-off site, not a fleet
    out = [{**a, "kind": "agency", "capacityLbs": C["AGENCY_CAPACITY_LBS"]}
           for a in agencies if a.get("mobileCapable", True)]
    out += [{**p, "kind": "pantry", "capacityLbs": C["PANTRY_CAPACITY_LBS"]}
            for p in pantries if p["dispatchable"]]
    return out


def compute(supplier: dict, agencies: list[dict], pantries: list[dict],
            hotspots: list[dict], now: datetime,
            ledger: Ledger | None = None) -> dict:
    """Rank every (collector, hotspot) pair for one surplus report."""
    ledger = LEDGER if ledger is None else ledger
    rep = supplier["report"]
    lbs = float(rep["lbs"])
    meals = lbs / C["LBS_PER_MEAL"]
    prepared = supplier["surplus"] == "prepared"

    reported_at = _clock(rep.get("time", "17:00"), now)
    expires_at = (_clock(rep["expiresAt"], reported_at) if rep.get("expiresAt")
                  else reported_at + timedelta(hours=float(rep.get("expiresInHours", 12))))
    if expires_at <= reported_at:
        expires_at += timedelta(days=1)
    win_from = _clock(rep.get("pickupFrom") or rep.get("time", "17:00"), reported_at)
    win_to = _clock(rep.get("pickupTo") or "23:59", win_from)
    if win_to <= win_from:
        win_to += timedelta(days=1)

    candidates = [h for h in hotspots if h["need"] >= C["MIN_CANDIDATE_NEED"]]
    all_collectors = collectors(agencies, pantries)
    dropoffs = dropoff_sites(agencies)

    # Fetch anything the shipped matrix has never seen -- a restaurant that
    # registered ten minutes ago -- in a few bulk calls, before the scoring
    # loop starts. Inside the loop every lookup must be an array index: this
    # is thousands of pairs, and one HTTP request each would not be a board.
    routing.warm([supplier, *all_collectors, *candidates, *dropoffs])

    # one lookup per collector, not per (collector x block) pair
    import demo_data as _dd
    next_run = {c["id"]: _dd.next_run_datetime(c, now) for c in all_collectors}

    pairs: list[dict] = []
    rejected: dict[str, dict] = {}

    def note(code: str, msg: str) -> None:
        r = rejected.setdefault(code, {"reason_code": code, "count": 0, "example": msg})
        r["count"] += 1

    for col in all_collectors:
        if prepared and not col["acceptsPrepared"]:
            note("NO_PREPARED_HANDLING",
                 f"{col['name']} is not set up to accept prepared food.")
            continue

        leg1, leg1_min = road_leg(col, supplier)
        arrive_pickup = now + timedelta(minutes=leg1_min)
        if arrive_pickup > win_to:
            note("PICKUP_WINDOW_MISSED",
                 f"{col['name']} would reach the dock at {arrive_pickup:%H:%M}, "
                 f"after the {win_to:%H:%M} cutoff.")
            continue
        start_load = max(arrive_pickup, win_from)

        collected = min(lbs, col["capacityLbs"])
        uncollected = lbs - collected
        col_meals = collected / C["LBS_PER_MEAL"]

        # the crew has to get home; a one-way route is not a run
        for h in candidates:
            leg2, leg2_min = road_leg(supplier, h)
            leg_home, home_min = road_leg(h, col)
            back_to_base, base_min = road_leg(supplier, col)

            # ---- mode A: straight out tonight, base -> donor -> block -> base
            miles = leg1 + leg2 + leg_home
            drive_min = leg1_min + leg2_min + home_min
            minutes = drive_min + C["HANDLING_MIN"]
            arrives = start_load + timedelta(
                minutes=leg2_min + C["HANDLING_MIN"] / 2)
            direct_ok = arrives <= tonight_close(col, now)
            deferred = False

            # ---- mode B: hold it and deliver on the next run
            # If the block cannot be reached before the crew stands down, the
            # food goes back to the agency and out on its next scheduled run --
            # but only if it is still good by then.
            if not direct_ok:
                nxt = next_run.get(col["id"])
                if nxt is None:
                    note("NO_NEXT_RUN",
                         f"{col['name']} has no further scheduled run to carry "
                         f"this on.")
                    continue
                if (expires_at - nxt).total_seconds() / 60 < C["SAFETY_MARGIN_MIN"]:
                    note("EXPIRES_BEFORE_NEXT_RUN",
                         f"{col['name']} could not reach {h['location']} before "
                         f"standing down tonight, and its next run is "
                         f"{nxt:%a %H:%M} — after this food expires at "
                         f"{expires_at:%H:%M}.")
                    continue
                deferred = True
                out_mi, out_min = road_leg(col, h)
                arrives = nxt + timedelta(minutes=out_min)
                # tonight: base -> donor -> base.  next run: base -> block -> base
                miles = leg1 + back_to_base + out_mi + leg_home
                drive_min = leg1_min + base_min + out_min + home_min
                minutes = drive_min + C["HANDLING_MIN"] + C["HOLD_HANDLING_MIN"]

            limit = C["MAX_TRANSIT_MIN"]["prepared" if prepared else "packaged/produce"]
            if not deferred and minutes > limit:
                note("TRANSIT_TOO_LONG",
                     f"{minutes:.0f} min run exceeds the {limit} min safe window "
                     f"for {'prepared' if prepared else 'packaged'} food.")
                continue

            margin = (expires_at - arrives).total_seconds() / 60
            if margin < C["SAFETY_MARGIN_MIN"]:
                when = ("after" if margin < 0
                        else f"only {int(margin)} min before")
                note("EXPIRES_BEFORE_SERVED",
                     f"Would reach {h['location']} at {arrives:%H:%M}, {when} "
                     f"the {expires_at:%H:%M} expiry.")
                continue

            closed, why = ledger.is_closed(h, C)
            if closed:
                if why == "drops":
                    note("BLOCK_DROP_LIMIT",
                         f"{h['location']} has already had "
                         f"{C['MAX_DROPS_PER_NIGHT']} deliveries tonight — "
                         f"the serving limit for one block.")
                else:
                    note("BLOCK_NEED_MET",
                         f"{h['location']} has been served its "
                         f"{h['need']:.0f} person-equivalents tonight.")
                continue
            room = ledger.remaining(h)

            fresh = freshness_factor(reported_at, expires_at, arrives)
            served = min(col_meals, room)
            surplus = col_meals - served
            boost = 1 + C["ACCESS_BOOST_MAX"] * (7 - min(h["accessDays"], 7)) / 7

            rc = run_cost(col["kind"], miles, minutes)
            labor, mileage, cost = rc["labor"], rc["mileage"], rc["total"]
            reward = (served * C["MEAL_VALUE"] * boost * fresh
                      + surplus * C["MEAL_VALUE"] * 0.5)

            # A run has to pay for itself. Sorting by net descending is not a
            # viability test -- without this the best of a bad set still wins,
            # and a 2 lb donation gets a -$4.39 "recommendation".
            if reward - cost <= 0:
                note("NET_NEGATIVE",
                     f"{col['name']} to {h['location']} costs ${cost:.2f} to "
                     f"deliver ${reward:.2f} of food — not worth the run.")
                continue

            pairs.append({
                "collector": col, "hotspot": h,
                "deferred": deferred,
                "deliversAt": arrives.strftime("%a %H:%M") if deferred else None,
                "returnMi": round(leg_home, 2),
                "remaining": round(room, 1),
                "collectedLbs": collected, "uncollectedLbs": uncollected,
                "leg1": leg1, "leg2": leg2, "miles": miles,
                "driveMin": drive_min, "minutes": minutes,
                "labor": labor, "mileage": mileage, "cost": cost,
                "fuel": rc["fuel"], "vehicle": rc["vehicle"], "crew": rc["crew"],
                "served": served, "surplus": surplus, "boost": boost,
                "freshness": fresh, "reward": reward, "net": reward - cost,
                "arrivesAt": arrives.strftime("%H:%M"),
                "pickupAt": start_load.strftime("%H:%M"),
                "hoursToPeople": round((arrives - reported_at).total_seconds() / 3600, 1),
            })

    # ---------------------------------------------------------------- drop-offs
    # A fixed-site agency has no vehicle and runs no route: food is brought to
    # it and people come to the pantry. So it is ONE leg, restaurant to agency,
    # and there is no hotspot to serve tonight.
    #
    # It is credited at DROPOFF_CREDIT, the same rate Oscar's model already
    # gives overflow meals that "ride along to the pantry network" -- because
    # that is exactly what this is. Stocking a pantry is worth less than
    # feeding a counted block tonight, so a drop-off only outranks a hotspot
    # run when that run genuinely was not worth making.
    for site in dropoffs:
        if prepared and not site.get("acceptsPrepared"):
            note("NO_PREPARED_HANDLING",
                 f"{site['name']} is not set up to accept prepared food.")
            continue

        leg, drive_min = road_leg(supplier, site)
        minutes = drive_min + C["HANDLING_MIN"]
        arrive = now + timedelta(minutes=drive_min)
        if arrive > win_to:
            note("PICKUP_WINDOW_MISSED",
                 f"{site['name']} could not take this before the "
                 f"{win_to:%H:%M} cutoff.")
            continue
        handover = max(arrive, win_from) + timedelta(minutes=C["HANDLING_MIN"] / 2)
        if (expires_at - handover).total_seconds() / 60 < C["SAFETY_MARGIN_MIN"]:
            note("EXPIRES_BEFORE_SERVED",
                 f"Would reach {site['name']} at {handover:%H:%M}, too close to "
                 f"the {expires_at:%H:%M} expiry.")
            continue

        meals_here = lbs / C["LBS_PER_MEAL"]
        fresh = freshness_factor(reported_at, expires_at, handover)
        rc = run_cost("dropoff", leg, minutes)
        labor, mileage, cost = rc["labor"], rc["mileage"], rc["total"]
        reward = meals_here * C["MEAL_VALUE"] * C["DROPOFF_CREDIT"] * fresh
        net = reward - cost

        # A drop-off has to pay for itself too. Moving food to a pantry that
        # costs more to reach than the food is worth is not a rescue.
        if net <= 0:
            note("DROPOFF_NOT_WORTH_IT",
                 f"{site['name']} is {leg:.1f} mi away — the run costs "
                 f"${cost:.2f} to hand over ${reward:.2f} of food.")
            continue

        pairs.append({
            "collector": {**site, "kind": "dropoff",
                          "capacityLbs": lbs},
            "hotspot": None,
            "dropoff": True,
            "deferred": False,          # a drop-off is handed over on arrival
            "deliversAt": None,
            "returnMi": 0.0,
            "remaining": None,
            "collectedLbs": lbs, "uncollectedLbs": 0.0,
            "leg1": leg, "leg2": 0.0, "miles": leg,
            "driveMin": drive_min, "minutes": minutes,
            "labor": labor, "mileage": mileage, "cost": cost,
            "fuel": rc["fuel"], "vehicle": rc["vehicle"], "crew": rc["crew"],
            "served": meals_here, "surplus": 0.0, "boost": 1.0,
            "freshness": fresh, "reward": reward, "net": net,
            "arrivesAt": handover.strftime("%H:%M"),
            "pickupAt": max(arrive, win_from).strftime("%H:%M"),
            "hoursToPeople": round((handover - reported_at).total_seconds() / 3600, 1),
        })

    pairs.sort(key=lambda p: -p["net"])

    return {
        "meals": meals, "prepared": prepared,
        "eligible": [c for c in all_collectors if not prepared or c["acceptsPrepared"]],
        "collectorCount": len(all_collectors),
        "dropoffCount": len(dropoffs),
        "routedCount": sum(1 for p in pairs if not p.get("dropoff")),
        "dropoffOptions": sum(1 for p in pairs if p.get("dropoff")),
        "candidateCount": len(candidates),
        "pairs": pairs,
        "evaluated": len(pairs),
        "rejections": sorted(rejected.values(), key=lambda r: -r["count"]),
        "alreadyDispatched": supplier["id"] in ledger.dispatched_supplier_ids(),
        "window": {"from": win_from.strftime("%H:%M"), "to": win_to.strftime("%H:%M")},
        "expiresAt": expires_at.strftime("%H:%M"),
        "reportedAt": reported_at.strftime("%H:%M"),
        "fmv": lbs * C["FMV_PER_LB"],
    }


COL_KEYS = ("id", "name", "kind", "lat", "lon", "program", "capacityLbs",
            "acceptsPrepared", "operator", "schedule")
HS_KEYS = ("id", "location", "area", "lat", "lon", "need", "rank", "accessDays")


def _collector_rank(pairs: list[dict]) -> list[dict]:
    """Distinct collectors, ranked by their own best pairing."""
    best: dict[str, dict] = {}
    for p in pairs:
        c = p["collector"]
        if p["net"] > best.get(c["id"], {"net": float("-inf")})["net"]:
            best[c["id"]] = {"id": c["id"], "name": c["name"],
                             "net": round(p["net"], 2),
                             "miles": round(p["leg1"], 2),
                             "dropoff": bool(p.get("dropoff"))}
    return sorted(best.values(), key=lambda r: -r["net"])


def route_for(pair: dict, supplier: dict) -> dict:
    """The streets one recommendation would actually be driven along.

    base -> restaurant -> block for a routed run; restaurant -> site for a
    drop-off, which is one leg because the food stops there and people come
    to it. Leg order matches what the board colours: collection first,
    distribution second.
    """
    if pair.get("dropoff") or not pair.get("hotspot"):
        return routing.path([supplier, pair["collector"]])
    return routing.path([pair["collector"], supplier, pair["hotspot"]])


def serialisable(result: dict, top: int = 12, supplier: dict | None = None) -> dict:
    """Trim the result for the wire: the front end only draws the top pairs.

    Geometry is fetched for the winner alone. Twelve routes is twelve
    round trips to draw eleven lines nobody is looking at.
    """
    def one(p):
        base = {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in p.items() if k not in ("collector", "hotspot")}
        base["collector"] = {k: p["collector"][k] for k in COL_KEYS if k in p["collector"]}
        base["hotspot"] = ({k: p["hotspot"][k] for k in HS_KEYS}
                           if p["hotspot"] else None)
        return base

    return {
        "meals": round(result["meals"], 1),
        "alreadyDispatched": result["alreadyDispatched"],
        "prepared": result["prepared"],
        "eligibleCount": len(result["eligible"]),
        "collectorCount": result["collectorCount"],
        "candidateCount": result["candidateCount"],
        "dropoffCount": result["dropoffCount"],
        "routedCount": result["routedCount"],
        "dropoffOptions": result["dropoffOptions"],
        "evaluated": result["evaluated"],
        "pairs": [one(p) for p in result["pairs"][:top]],
        # Every viable collector, best-first, one row each. `pairs` is trimmed
        # to the top few AND holds one row per (collector, block), so it can
        # answer neither "how many collectors could take this" nor "where does
        # the one we asked rank" -- counting its rows called a winner "#1 of
        # 40" and its truncation called a third-placed collector "#3 of 4".
        "collectorRank": _collector_rank(result["pairs"]),
        "geometry": (route_for(result["pairs"][0], supplier)
                     if supplier and result["pairs"] else None),
        "routing": routing.status(),
        "rejections": result["rejections"],
        "window": result["window"],
        "expiresAt": result["expiresAt"],
        "reportedAt": result["reportedAt"],
        "fmv": round(result["fmv"], 2),
    }


# --------------------------------------------------------------------------
# combining several accepted pickups into one run
# --------------------------------------------------------------------------

EXACT_ORDER_LIMIT = 8   # 8! = 40,320 orderings, still instant


def _greedy_order(pool, bounds, collector, now, c, leg):
    """Window-aware nearest neighbour, for baskets too large to enumerate.

    At each step take the nearest pickup whose window is still open on arrival;
    if none is, take the one closing soonest and record the miss. Approximate
    by construction -- it is the fallback, not the method.
    """
    remaining = list(range(len(pool)))
    order, missed = [], []
    at, t = collector, now

    while remaining:
        reachable = []
        for i in remaining:
            d, m = road_leg(at, pool[i])
            arrive = t + timedelta(minutes=m)
            opens, closes = bounds[pool[i]["id"]]
            reachable.append((arrive <= closes, d, closes, i, arrive))
        # prefer feasible, then nearest; if none feasible, whichever shuts first
        reachable.sort(key=lambda r: (not r[0], r[1] if r[0] else r[2]))
        ok, d, _closes, i, arrive = reachable[0]
        if not ok:
            missed.append(pool[i]["name"])
        opens, _ = bounds[pool[i]["id"]]
        t = max(arrive, opens) + timedelta(minutes=c["HANDLING_MIN"] / 2)
        at = pool[i]
        order.append(i)
        remaining.remove(i)

    return order, missed


def combine_run(collector: dict, suppliers: list[dict], hotspots: list[dict],
                now: datetime, ledger: "Ledger | None" = None,
                cfg: dict | None = None, max_stops: int = 3) -> dict:
    """One vehicle, several pickups, then drop-offs at the blocks that need it.

    The pickup order is solved exactly by trying every permutation. With the
    handful of jobs one van takes in an evening that is a few hundred routes,
    so there is no heuristic here to defend -- it is the shortest order, not an
    approximation of it.

    Deliveries are then assigned greedily: the block with the best
    need-weighted value per mile of detour goes first, up to what it can absorb
    tonight, and on down until the van is empty or nothing is worth another
    stop. Capacity is the vehicle's, so a van will leave food behind and say so.
    """
    from itertools import permutations

    c = cfg or C
    ledger = LEDGER if ledger is None else ledger
    if not suppliers:
        return {"feasible": False, "reason": "nothing accepted yet"}

    kind = collector.get("kind", "agency")
    cap = collector.get("capacityLbs", c["AGENCY_CAPACITY_LBS"])

    # Same reason as compute(): the order search below is exhaustive, so every
    # distance it asks for has to already be in memory.
    routing.warm([collector, *suppliers, *hotspots])

    # --- which pickups fit, in what order -------------------------------
    # Fill the vehicle. Smallest first, so a van takes as many donors as it
    # can rather than being blocked by one pallet it cannot lift; the last one
    # aboard may be a PARTIAL take, which is what a real collection does and
    # what the single-pickup path already allowed. What is left over is named,
    # not silently dropped.
    loaded, left_behind, running = [], [], 0.0
    partial = None
    for s in sorted(suppliers, key=lambda x: x["report"]["lbs"]):
        lbs = float(s["report"]["lbs"])
        room = cap - running
        if room <= 0:
            left_behind.append({"supplier": s, "lbs": lbs})
            continue
        take = min(lbs, room)
        if take < 1:
            left_behind.append({"supplier": s, "lbs": lbs})
            continue
        loaded.append({**s, "_take": take})
        running += take
        if take < lbs:
            partial = {"name": s["name"], "took": round(take, 1),
                       "of": lbs, "leaves": round(lbs - take, 1)}
    if not loaded:
        return {"feasible": False,
                "reason": f"nothing selected fits the {cap:.0f} lb capacity "
                          f"of this vehicle"}

    def leg(a, b):
        return road_mi(a, b)

    # The shortest order is not automatically a legal one: each donor has a
    # window, and a route that reaches a loading dock after it closes is not a
    # route. Feasible orders win outright; among them, the shortest. Only if
    # nothing is fully feasible do we fall back to fewest missed windows, and
    # say which ones.
    def window_bounds(sup):
        rep = sup["report"]
        f = _clock(rep.get("pickupFrom") or rep.get("time", "17:00"), now)
        t = _clock(rep.get("pickupTo") or "23:59", f)
        if t <= f:
            t += timedelta(days=1)
        return f, t

    # Every loaded pickup is ordered -- none is dropped. Truncating the pool
    # silently lost the 7th pickup onward from a run the driver had already
    # agreed to collect, which is worse than ordering it imperfectly.
    pool = loaded
    bounds = {s["id"]: window_bounds(s) for s in pool}

    def evaluate(order):
        pts = [pool[i] for i in order]
        dist, t, missed = 0.0, now, []
        prev = collector
        for x in pts:
            d, m = road_leg(prev, x)
            dist += d
            t = t + timedelta(minutes=m)
            opens, closes = bounds[x["id"]]
            if t > closes:
                missed.append(x["name"])
            t = max(t, opens) + timedelta(minutes=c["HANDLING_MIN"] / 2)
            prev = x
        return dist, missed

    # 8! = 40,320 orderings is still instant, and one vehicle taking more than
    # eight pickups in an evening is beyond what this demo models. Above that,
    # fall back to a window-aware nearest-neighbour pass: not provably optimal,
    # but it orders every pickup instead of discarding some.
    if len(pool) <= EXACT_ORDER_LIMIT:
        best = None
        for order in permutations(range(len(pool))):
            dist, missed = evaluate(order)
            key = (len(missed), dist)
            if best is None or key < best[0]:
                best = (key, list(order), missed)
        best_order, missed_windows = best[1], best[2]
    else:
        best_order, missed_windows = _greedy_order(pool, bounds, collector,
                                                  now, c, leg)
    pickups = [pool[i] for i in best_order]

    # --- deliveries -----------------------------------------------------
    meals = running / c["LBS_PER_MEAL"]
    at = pickups[-1]
    stops, remaining_meals = [], meals
    # A fixed drop-off site has no distribution round. The crew drives out for
    # the food and brings it back to its own building, where people walk in for
    # it -- routing it on to hotspots would invent exactly the vehicle and the
    # round that `mobile_capable = no` says it does not have. So it runs the
    # pickup legs only, and every meal is credited at DROPOFF_CREDIT, which the
    # reward block below already applies to whatever is not delivered to a
    # block. Same treatment the single-donation path gives a drop-off.
    dropoff_run = kind == "dropoff"
    open_blocks = [] if dropoff_run else [
        h for h in hotspots
        if h["need"] >= c["MIN_CANDIDATE_NEED"]
        and not ledger.is_closed(h, c)[0]]

    while remaining_meals >= 1 and len(stops) < max_stops and open_blocks:
        scored = []
        for h in open_blocks:
            room = ledger.remaining(h) - sum(x["meals"] for x in stops
                                             if x["id"] == h["id"])
            if room < 1:
                continue
            detour = leg(at, h)
            boost = 1 + c["ACCESS_BOOST_MAX"] * (7 - min(h["accessDays"], 7)) / 7
            served = min(remaining_meals, room)
            scored.append((served * c["MEAL_VALUE"] * boost / max(detour, 0.1),
                           served, detour, h))
        if not scored:
            break
        scored.sort(key=lambda t: -t[0])
        _, served, detour, h = scored[0]
        stops.append({"id": h["id"], "location": h["location"], "area": h["area"],
                      "lat": h["lat"], "lon": h["lon"],
                      "meals": round(served, 1),
                      "lbs": round(served * c["LBS_PER_MEAL"], 1),
                      "need": h["need"], "accessDays": h["accessDays"],
                      "detourMi": round(detour, 2)})
        remaining_meals -= served
        at = h
        open_blocks = [x for x in open_blocks if x["id"] != h["id"]]

    if not stops and not dropoff_run:
        return {"feasible": False,
                "reason": "every block within reach has been served tonight"}

    # --- put the chosen blocks in the shortest legal order ---------------
    # WHICH blocks to serve is a value question, and the greedy pass above
    # answers it: each block earns its place on meals per mile of detour,
    # against what the ledger says it can still absorb. In WHAT ORDER to
    # visit them is a distance question, and greedy answers that badly --
    # picking the best next block each time is exactly how a route ends up
    # crossing itself, driving out to a block, back past the last one, and
    # out again.
    #
    # So the set stays as chosen and the order is re-solved exhaustively over
    # the tour last-pickup -> blocks -> base. At most MAX_STOPS blocks, so
    # this is 6 orderings at three stops and 24 at four: optimal, not
    # heuristic, and there is no TSP approximation to defend on stage.
    if len(stops) > 1:
        start, home = pickups[-1], collector
        best_seq, best_dist = None, None
        for order in permutations(range(len(stops))):
            seq = [stops[i] for i in order]
            d = road_mi(start, seq[0])
            for a, b in zip(seq, seq[1:]):
                d += road_mi(a, b)
            d += road_mi(seq[-1], home)
            if best_dist is None or d < best_dist:
                best_seq, best_dist = seq, d
        # detourMi was measured against the greedy visit order, which no
        # longer exists. Restate it against the order actually driven.
        prev = start
        for st in best_seq:
            st["detourMi"] = round(road_mi(prev, st), 2)
            prev = st
        stops = best_seq

    # --- cost the whole thing ------------------------------------------
    # One chain, base -> every pickup -> every block -> base, so the miles
    # costed and the geometry drawn on the map are the same route. Time is
    # summed per leg from the road graph rather than divided out of the total
    # at one flat speed.
    route_pts = [collector, *pickups]
    if stops:
        route_pts += stops
    route_pts.append(collector)          # the crew has to get home

    miles = drive = 0.0
    for a, b in zip(route_pts, route_pts[1:]):
        d, m = road_leg(a, b)
        miles += d
        drive += m

    n_stops = len(pickups) + len(stops)
    minutes = drive + c["HANDLING_MIN"] * n_stops / 2
    rc = run_cost(kind, miles, minutes, c)

    served_total = sum(x["meals"] for x in stops)
    # (viability is checked below, once the reward is known)
    reward = sum(x["meals"] * c["MEAL_VALUE"]
                 * (1 + c["ACCESS_BOOST_MAX"] * (7 - min(x["accessDays"], 7)) / 7)
                 for x in stops)
    leftover = max(0.0, meals - served_total)
    reward += leftover * c["MEAL_VALUE"] * c["DROPOFF_CREDIT"]

    # What the same pickups would have cost as separate runs: each is its own
    # base -> donor -> block -> base. Only meaningful once there is more than
    # one, so a single-pickup "combination" reports no saving rather than a
    # rounding artefact.
    solo = 0.0
    # Where a solo run would turn round: the first block for a routed run, the
    # site itself for a drop-off, which makes each solo trip a plain round trip.
    first_stop = ({"lat": stops[0]["lat"], "lon": stops[0]["lon"]} if stops
                  else collector)
    for s in pickups:
        one = one_min = 0.0
        for a, b in ((collector, s), (s, first_stop), (first_stop, collector)):
            d, m = road_leg(a, b)
            one += d
            one_min += m
        solo += run_cost(kind, one, one_min + c["HANDLING_MIN"], c)["total"]
    if len(pickups) < 2:
        solo = rc["total"]

    if reward - rc["total"] <= 0:
        return {"feasible": False,
                "reason": f"the run costs ${rc['total']:.2f} to move "
                          f"${reward:.2f} of food — not worth making"}

    # the legs out to the first pickup and home from the last drop carry no
    # food; worth naming, because a depot far from downtown spends most of its
    # miles on them
    # On a drop-off run the leg home is the loaded one -- the food is on the
    # vehicle until it is unloaded at the site -- so only the run out to the
    # first donor is empty.
    deadhead = leg(collector, pickups[0])
    if stops:
        deadhead += road_mi(stops[-1], collector)

    return {
        "feasible": True,
        # The actual streets, in the solved order -- what the board draws.
        "geometry": routing.path(route_pts),
        "routing": routing.status(),
        "deadheadMi": round(deadhead, 2),
        "workingMi": round(miles - deadhead, 2),
        "collector": {k: collector[k] for k in
                      ("id", "name", "kind", "lat", "lon", "capacityLbs")
                      if k in collector},
        "pickups": [{"id": s["id"], "name": s["name"], "address": s.get("address", ""),
                     "lat": s["lat"], "lon": s["lon"],
                     "lbs": round(s.get("_take", s["report"]["lbs"]), 1),
                     "offeredLbs": s["report"]["lbs"],
                     "window": f"{s['report'].get('pickupFrom','')}"
                               f"–{s['report'].get('pickupTo','')}",
                     "items": s["report"].get("items", "")} for s in pickups],
        "stops": stops,
        "leftBehind": [{"id": x["supplier"]["id"], "name": x["supplier"]["name"],
                        "lbs": x["lbs"]} for x in left_behind],
        "partial": partial,
        "missedWindows": missed_windows,
        "loadedLbs": round(running, 1),
        "capacityLbs": cap,
        "meals": round(meals, 1),
        "servedMeals": round(served_total, 1),
        "leftoverMeals": round(leftover, 1),
        "miles": round(miles, 2),
        "minutes": round(minutes, 1),
        "fuel": round(rc["fuel"], 2), "vehicle": round(rc["vehicle"], 2),
        "labor": round(rc["labor"], 2), "crew": rc["crew"],
        "cost": round(rc["total"], 2),
        "reward": round(reward, 2),
        "net": round(reward - rc["total"], 2),
        "soloCost": round(solo, 2),
        "saved": round(solo - rc["total"], 2),
    }


# --------------------------------------------------------------------------
# who each report is addressed to
# --------------------------------------------------------------------------

def dropoff_sites(agencies: list[dict]) -> list[dict]:
    """Agencies that can RECEIVE food at a building of their own.

    Owning a van does not stop a warehouse being a warehouse. This used to be
    `not mobileCapable`, which read "can drive" as "cannot be a destination"
    and barred both real food banks: Feeding San Diego, four road miles from a
    donor and set up for prepared food, could not be sent 40 lb, while five
    churches eleven miles further away could. Every agency the loader keeps has
    a geocoded site -- transport-only operators with no building (A.B. Jones)
    are dropped for having no address -- so a fixed site is what is left.

    Being reachable as a drop-off does not make one preferable: a drop-off is
    still credited at DROPOFF_CREDIT, so it only wins when no run to a counted
    block was worth making.
    """
    return list(agencies)


def request_targets(agencies: list[dict], pantries: list[dict]) -> list[dict]:
    """Everyone who can be asked to collect.

    Wider than collectors(): a fixed drop-off site cannot run a distribution
    route, but it can send someone for a pickup and hold the food for people
    who walk in. So it can receive a request; it just never gets a hotspot leg.
    """
    out = collectors(agencies, pantries)
    have = {c["id"] for c in out}
    out += [{**a, "kind": "dropoff", "capacityLbs": a.get("intakeLbs", 400)}
            for a in dropoff_sites(agencies) if a["id"] not in have]
    return out


def assign_targets(suppliers: list[dict], agencies: list[dict],
                   pantries: list[dict], hotspots: list[dict], now: datetime,
                   ledger: "Ledger | None" = None,
                   cfg: dict | None = None) -> dict[str, str]:
    """One collector per report, spread so nobody opens an empty board.

    Ranking each report independently sends them all to whichever one or two
    collectors happen to score best -- measured on this data, Father Joe's took
    6 and Feeding San Diego (South Bay) 5, while four of eight collectors got
    nothing at all. Half the agencies would open an empty board, which is worse
    than the crowding it replaced.

    The first version answered that greedily: each report, taken in turn, went
    to its LEAST-LOADED viable collector with net value only as a tie-break.
    That is the same shape as early rideshare dispatch -- one request at a
    time, first come first served -- and it fails the same way. A report
    decided early takes a collector that a later report needed far more, and
    nothing downstream can undo it. Measured on tonight's board it gave up
    $345 of net value and drove no fewer miles than the version below, at the
    same spread: the imbalance was not buying the balance it cost.

    So the whole evening is solved AT ONCE instead. Build the full
    report x collector value matrix, then find the assignment maximising total
    net value -- an optimal bipartite matching, the batched-matching approach
    rideshare dispatch moved to for exactly this reason.

    Balance enters as a CONVEX PENALTY rather than a hard first key. A
    collector's second report costs ASSIGN_LOAD_PENALTY of net value, its third
    twice that, its nth (n-1) times. The solver then spends imbalance only
    where it is cheap, instead of treating one idle collector as worth any
    detour at all. The penalty is a real dial with a plain meaning: what one
    report's worth of crowding is worth in dollars.

    There is still no fixed per-agency cap, because a cap cannot be honoured:
    17 of the 24 reports are prepared food and only three collectors accept
    prepared food at all, so eight reports have exactly one viable collector.
    Capping that collector would not spread those reports, it would refuse
    them. The penalty bends; a cap would break.

    Optimal, and it needs no solver dependency -- scipy is already here, and
    24 reports x 13 collectors is about a millisecond. If scipy is ever
    missing, the greedy pass is kept below as a fallback so the board still
    assigns rather than failing.
    """
    c = cfg or C
    targets = request_targets(agencies, pantries)
    by_id = {t["id"]: t for t in targets}

    # net value of each (report, collector) pairing
    options: dict[str, list[tuple[float, str]]] = {}
    for s in suppliers:
        if not s.get("report"):
            continue
        r = compute(s, agencies, pantries, hotspots, now, ledger)
        best: dict[str, float] = {}
        for p in r["pairs"]:
            cid = p["collector"]["id"]
            if cid in by_id and p["net"] > best.get(cid, float("-inf")):
                best[cid] = p["net"]
        if best:
            options[s["id"]] = sorted(((v, k) for k, v in best.items()), reverse=True)

    if not options:
        return {}

    solved = _match_batch(options, [t["id"] for t in targets],
                          float(c.get("ASSIGN_LOAD_PENALTY", 15.0)))
    return solved if solved is not None else _match_greedy(options, targets)


def _match_batch(options: dict[str, list[tuple[float, str]]],
                 collector_ids: list[str], penalty: float) -> dict[str, str] | None:
    """Optimal report -> collector matching over the whole evening at once.

    Each collector becomes N interchangeable SLOTS, where N is the number of
    reports, so nothing is capped. Slot k of a collector is worth
    `net - penalty * k`, which is what makes crowding cost something without
    forbidding it: taking a second report from the same collector is allowed,
    it just has to be worth more than the penalty.

    Because slots of one collector are identical apart from that penalty, the
    solver always fills them in order -- it can never pick slot 3 while slot 1
    sits empty, since slot 1 scores strictly higher. So the kth slot really is
    the kth report.

    Returns None if scipy is unavailable, so the caller can fall back.
    """
    try:
        import numpy as np
        from scipy.optimize import linear_sum_assignment
    except ImportError:
        return None

    sids = list(options)
    col_of = {cid: j for j, cid in enumerate(collector_ids)}
    n_rows, n_cols = len(sids), len(collector_ids)

    # Value of every (report, collector) pair; -INF where the pair is not
    # viable at all. A finite sentinel, not float("-inf"): the solver needs
    # arithmetic on these, and an infeasible cell simply has to lose to every
    # feasible one.
    INFEASIBLE = -1e9
    value = np.full((n_rows, n_cols), INFEASIBLE, dtype=float)
    for i, sid in enumerate(options):
        for net, cid in options[sid]:
            j = col_of.get(cid)
            if j is not None:
                value[i, j] = net

    # widen to slots
    slots = np.repeat(value, n_rows, axis=1)
    step = np.tile(np.arange(n_rows, dtype=float), n_cols)
    slots = np.where(slots <= INFEASIBLE, INFEASIBLE, slots - penalty * step)
    owner = np.repeat(np.arange(n_cols), n_rows)

    # linear_sum_assignment minimises, so hand it the negated value
    rows, cols = linear_sum_assignment(-slots)

    out: dict[str, str] = {}
    for i, k in zip(rows, cols):
        if slots[i, k] <= INFEASIBLE:
            continue          # no viable collector for this report
        out[sids[i]] = collector_ids[owner[k]]
    return out


def _match_greedy(options: dict[str, list[tuple[float, str]]],
                  targets: list[dict]) -> dict[str, str]:
    """The original pass, kept only for the case where scipy is missing.

    Least-loaded viable collector, best net as the tie-break, reports taken in
    order of what they have to gain. Not optimal -- see assign_targets -- but
    it assigns every report, which beats not answering.
    """
    load: dict[str, int] = {t["id"]: 0 for t in targets}
    assigned: dict[str, str] = {}
    for sid in sorted(options, key=lambda x: -options[x][0][0]):
        cid = min(options[sid], key=lambda t: (load[t[1]], -t[0]))[1]
        assigned[sid] = cid
        load[cid] += 1
    return assigned
