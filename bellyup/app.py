"""FastAPI surface. Three roles, three different views of the same engine.

    /api/restaurant/*   post surplus, get matched, see the tax estimate
    /api/agency/*       hubspots, scheduled pickups, intake limit, distribution
    /api/pantries       public: where a person can get food

The role split is not cosmetic. Hubspots are served ONLY under /api/agency.
They are block-level locations where unsheltered people gather, and publishing
them would hand anyone -- including someone looking to move people on -- a map
of where to find them. The public and restaurant views never see them.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import agencies as ag_mod
import collection
import demand as demand_mod
import distribution
import geocode as geo
import needs as needs_mod
import pantry_finder
import pipeline
import scenarios
import schedule as sched
from economics import CONFIG, CONFIG_SOURCES, tax_deduction

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

app = FastAPI(title="BellyUp", version="2.0")

_state: dict = {}


def boot():
    if not _state:
        _state["agencies"] = ag_mod.load(CONFIG)
        dests = json.loads((DATA / "destinations.json").read_text())
        _state["hubspots"] = [d for d in dests if d["dest_type"] == "hubspot"]
        _state["donors"] = json.loads((DATA / "donors.json").read_text())["donors"]
        _state["idx"] = needs_mod.get_index()
    return _state


def now() -> datetime:
    """Demo clock. Anchored so the rehearsed scenarios are reproducible."""
    return _state.get("clock", scenarios.DEMO_NOW)


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

class Donation(BaseModel):
    donor_name: str = "My Restaurant"
    lat: float
    lon: float
    address: str = ""
    food_type: str = "produce"
    quantity_lbs: float = Field(gt=0)
    condition: str = "ambient"
    ready_in_min: int = 15
    expires_in_hours: float = 24.0

    def to_donation(self, t: datetime) -> dict:
        return {
            "donor_name": self.donor_name, "lat": self.lat, "lon": self.lon,
            "address": self.address, "food_type": self.food_type,
            "quantity_lbs": self.quantity_lbs, "condition": self.condition,
            "ready_at": t + timedelta(minutes=self.ready_in_min),
            "expires_at": t + timedelta(hours=self.expires_in_hours),
            "sb1383_tier": None,
        }


class LimitBody(BaseModel):
    limit_lbs: float | None = None


class ConfigBody(BaseModel):
    updates: dict


# --------------------------------------------------------------------------
# shared / map
# --------------------------------------------------------------------------

@app.get("/api/config")
def get_config():
    return {"config": CONFIG,
            "sources": CONFIG_SOURCES,
            "unverified": [k for k, v in CONFIG_SOURCES.items() if not v["verified"]]}


@app.post("/api/config")
def set_config(body: ConfigBody):
    """Live tuning. Only existing keys, only numeric/bool values."""
    changed = {}
    for k, v in body.updates.items():
        if k not in CONFIG:
            raise HTTPException(400, f"unknown config key: {k}")
        if not isinstance(v, (int, float, bool)):
            raise HTTPException(400, f"{k} must be numeric or boolean")
        CONFIG[k] = type(CONFIG[k])(v) if not isinstance(CONFIG[k], dict) else v
        changed[k] = CONFIG[k]
    return {"changed": changed}


@app.get("/api/geocode")
def geocode(address: str = Query(..., min_length=3)):
    """Resolve a street address so nobody has to know their own coordinates."""
    hit = geo.lookup(address)
    if hit is None:
        raise HTTPException(404, f"Could not find '{address}'. Try adding the "
                                 f"city and ZIP.")
    return hit


@app.get("/api/blocks")
def blocks():
    """Block polygons with need. Aggregated to block level, never finer."""
    s = boot()
    idx = s["idx"]
    feats = []
    for b in idx.blocks.values():
        ring = [[round(x, 6), round(y, 6)] for x, y in
                _to_lonlat(b.polygon_m.exterior.coords)]
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"block_id": b.block_id, "area": b.area, "label": b.label,
                           "need_now": b.need_now, "need_trend": round(b.need_trend, 2),
                           "in_panel": b.block_id in idx.panel_block_ids},
        })
    return {"type": "FeatureCollection", "features": feats,
            "latest_count_date": idx.latest_count_date.date().isoformat()}


def _to_lonlat(coords):
    from needs import _LAT0, _LON0, _M_PER_DEG_LAT, _M_PER_DEG_LON
    return [(x / _M_PER_DEG_LON + _LON0, y / _M_PER_DEG_LAT + _LAT0) for x, y in coords]


@app.get("/api/forecast")
def forecast():
    return boot()["idx"].forecast


@app.get("/api/scenarios")
def list_scenarios():
    out = []
    for k, v in scenarios.SCENARIOS.items():
        d = v["donation"]
        out.append({"key": k, "label": v["label"], "blurb": v["blurb"],
                    "donor_name": d["donor_name"], "lat": d["lat"], "lon": d["lon"],
                    "address": d["address"], "food_type": d["food_type"],
                    "quantity_lbs": d["quantity_lbs"], "condition": d["condition"]})
    return {"scenarios": out}


# --------------------------------------------------------------------------
# RESTAURANT view
# --------------------------------------------------------------------------

@app.post("/api/restaurant/match")
def restaurant_match(body: Donation, commit: bool = False,
                     max_km: float | None = Query(None)):
    """Post surplus, get ranked agencies who will collect it.

    Deliberately returns no hubspot information. A donor learns that a pickup
    was accepted and by whom -- never where the food goes afterwards.
    """
    s = boot()
    t = now()
    don = body.to_donation(t)
    r = collection.match(don, s["agencies"], s["hubspots"], now=t, commit=commit)

    matches = r["matches"]
    if max_km is not None:
        matches = [m for m in matches if m["one_way_km"] <= max_km]

    return {
        "view": "restaurant",
        "quantity_lbs": r["quantity_lbs"],
        "tax": r["tax"],
        "feasible": len(matches),
        "evaluated": r["evaluated"],
        "matches": [_donor_safe(m) for m in matches],
        "rejection_summary": r["rejection_summary"],
        "headline": r["headline"],
        "simulated_agency_data": r["simulated_agency_data"],
    }


def _donor_safe(m: dict) -> dict:
    """Strip anything that would tell a donor where food goes after pickup."""
    drop = {"need_basis", "need_now", "need_multiplier", "serves_hubspots"}
    out = {k: v for k, v in m.items() if k not in drop}
    out["collection_type"] = ("mobile outreach — taken out to people"
                              if m["kind"] == "mobile"
                              else "walk-in pantry — people collect it there")
    return out


@app.get("/api/restaurant/donors")
def sample_donors(limit: int = 60):
    """Real downtown food businesses, to prefill the form."""
    return {"donors": boot()["donors"][:limit]}


# --------------------------------------------------------------------------
# AGENCY view
# --------------------------------------------------------------------------

@app.get("/api/agency")
def agency_list():
    s = boot()
    budgets = ag_mod.intake_demand(s["agencies"], CONFIG)
    t = now()
    return {"simulated": ag_mod.is_simulated(s["agencies"]),
            "agencies": [{
                "agency_id": a["agency_id"], "name": a["name"],
                "lat": a["lat"], "lon": a["lon"],
                "has_mobile_pantry": a["has_mobile_pantry"],
                "simulated": a.get("simulated", False),
                "demand_lbs": budgets[a["agency_id"]]["demand_lbs"],
                "limit_lbs": sched.LIMITS.get(a["agency_id"], t),
                "scheduled_lbs": sched.SCHEDULE.committed_lbs(a["agency_id"], t),
            } for a in s["agencies"]]}


@app.get("/api/agency/{agency_id}")
def agency_dashboard(agency_id: str, max_km: float | None = Query(None)):
    """Everything one agency sees: hubspots, scheduled pickups, its own limit."""
    s = boot()
    t = now()
    a = next((x for x in s["agencies"] if x["agency_id"] == agency_id), None)
    if a is None:
        raise HTTPException(404, "no such agency")

    budgets = ag_mod.intake_demand(s["agencies"], CONFIG)
    hub_budgets = demand_mod.daily_demand(s["hubspots"], CONFIG)

    hubs = []
    for h in s["hubspots"]:
        km = needs_mod.haversine_km(a["lat"], a["lon"], h["lat"], h["lon"])
        if max_km is not None and km > max_km:
            continue
        b = hub_budgets[h["dest_id"]]
        hubs.append({
            "dest_id": h["dest_id"], "name": h["name"], "block_id": h.get("block_id"),
            "lat": h["lat"], "lon": h["lon"], "area": h.get("area"),
            "need_now": h["need_now"], "need_trend": h["need_trend"],
            "daily_demand_lbs": b["daily_demand_lbs"],
            "committed_today_lbs": round(demand_mod.LEDGER.committed(h["dest_id"], t), 1),
            "remaining_lbs": round(demand_mod.LEDGER.remaining(
                h["dest_id"], t, b["daily_demand_lbs"]), 1),
            "distance_km": round(km, 2),
        })
    hubs.sort(key=lambda x: x["distance_km"])

    return {
        "view": "agency",
        "agency": {**{k: a[k] for k in
                      ("agency_id", "name", "lat", "lon", "has_mobile_pantry",
                       "mobile_capacity_lbs", "max_hubspot_stops", "accepts")},
                   "simulated": a.get("simulated", False),
                   "intake": budgets[agency_id]},
        "limit_lbs": sched.LIMITS.get(agency_id, t),
        "scheduled_lbs": sched.SCHEDULE.committed_lbs(agency_id, t),
        "pickups": sched.SCHEDULE.for_agency(agency_id, t, max_km=max_km),
        "hubspots": hubs if a["has_mobile_pantry"] else [],
        "hubspots_note": ("" if a["has_mobile_pantry"] else
                          "This agency has no mobile pantry, so it does not serve "
                          "hubspots. People collect from the pantry instead."),
    }


@app.post("/api/agency/{agency_id}/limit")
def set_limit(agency_id: str, body: LimitBody):
    """An agency caps how much it wants today. Only ever tightens."""
    s = boot()
    if not any(x["agency_id"] == agency_id for x in s["agencies"]):
        raise HTTPException(404, "no such agency")
    sched.LIMITS.set(agency_id, now(), body.limit_lbs)
    return {"agency_id": agency_id, "limit_lbs": body.limit_lbs,
            "scheduled_lbs": sched.SCHEDULE.committed_lbs(agency_id, now())}


@app.post("/api/agency/{agency_id}/distribute")
def distribute(agency_id: str, available_lbs: float | None = None,
               commit: bool = False):
    """Plan the mobile pantry's run out to hubspots."""
    s = boot()
    t = now()
    a = next((x for x in s["agencies"] if x["agency_id"] == agency_id), None)
    if a is None:
        raise HTTPException(404, "no such agency")
    load = available_lbs if available_lbs is not None else \
        sched.SCHEDULE.committed_lbs(agency_id, t)
    if load <= 0:
        load = a.get("mobile_capacity_lbs", 0) or 0
    return distribution.plan(a, s["hubspots"], float(load), now=t, commit=commit)


