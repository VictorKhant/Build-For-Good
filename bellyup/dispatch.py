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
    return haversine_mi(a, b) * C["ROAD_FACTOR"]


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
        self.deliveries.append(rec)
        return rec

    def snapshot(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for d in self.deliveries:
            out[d["hotspotId"]] = out.get(d["hotspotId"], 0.0) + d["servedMeals"]
        return out

    def reset(self) -> None:
        self.deliveries.clear()


LEDGER = Ledger()


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
    dropoffs = [a for a in agencies if not a.get("mobileCapable", True)]

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

        leg1 = road_mi(col, supplier)
        arrive_pickup = now + timedelta(minutes=leg1 / C["AVG_SPEED_MPH"] * 60)
        if arrive_pickup > win_to:
            note("PICKUP_WINDOW_MISSED",
                 f"{col['name']} would reach the dock at {arrive_pickup:%H:%M}, "
                 f"after the {win_to:%H:%M} cutoff.")
            continue
        start_load = max(arrive_pickup, win_from)

        collected = min(lbs, col["capacityLbs"])
        uncollected = lbs - collected
        col_meals = collected / C["LBS_PER_MEAL"]

        for h in candidates:
            leg2 = road_mi(supplier, h)
            miles = leg1 + leg2
            drive_min = miles / C["AVG_SPEED_MPH"] * 60
            minutes = drive_min + C["HANDLING_MIN"]
            arrives = start_load + timedelta(
                minutes=leg2 / C["AVG_SPEED_MPH"] * 60 + C["HANDLING_MIN"] / 2)

            limit = C["MAX_TRANSIT_MIN"]["prepared" if prepared else "packaged/produce"]
            if minutes > limit:
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

        leg = road_mi(supplier, site)
        drive_min = leg / C["AVG_SPEED_MPH"] * 60
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


def serialisable(result: dict, top: int = 12) -> dict:
    """Trim the result for the wire: the front end only draws the top pairs."""
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
        "rejections": result["rejections"],
        "window": result["window"],
        "expiresAt": result["expiresAt"],
        "reportedAt": result["reportedAt"],
        "fmv": round(result["fmv"], 2),
    }
