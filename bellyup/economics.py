"""Cost and benefit model for a single pickup-and-delivery run.

Every tunable lives in CONFIG so it can be moved live during the demo, and
every one of them has an entry in CONFIG_SOURCES. A judge will ask where the
numbers came from; "we picked it" is a bad answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from needs import haversine_km

CONFIG: dict = {
    # --- routing ---
    "AVG_SPEED_KMH": 25.0,
    "ROAD_FACTOR": 1.35,          # haversine -> road distance
    "LOAD_MIN": 15.0,
    "UNLOAD_MIN": 10.0,
    "INCLUDE_RETURN_LEG": True,   # the crew has to get back to HQ

    # --- cost ---
    "COST_PER_KM": 0.43,          # fuel + wear
    "WAGE_PER_HOUR": 22.00,       # loaded

    # --- benefit ---
    "LBS_PER_MEAL": 1.2,
    "VALUE_PER_MEAL": 3.50,

    # --- freshness: how long food waits before a person eats it ---
    "FIXED_PANTRY_DWELL_HOURS": 18.0,   # walk-in traffic is not instant
    "MOBILE_HANDOUT_MIN": 20.0,         # handed out on arrival at the hubspot
    "FRESHNESS_FLOOR": 0.35,            # value retained by shelf-stable food
    "MOBILE_PRIORITY_BONUS": 0.0,       # blunt thumb on the scale, if wanted

    # --- donor tax incentive (IRC 170(e)(3) enhanced deduction) ---
    "FOOD_COST_BASIS_PER_LB": 1.50,    # what the food cost the restaurant
    "FOOD_FMV_PER_LB": 3.00,           # fair market value of the same food

    # --- demand side (supply must not exceed what a site can absorb) ---
    "MEALS_PER_PERSON_PER_DAY": 1.0,   # recovered meals per person per day
    "DEMAND_HORIZON_DAYS": 90,         # project need forward when budgeting
    "MOBILE_RANGE_KM": 8.0,            # how far a mobile pantry will range
    "WALK_IN_RADIUS_M": 800.0,         # how far someone will walk to a pantry
    "MAX_STOPS": 3,                    # dropoffs a driver will make in one run
    "MIN_STOP_LBS": 15.0,              # below this a detour is not worth making

    # --- need weighting ---
    "SERVICE_RADIUS_M": 300.0,    # destination catchment; live-tunable
    "ALPHA_NEED": 0.6,            # weight on current need
    "BETA_TREND": 0.4,            # weight on rising need -- the forward-looking term

    # --- viability guards ---
    "MAX_COST_PER_MEAL": 2.00,
    "MIN_QUANTITY_LBS": 10.0,

    # --- safety ---
    "MAX_TRANSIT_MIN": {"hot": 120, "refrigerated": 240, "frozen": 240, "ambient": 480},
    "SAFETY_MARGIN_MIN": 30.0,
}

# verified=False means: real source, but the exact current figure still needs a
# human to confirm it before this goes on a slide.
CONFIG_SOURCES: dict[str, dict] = {
    "AVG_SPEED_KMH": {
        "value": 25.0, "verified": False,
        "source": "Urban arterial average speed, downtown San Diego surface streets.",
        "note": "Sanity band is 20-30 km/h for stop-and-go downtown driving with parking.",
    },
    "ROAD_FACTOR": {
        "value": 1.35, "verified": False,
        "source": "Circuity factor: road distance / straight-line distance.",
        "note": "1.3-1.4 is the standard range for dense grid street networks. Downtown "
                "SD is a regular grid, so it sits at the low end.",
    },
    "LOAD_MIN": {
        "value": 15.0, "verified": False,
        "source": "Operational estimate: park, weigh, sign the SB 1383 transfer record, load.",
    },
    "UNLOAD_MIN": {
        "value": 10.0, "verified": False,
        "source": "Operational estimate: unload and log receipt at destination.",
    },
    "COST_PER_KM": {
        "value": 0.43, "verified": False,
        "source": "IRS standard mileage rate for business use, 2025: $0.70/mile.",
        "note": "$0.70/mile / 1.609 km/mile = $0.435/km. The IRS rate bundles fuel, "
                "maintenance, tyres, insurance and depreciation, which is exactly the "
                "'fuel + wear' this term is meant to carry.",
    },
    "WAGE_PER_HOUR": {
        "value": 22.00, "verified": False,
        "source": "Loaded hourly cost of a San Diego nonprofit driver/outreach worker.",
        "note": "CA minimum wage is $16.50/hr (2025) and San Diego nonprofit driver "
                "postings run $18-22/hr base; 'loaded' adds payroll tax and benefits. "
                "Confirm against a real org's rate before the demo.",
    },
    "LBS_PER_MEAL": {
        "value": 1.2, "verified": False,
        "source": "Feeding America conversion: 1.2 lbs of food = 1 meal.",
        "note": "The standard figure used across the Feeding America network for "
                "reporting pounds distributed as meals provided.",
    },
    "VALUE_PER_MEAL": {
        "value": 3.50, "verified": False,
        "source": "USDA Thrifty Food Plan per-meal cost, adjusted for a high-cost metro.",
        "note": "Feeding America's Map the Meal Gap reports a San Diego County average "
                "meal cost in this band. Confirm the current year's figure.",
    },
    "FIXED_PANTRY_DWELL_HOURS": {
        "value": 18.0, "verified": False,
        "source": "Average time donated food sits at a walk-in pantry before a "
                  "client takes it.",
        "note": "The core asymmetry. A mobile pantry hands food to a person on "
                "arrival; a walk-in pantry waits for the person to come. For "
                "packaged dry goods that wait costs nothing. For food out of a "
                "restaurant kitchen it is the whole problem.",
    },
    "MOBILE_HANDOUT_MIN": {
        "value": 20.0, "verified": True,
        "source": "Time from mobile pantry arrival at a hubspot to food in hands.",
    },
    "FRESHNESS_FLOOR": {
        "value": 0.35, "verified": True,
        "source": "Modelling choice: the share of value food keeps even when it "
                  "reaches people late in its life.",
        "note": "Stops the freshness term zeroing out shelf-stable donations that "
                "are still perfectly good on day four.",
    },
    "MOBILE_PRIORITY_BONUS": {
        "value": 0.0, "verified": True,
        "source": "Optional flat preference for mobile pantries, on top of the "
                  "freshness model.",
        "note": "Left at 0 deliberately. Mobile pantries should win because "
                "restaurant food spoils and they move it faster -- a modelled "
                "reason that survives 'why not always use mobile?'. Raise it only "
                "if you want a preference the data does not justify.",
    },
    "FOOD_COST_BASIS_PER_LB": {
        "value": 1.50, "verified": False,
        "source": "Restaurant food cost per pound -- the donor's tax basis.",
        "note": "Restaurant food cost typically runs 28-35% of menu price. This is "
                "the number an individual restaurant should override with its own "
                "figure; the deduction is computed from basis, so it matters.",
    },
    "FOOD_FMV_PER_LB": {
        "value": 3.00, "verified": False,
        "source": "Fair market value per pound of prepared/fresh food donated.",
        "note": "Used only for the enhanced-deduction calculation, which credits "
                "half the appreciation over basis.",
    },
    "MEALS_PER_PERSON_PER_DAY": {
        "value": 1.0, "verified": False,
        "source": "Policy assumption: recovered surplus supplies one meal per "
                  "counted person per day.",
        "note": "Deliberately conservative. Recovered food supplements existing "
                "services rather than replacing all three meals, so budgeting one "
                "meal avoids over-routing. Raise it and every budget scales linearly.",
    },
    "DEMAND_HORIZON_DAYS": {
        "value": 90, "verified": True,
        "source": "Modelling choice: budget against need projected a quarter ahead "
                  "rather than need on the last count night.",
        "note": "A block whose count is climbing earns a larger budget before the "
                "people arrive. This is the forecast entering the supply side.",
    },
    "MOBILE_RANGE_KM": {
        "value": 8.0, "verified": False,
        "source": "Operational range of a mobile pantry run from its base.",
        "note": "Bounds which hubspots count toward a mobile agency's service need. "
                "Not a hard constraint on routing -- the cost model handles distance "
                "-- just the reach credited when ranking agencies.",
    },
    "WALK_IN_RADIUS_M": {
        "value": 800.0, "verified": False,
        "source": "Walking access to a food source: half a mile, roughly a ten "
                  "minute walk.",
        "note": "A DIFFERENT question from SERVICE_RADIUS_M. That one asks which "
                "blocks to attribute to a site for scoring, and 300 m is right there "
                "because downtown blocks are tiny. This one asks how far a person "
                "will actually walk carrying food back, which is further. Applies to "
                "fixed pantries only -- a mobile pantry travels to people.",
    },
    "MAX_STOPS": {
        "value": 3, "verified": False,
        "source": "Operational limit on dropoffs per run.",
        "note": "Each stop adds UNLOAD_MIN plus a detour. Three keeps a run inside a "
                "driver's shift and inside the cold-chain window.",
    },
    "MIN_STOP_LBS": {
        "value": 15.0, "verified": True,
        "source": "Policy floor: a detour must deliver enough to justify itself.",
        "note": "Stops below this are pruned even when the destination has budget.",
    },
    "SERVICE_RADIUS_M": {
        "value": 300.0, "verified": True,
        "source": "Catchment radius, calibrated to this block grid rather than taken "
                  "from the spec's 800 m.",
        "note": "Downtown SD blocks are ~90x60 m. At 800 m each destination sweeps "
                "120-190 of the 382 cells, so the score measures centrality, not "
                "catchment, and the ranking inverts. 300 m is a 3-4 minute walk and "
                "gives ~36 blocks per destination. Tunable live.",
    },
    "ALPHA_NEED": {
        "value": 0.6, "verified": True,
        "source": "Model weight, not an empirical constant. Tunable at demo time.",
        "note": "Weight on normalised current need in the need multiplier.",
    },
    "BETA_TREND": {
        "value": 0.4, "verified": True,
        "source": "Model weight, not an empirical constant. Tunable at demo time.",
        "note": "Weight on normalised rising need. Setting it to 0 collapses the model "
                "to present-need-only -- that is demo scenario 3.",
    },
    "MAX_COST_PER_MEAL": {
        "value": 2.00, "verified": False,
        "source": "Policy ceiling: transport cost must stay well under the $3.50 value "
                  "of the meal being moved.",
        "note": "An organisation would set this itself. It is a spend rule, not a "
                "measurement.",
    },
    "MIN_QUANTITY_LBS": {
        "value": 10.0, "verified": False,
        "source": "Policy floor: below ~8 meals a dedicated vehicle run cannot pay for "
                  "itself at any distance.",
    },
    "MAX_TRANSIT_MIN": {
        "value": {"hot": 120, "refrigerated": 240, "frozen": 240, "ambient": 480},
        "verified": False,
        "source": "FDA Food Code time/temperature control for safety (TCS) limits.",
        "note": "The Food Code allows TCS food out of temperature control for up to 4 "
                "hours; hot prepared food is held to 2 here as a deliberately "
                "conservative margin. Ambient dry goods are bounded by the shift, not "
                "by safety.",
    },
    "SAFETY_MARGIN_MIN": {
        "value": 30.0, "verified": True,
        "source": "Operational buffer: food must land 30 min before stated expiry so "
                  "the destination has time to serve or store it.",
    },
    "INCLUDE_RETURN_LEG": {
        "value": True, "verified": True,
        "source": "Modelling choice. The crew and vehicle return to HQ, so the org pays "
                  "for three legs, not two. Excluding it understates real cost by "
                  "roughly a third.",
    },
}


@dataclass
class Route:
    to_pickup_km: float
    to_dropoff_km: float
    return_km: float
    total_km: float
    drive_min: float
    total_min: float
    arrival_at: datetime

    def as_dict(self) -> dict:
        return {
            "to_pickup": round(self.to_pickup_km, 2),
            "to_dropoff": round(self.to_dropoff_km, 2),
            "return": round(self.return_km, 2),
            "total": round(self.total_km, 2),
        }


def build_route(org, donation, dest, now: datetime, cfg: dict | None = None) -> Route:
    """Three legs: HQ -> donor, donor -> destination, destination -> HQ.

    Straight line scaled by ROAD_FACTOR. No routing API: offline, deterministic,
    no key, no rate limit, nothing that can fail on stage.
    """
    c = cfg or CONFIG
    rf = c["ROAD_FACTOR"]

    to_pickup = haversine_km(org["hq_lat"], org["hq_lon"], donation["lat"], donation["lon"]) * rf
    to_dropoff = haversine_km(donation["lat"], donation["lon"], dest["lat"], dest["lon"]) * rf
    ret = haversine_km(dest["lat"], dest["lon"], org["hq_lat"], org["hq_lon"]) * rf
    if not c["INCLUDE_RETURN_LEG"]:
        ret = 0.0

    total_km = to_pickup + to_dropoff + ret
    drive_min = total_km / c["AVG_SPEED_KMH"] * 60.0
    total_min = drive_min + c["LOAD_MIN"] + c["UNLOAD_MIN"]

    ready = donation["ready_at"]
    start = max(ready, now)
    to_dest_min = (to_pickup + to_dropoff) / c["AVG_SPEED_KMH"] * 60.0 + c["LOAD_MIN"]
    arrival = start + timedelta(minutes=to_dest_min)

    return Route(to_pickup, to_dropoff, ret, total_km, drive_min, total_min, arrival)


def transport_cost(route: Route, org, cfg: dict | None = None) -> dict:
    c = cfg or CONFIG
    fuel = route.total_km * c["COST_PER_KM"]
    personnel = (route.total_min / 60.0) * c["WAGE_PER_HOUR"] * org.get("staff_per_run", 1)
    return {"fuel_cost": fuel, "personnel_cost": personnel, "transport_cost": fuel + personnel}


def food_value(quantity_lbs: float, cfg: dict | None = None) -> dict:
    c = cfg or CONFIG
    meals = quantity_lbs / c["LBS_PER_MEAL"]
    return {"meals": meals, "food_value": meals * c["VALUE_PER_MEAL"]}


def normalise(value: float, lo: float, hi: float) -> float:
    """Min-max across the candidate destinations for this donation.

    A flat field (every candidate equally needy) returns 0 rather than 0.5, so
    the multiplier collapses to 1.0 and need stops influencing the ranking --
    which is the honest answer when need carries no information.
    """
    if hi <= lo:
        return 0.0
    return (value - lo) / (hi - lo)


def need_multiplier(norm_now: float, norm_trend: float, cfg: dict | None = None) -> float:
    """1 + a*now + b*trend.

    This is the merge. Pure cost minimisation sends food wherever is cheapest to
    reach -- systematically the destinations nearest a nonprofit's HQ, whoever
    happens to need it. The multiplier turns the objective from cost into
    cost-effectiveness, and BETA_TREND is where the forecast enters.
    """
    c = cfg or CONFIG
    return 1.0 + c["ALPHA_NEED"] * norm_now + c["BETA_TREND"] * norm_trend


def score(quantity_lbs: float, route: Route, org, multiplier: float,
          cfg: dict | None = None) -> dict:
    c = cfg or CONFIG
    cost = transport_cost(route, org, c)
    value = food_value(quantity_lbs, c)
    weighted = value["food_value"] * multiplier
    return {
        **cost,
        **value,
        "need_multiplier": multiplier,
        "weighted_value": weighted,
        "net_value": weighted - cost["transport_cost"],
        "cost_per_meal": cost["transport_cost"] / value["meals"] if value["meals"] else float("inf"),
    }


# --------------------------------------------------------------------------
# multi-stop routing
# --------------------------------------------------------------------------

@dataclass
class MultiRoute:
    """HQ -> donor -> stop1 -> ... -> stopN -> HQ."""
    to_pickup_km: float
    leg_km: list[float]          # donor->stop1, stop1->stop2, ...
    return_km: float
    total_km: float
    drive_min: float
    total_min: float
    arrivals: list[datetime]     # one per stop, in visit order

    def as_dict(self) -> dict:
        return {
            "to_pickup": round(self.to_pickup_km, 2),
            "legs": [round(x, 2) for x in self.leg_km],
            "return": round(self.return_km, 2),
            "total": round(self.total_km, 2),
        }


def _route_for_order(org, donation, ordered_dests, now, c) -> MultiRoute:
    rf = c["ROAD_FACTOR"]
    speed = c["AVG_SPEED_KMH"]

    to_pickup = haversine_km(org["hq_lat"], org["hq_lon"],
                             donation["lat"], donation["lon"]) * rf

    legs, arrivals = [], []
    cur_lat, cur_lon = donation["lat"], donation["lon"]
    start = max(donation["ready_at"], now)
    # clock at the moment the vehicle leaves the donor
    t_min = to_pickup / speed * 60.0 + c["LOAD_MIN"]

    for i, d in enumerate(ordered_dests):
        leg = haversine_km(cur_lat, cur_lon, d["lat"], d["lon"]) * rf
        legs.append(leg)
        t_min += leg / speed * 60.0
        arrivals.append(start + timedelta(minutes=t_min))
        t_min += c["UNLOAD_MIN"]
        cur_lat, cur_lon = d["lat"], d["lon"]

    ret = haversine_km(cur_lat, cur_lon, org["hq_lat"], org["hq_lon"]) * rf
    if not c["INCLUDE_RETURN_LEG"]:
        ret = 0.0

    total_km = to_pickup + sum(legs) + ret
    drive_min = total_km / speed * 60.0
    total_min = drive_min + c["LOAD_MIN"] + c["UNLOAD_MIN"] * len(ordered_dests)
    return MultiRoute(to_pickup, legs, ret, total_km, drive_min, total_min, arrivals)


def build_multi_route(org, donation, dests, now: datetime, cfg: dict | None = None):
    """Shortest visit order over the chosen stops.

    At MAX_STOPS = 3 there are at most six orderings, so this is exhaustive
    rather than heuristic -- no TSP approximation to defend.
    """
    from itertools import permutations
    c = cfg or CONFIG
    if not dests:
        return None, []
    best, best_order = None, None
    for order in permutations(range(len(dests))):
        ordered = [dests[i] for i in order]
        r = _route_for_order(org, donation, ordered, now, c)
        if best is None or r.total_km < best.total_km:
            best, best_order = r, order
    return best, list(best_order)


def multi_transport_cost(route: MultiRoute, org, cfg: dict | None = None) -> dict:
    c = cfg or CONFIG
    fuel = route.total_km * c["COST_PER_KM"]
    personnel = (route.total_min / 60.0) * c["WAGE_PER_HOUR"] * org.get("staff_per_run", 1)
    return {"fuel_cost": fuel, "personnel_cost": personnel,
            "transport_cost": fuel + personnel}


# --------------------------------------------------------------------------
# donor tax incentive
# --------------------------------------------------------------------------

def tax_deduction(quantity_lbs: float, cfg: dict | None = None) -> dict:
    """Estimated enhanced charitable deduction under IRC 170(e)(3).

    Small restaurants are NOT covered by SB 1383 -- the mandate reaches Tier One
    and Tier Two generators only -- so nothing legally requires them to donate.
    The deduction is the entire incentive, which is why it is computed and shown.

    The enhanced deduction for donated food inventory is:

        basis + 50% of (fair market value - basis),  capped at 2 x basis

    This is an ESTIMATE from per-pound defaults. A restaurant's real basis is
    its own food cost, and the result depends on entity type and taxable income.
    It is labelled as an estimate everywhere it is shown.
    """
    c = cfg or CONFIG
    basis = quantity_lbs * c["FOOD_COST_BASIS_PER_LB"]
    fmv = quantity_lbs * c["FOOD_FMV_PER_LB"]
    enhanced = basis + 0.5 * max(0.0, fmv - basis)
    deduction = min(enhanced, 2.0 * basis)
    return {
        "cost_basis": round(basis, 2),
        "fair_market_value": round(fmv, 2),
        "deduction_estimate": round(deduction, 2),
        "rule": "IRC 170(e)(3) enhanced deduction for donated food inventory",
        "basis_of_estimate": f"${c['FOOD_COST_BASIS_PER_LB']:.2f}/lb basis, "
                             f"${c['FOOD_FMV_PER_LB']:.2f}/lb FMV",
        "disclaimer": "Estimate only. Confirm with your accountant — the actual "
                      "deduction depends on your food cost, entity type and "
                      "taxable income.",
    }


# --------------------------------------------------------------------------
# freshness -- how much of the food's life is left when a person gets it
# --------------------------------------------------------------------------

def freshness_factor(ready_at, expires_at, reaches_people_at, cfg=None) -> float:
    """Share of the donation's value still intact when it reaches a person.

    1.0 at the moment it is ready, falling toward FRESHNESS_FLOOR as its usable
    life is consumed. This is what makes a mobile pantry the right answer for
    food out of a restaurant kitchen without hard-coding a preference for one:
    prepared food has hours of life, so an 18-hour wait for walk-in traffic
    destroys most of its value, while packaged dry goods barely notice.
    """
    c = cfg or CONFIG
    life = (expires_at - ready_at).total_seconds()
    if life <= 0:
        return c["FRESHNESS_FLOOR"]
    used = (reaches_people_at - ready_at).total_seconds()
    left = max(0.0, min(1.0, 1.0 - used / life))
    return c["FRESHNESS_FLOOR"] + (1.0 - c["FRESHNESS_FLOOR"]) * left
