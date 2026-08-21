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
    """Meals already committed to each hotspot tonight.

    A block holds a finite number of people. Without this, every report in the
    feed routes to the same highest-need block and the later ones deliver food
    nobody is there to take.
    """

    def __init__(self) -> None:
        self._served: dict[str, float] = {}
        self.log: list[dict] = []

    def served(self, hotspot_id: str) -> float:
        return self._served.get(hotspot_id, 0.0)

    def remaining(self, hotspot: dict) -> float:
        return max(0.0, hotspot["need"] - self.served(hotspot["id"]))

    def commit(self, hotspot_id: str, meals: float, donor: str, collector: str) -> None:
        self._served[hotspot_id] = self.served(hotspot_id) + meals
        self.log.append({"hotspot_id": hotspot_id, "meals": round(meals, 1),
                         "donor": donor, "collector": collector})

    def snapshot(self) -> dict[str, float]:
        return {k: round(v, 1) for k, v in self._served.items() if v > 0}

    def reset(self) -> None:
        self._served.clear()
        self.log.clear()


LEDGER = Ledger()


def collectors(agencies: list[dict], pantries: list[dict]) -> list[dict]:
    """Agency box trucks plus mobile pantry units with staff on site tonight.

    Capacity is what separates them: a pantry van handles the nearby long tail,
    an agency truck handles bulk.
    """
    out = [{**a, "kind": "agency", "capacityLbs": C["AGENCY_CAPACITY_LBS"]}
           for a in agencies]
    out += [{**p, "kind": "pantry", "capacityLbs": C["PANTRY_CAPACITY_LBS"]}
            for p in pantries if p["dispatchable"]]
    return out


def compute(supplier: dict, agencies: list[dict], pantries: list[dict],
            hotspots: list[dict], now: datetime, ledger: Ledger | None = None,
            commit: bool = False) -> dict:
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

            room = ledger.remaining(h)
            if room <= 0:
                note("BLOCK_NEED_MET",
                     f"{h['location']} has already been served its "
                     f"{h['need']:.0f} person-equivalents tonight.")
                continue

            fresh = freshness_factor(reported_at, expires_at, arrives)
            served = min(col_meals, room)
            surplus = col_meals - served
            boost = 1 + C["ACCESS_BOOST_MAX"] * (7 - min(h["accessDays"], 7)) / 7

            labor = minutes / 60 * C["WAGE_PER_HR"]
            mileage = miles * C["COST_PER_MILE"]
            cost = labor + mileage
            reward = (served * C["MEAL_VALUE"] * boost * fresh
                      + surplus * C["MEAL_VALUE"] * 0.5)

            pairs.append({
                "collector": col, "hotspot": h,
                "collectedLbs": collected, "uncollectedLbs": uncollected,
                "leg1": leg1, "leg2": leg2, "miles": miles,
                "driveMin": drive_min, "minutes": minutes,
                "labor": labor, "mileage": mileage, "cost": cost,
                "served": served, "surplus": surplus, "boost": boost,
                "freshness": fresh, "reward": reward, "net": reward - cost,
                "arrivesAt": arrives.strftime("%H:%M"),
                "pickupAt": start_load.strftime("%H:%M"),
                "hoursToPeople": round((arrives - reported_at).total_seconds() / 3600, 1),
            })

    pairs.sort(key=lambda p: -p["net"])

    if commit and pairs:
        b = pairs[0]
        ledger.commit(b["hotspot"]["id"], b["served"],
                      supplier["name"], b["collector"]["name"])

    return {
        "meals": meals, "prepared": prepared,
        "eligible": [c for c in all_collectors if not prepared or c["acceptsPrepared"]],
        "collectorCount": len(all_collectors),
        "candidateCount": len(candidates),
        "pairs": pairs,
        "evaluated": len(pairs),
        "rejections": sorted(rejected.values(), key=lambda r: -r["count"]),
        "window": {"from": win_from.strftime("%H:%M"), "to": win_to.strftime("%H:%M")},
        "expiresAt": expires_at.strftime("%H:%M"),
        "reportedAt": reported_at.strftime("%H:%M"),
        "fmv": lbs * C["FMV_PER_LB"],
    }


COL_KEYS = ("id", "name", "kind", "lat", "lon", "program", "capacityLbs",
            "acceptsPrepared", "operator")
HS_KEYS = ("id", "location", "area", "lat", "lon", "need", "rank", "accessDays")


def serialisable(result: dict, top: int = 12) -> dict:
    """Trim the result for the wire: the front end only draws the top pairs."""
    def one(p):
        base = {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in p.items() if k not in ("collector", "hotspot")}
        base["collector"] = {k: p["collector"][k] for k in COL_KEYS if k in p["collector"]}
        base["hotspot"] = {k: p["hotspot"][k] for k in HS_KEYS}
        return base

    return {
        "meals": round(result["meals"], 1),
        "prepared": result["prepared"],
        "eligibleCount": len(result["eligible"]),
        "collectorCount": result["collectorCount"],
        "candidateCount": result["candidateCount"],
        "evaluated": result["evaluated"],
        "pairs": [one(p) for p in result["pairs"][:top]],
        "rejections": result["rejections"],
        "window": result["window"],
        "expiresAt": result["expiresAt"],
        "reportedAt": result["reportedAt"],
        "fmv": round(result["fmv"], 2),
    }