@app.delete("/api/agency/pickup/{pickup_id}")
def cancel_pickup(pickup_id: str):
    if not sched.SCHEDULE.cancel(pickup_id):
        raise HTTPException(404, "no such pickup")
    return {"pickup_id": pickup_id, "status": "cancelled"}


# --------------------------------------------------------------------------
# PUBLIC view -- a person looking for food
# --------------------------------------------------------------------------

@app.get("/api/pantries")
def pantries(lat: float | None = None, lon: float | None = None,
             max_km: float | None = None, only_with_food: bool = False):
    """Pantries only. Never hubspots. See pantry_finder for why."""
    s = boot()
    return pantry_finder.find(s["agencies"], lat=lat, lon=lon, now=now(),
                              max_km=max_km, only_with_food=only_with_food)


# --------------------------------------------------------------------------
# demo control
# --------------------------------------------------------------------------

@app.post("/api/reset")
def reset():
    demand_mod.LEDGER.reset()
    sched.SCHEDULE.reset()
    sched.LIMITS.reset()
    return {"reset": True}


@app.get("/api/state")
def state():
    t = now()
    return {"clock": t.isoformat(),
            "ledger": demand_mod.LEDGER.snapshot(t),
            "pickups": sched.SCHEDULE.all(t),
            "limits": sched.LIMITS.snapshot(t)}


@app.post("/api/demo/{key}")
def run_scenario(key: str, commit: bool = True):
    if key not in scenarios.SCENARIOS:
        raise HTTPException(404, "no such scenario")
    s = boot()
    return pipeline.run(scenarios.get(key)["donation"], s["agencies"],
                        s["hubspots"], now=now(), commit=commit)


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")
