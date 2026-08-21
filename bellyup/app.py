"""FastAPI surface for the BellyUp dispatch board.

    /api/board/*   restaurants report surplus, agencies get matched offers,
                   the public finds pantries -- one merged evening board.

Hubspots are agency-facing only. They are block-level locations where
unsheltered people gather, and publishing them would hand anyone --
including someone looking to move people on -- a map of where to find them.
The business and public views never see them.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import claims as claims_mod
import demo_data
import dispatch
import geocode as geo
import registry

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

app = FastAPI(title="BellyUp", version="2.0")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def board_now() -> datetime:
    """Clock for the dispatch board.

    Separate from now() on purpose: the board is an evening scenario anchored
    to the 3rd Thursday, which is what mobile-pantry availability is resolved
    against. The role views are anchored to a Wednesday afternoon.
    """
    return _board.get("clock", demo_data.DEMO_NOW)


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

class SurplusReport(BaseModel):
    """A restaurant registering and reporting tonight's surplus."""
    name: str = Field(min_length=2)
    address: str = ""
    lat: float | None = None
    lon: float | None = None
    facility_type: str = "restaurant"
    surplus: str = "prepared"          # 'prepared' | 'packaged/produce'
    lbs: float = Field(gt=0)
    items: str = ""
    pickup_from: str | None = None     # 'HH:MM'
    pickup_to: str | None = None
    expires_at: str | None = None      # 'HH:MM'
    expires_in_hours: float | None = None
    freshness: str = "fresh"


class ReportUpdate(BaseModel):
    """Tonight's numbers for a supplier that is already on the platform.

    Every field is optional: a restaurant updating only the weight should not
    have to restate its pickup window. `has_surplus: false` clears the report
    entirely -- some nights a kitchen has nothing, and saying so is a real
    answer, not a missing one.
    """
    has_surplus: bool = True
    surplus: str | None = None
    lbs: float | None = None
    items: str | None = None
    pickup_from: str | None = None
    pickup_to: str | None = None
    expires_at: str | None = None
    freshness: str | None = None


# --------------------------------------------------------------------------
# shared / map
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# DISPATCH BOARD -- the merged view: real data, live registration
# --------------------------------------------------------------------------

_board: dict = {}


def board():
    """Real datasets, loaded once, plus suppliers registered this session."""
    if not _board:
        _board["hotspots"] = demo_data.load_hotspots()
        _board["suppliers"] = demo_data.load_suppliers()
        _board["agencies"] = demo_data.load_agencies()
        _board["pantries"] = demo_data.load_pantries()
        _board["history"] = demo_data.load_history(
            _board["suppliers"], _board["agencies"],
            _board["pantries"], _board["hotspots"])
    return _board


def _find_supplier(sid: str) -> dict:
    s = next((x for x in board()["suppliers"] if x["id"] == sid), None)
    if s is None:
        raise HTTPException(404, "no such supplier")
    if not s.get("report"):
        raise HTTPException(400, f"{s['name']} has not reported surplus tonight")
    return s


@app.get("/api/board")
def get_board():
    """Everything the front end needs to draw the map and the feed."""
    b = board()
    return {
        "hotspots": b["hotspots"],
        "suppliers": b["suppliers"],
        "agencies": b["agencies"],
        "pantries": b["pantries"],
        "constants": demo_data.CONSTANTS,
        "claims": claims_mod.CLAIMS.all(),
        "history": b["history"],
        "tonight": dispatch.LEDGER.deliveries,
        "registered": registry.count(),
        "optedOut": registry.optout_count(),
        "now": board_now().strftime("%H:%M"),
        "date": board_now().strftime("%A, %b %-d %Y"),
        "served": dispatch.LEDGER.snapshot(),
    }


@app.post("/api/board/register")
def register_supplier(body: SurplusReport):
    """A restaurant signs up and reports surplus in one step.

    Geocodes the address if no coordinates were given, so an owner only has to
    type where they are. The report joins tonight's feed immediately.
    """
    b = board()

    lat, lon = body.lat, body.lon
    if lat is None or lon is None:
        if not body.address:
            raise HTTPException(400, "give an address, or a lat/lon")
        hit = geo.lookup(body.address)
        if hit is None:
            raise HTTPException(404, f"Could not find '{body.address}'. "
                                     f"Try adding the city and ZIP.")
        lat, lon = hit["lat"], hit["lon"]

    reported = body.pickup_from or board_now().strftime("%H:%M")
    sid = registry.next_id()

    supplier = {
        "id": sid, "name": body.name, "type": body.facility_type,
        "address": body.address or f"{lat:.4f}, {lon:.4f}",
        "lat": lat, "lon": lon,
        "surplus": body.surplus,
        "sb1383Tier": None,
        "registered": True,
        "report": {
            "lbs": body.lbs,
            "items": body.items or "surplus reported by the kitchen",
            "time": reported,
            "pickupFrom": reported,
            "pickupTo": body.pickup_to,
            "expiresAt": body.expires_at,
            "expiresInHours": body.expires_in_hours,
            "freshness": body.freshness,
        },
    }
    # Permanent: appended to dataset/businesses.csv alongside the curated 31,
    # with tonight's numbers in surplus_reports.csv.
    registry.add_business(supplier)
    registry.save_report(supplier, has_surplus=True)
    _board.clear()
    return {"supplier": supplier, "persisted": True,
            "registered_total": registry.count()}


@app.post("/api/board/report/{supplier_id}")
def update_report(supplier_id: str, body: ReportUpdate):
    """Update tonight's surplus for a supplier already on the platform.

    Surplus is different every night. Without this the feed is a fixture: the
    same fourteen quantities forever, which is exactly what voluntary daily
    reporting is meant to replace.
    """
    b = board()
    s = next((x for x in b["suppliers"] if x["id"] == supplier_id), None)
    if s is None:
        raise HTTPException(404, "no such supplier")

    if not body.has_surplus:
        s["report"] = None
        registry.save_report(s, has_surplus=False)
        return {"supplier": s, "cleared": True}

    rep = dict(s.get("report") or {})
    if not rep:
        # first report of the evening for a partner that was quiet
        rep = {"time": board_now().strftime("%H:%M"),
               "items": "surplus reported by the kitchen"}
        rep["pickupFrom"] = rep["time"]

    if body.lbs is not None:
        if body.lbs <= 0:
            raise HTTPException(422, "lbs must be greater than 0")
        rep["lbs"] = body.lbs
    if body.items is not None:
        rep["items"] = body.items or rep.get("items", "")
    if body.pickup_from:
        rep["pickupFrom"] = body.pickup_from
        rep["time"] = body.pickup_from
    if body.pickup_to:
        rep["pickupTo"] = body.pickup_to
    if body.expires_at:
        rep["expiresAt"] = body.expires_at
        rep.pop("expiresInHours", None)      # an explicit time wins
    if body.freshness:
        rep["freshness"] = body.freshness
    if body.surplus:
        s["surplus"] = body.surplus

    rep["updated"] = True
    s["report"] = rep
    # Every supplier's report persists now, curated ones included -- an updated
    # weight that vanished on restart was its own small bug.
    registry.save_report(s, has_surplus=True)
    return {"supplier": s, "cleared": False, "persisted": True}


@app.post("/api/board/dispatch/{supplier_id}")
def dispatch_supplier(supplier_id: str):
    """Rank every (collector, hotspot) pair for one report.

    A recommendation only. Nothing enters the ledger until it is confirmed.
    """
    b = board()
    s = _find_supplier(supplier_id)
    result = dispatch.compute(s, b["agencies"], b["pantries"], b["hotspots"],
                              board_now())
    out = dispatch.serialisable(result)
    out["supplier"] = s
    out["served"] = dispatch.LEDGER.snapshot()
    return out


@app.post("/api/board/confirm/{supplier_id}")
def confirm_dispatch(supplier_id: str):
    """Book the top dispatch for this report and issue a receipt."""
    b = board()
    s = _find_supplier(supplier_id)
    if supplier_id in dispatch.LEDGER.dispatched_supplier_ids():
        raise HTTPException(409, f"{s['name']} has already been dispatched tonight")

    result = dispatch.compute(s, b["agencies"], b["pantries"], b["hotspots"],
                              board_now())
    if not result["pairs"]:
        raise HTTPException(422, "no viable dispatch to confirm")

    top = result["pairs"][0]
    rec = dispatch.LEDGER.confirm(s, top, demo_data.CONSTANTS, board_now())
    hs = top["hotspot"]

    if hs is None:
        # a drop-off: no block was served, so no block's limit moved
        site = top["collector"]
        return {
            "receipt": rec,
            "dropoff": True,
            "hotspot": {"id": None, "location": site["name"], "need": None,
                        "remaining": None, "drops": 0,
                        "closed": False, "closedWhy": ""},
            "tonight": dispatch.LEDGER.deliveries,
        }

    closed, why = dispatch.LEDGER.is_closed(hs, demo_data.CONSTANTS)
    return {
        "receipt": rec,
        "dropoff": False,
        "hotspot": {"id": hs["id"], "location": hs["location"],
                    "need": hs["need"],
                    "remaining": round(dispatch.LEDGER.remaining(hs), 1),
                    "drops": dispatch.LEDGER.drops(hs["id"]),
                    "closed": closed, "closedWhy": why},
        "tonight": dispatch.LEDGER.deliveries,
    }


@app.get("/api/board/ledger")
def get_ledger():
    """Past deliveries plus tonight's confirmed ones."""
    b = board()
    return {"history": b["history"], "tonight": dispatch.LEDGER.deliveries,
            "maxDropsPerNight": demo_data.CONSTANTS["MAX_DROPS_PER_NIGHT"],
            "demoDate": demo_data.CONSTANTS["DEMO_DATE"]}


@app.post("/api/board/ledger/reset")
def reset_ledger():
    """Clear tonight's confirmed deliveries and acceptances. History stays."""
    dispatch.LEDGER.reset()
    claims_mod.CLAIMS.reset()
    return {"reset": True, "tonight": []}


@app.delete("/api/board/supplier/{supplier_id}")
def remove_supplier(supplier_id: str):
    """Take a restaurant off the platform.

    Two different things depending on where it came from:

      self-registered -> its row is deleted outright
      curated         -> recorded as an opt-out and filtered out at load

    The 31 rows in businesses.csv are externally sourced, so they are never
    rewritten. A business leaving is an opt-out, not a deletion from the
    record, and `POST /api/board/restore` puts it back.
    """
    b = board()
    s = next((x for x in b["suppliers"] if x["id"] == supplier_id), None)
    if s is None:
        raise HTTPException(404, "no such supplier")

    if s.get("registered"):
        registry.remove_business(s["name"])
        registry.drop_report(s["name"])
        how = "deleted"
    else:
        registry.opt_out(s["name"], supplier_id)
        how = "opted out"

    _board.clear()
    return {"removed": supplier_id, "name": s["name"], "how": how,
            "registered_total": registry.count(),
            "opted_out_total": registry.optout_count()}


# the old path, kept so nothing that already calls it breaks
@app.delete("/api/board/registered/{supplier_id}")
def remove_registered(supplier_id: str):
    return remove_supplier(supplier_id)


@app.post("/api/board/restore")
def restore_suppliers(name: str | None = None):
    """Put opted-out businesses back on the platform."""
    n = registry.restore(name)
    _board.clear()
    return {"restored": n, "opted_out_total": registry.optout_count()}


@app.post("/api/board/reset")
def reset_board():
    """Clear tonight's ledger and reload.

    Registered restaurants survive this -- they are on disk, and they are
    partners now, not demo state.
    """
    dispatch.LEDGER.reset()
    claims_mod.CLAIMS.reset()
    _board.clear()
    return {"reset": True, "registered_kept": registry.count()}


# --------------------------------------------------------------------------
# ROLE-SCOPED VIEWS
# --------------------------------------------------------------------------
# What each role may see is a product decision, not a UI one, so it is
# enforced here. A business never receives hotspot coordinates; the public
# view never receives them either.

def _status(sid: str) -> str:
    if sid in {d["supplierId"] for d in dispatch.LEDGER.deliveries}:
        return "delivered"
    return "accepted" if claims_mod.CLAIMS.is_claimed(sid) else "pending"


@app.get("/api/board/business")
def business_view(lat: float | None = None, lon: float | None = None,
                  max_km: float | None = None):
    """What a donating business sees: itself, its neighbours, and who collects.

    Deliberately no hotspots. A donor learns that a pickup was accepted and by
    whom -- never which block the food goes to. Block-level locations are where
    unsheltered people gather, and publishing them to every restaurant that
    signs up would defeat the point of aggregating them.
    """
    b = board()
    ags = [{k: a[k] for k in
            ("id", "name", "program", "lat", "lon", "acceptsPrepared",
             "mobileCapable", "address", "phone") if k in a}
           for a in b["agencies"]]
    pans = [{k: p[k] for k in
             ("id", "name", "operator", "lat", "lon", "program", "schedule",
              "acceptsPrepared", "dispatchable") if k in p}
            for p in b["pantries"]]

    sups = []
    for s in b["suppliers"]:
        row = {k: s[k] for k in ("id", "name", "type", "address", "lat", "lon",
                                 "surplus", "registered")}
        row["reporting"] = bool(s["report"])
        row["status"] = _status(s["id"]) if s["report"] else None
        if s["report"]:
            row["lbs"] = s["report"]["lbs"]
            holder = claims_mod.CLAIMS.holder(s["id"])
            if holder:
                row["acceptedBy"] = next(
                    (a["name"] for a in b["agencies"] if a["id"] == holder), holder)
        if lat is not None and lon is not None and max_km:
            if haversine_km(lat, lon, s["lat"], s["lon"]) > max_km:
                continue
        sups.append(row)

    return {"view": "business", "agencies": ags, "pantries": pans,
            "suppliers": sups, "constants": demo_data.CONSTANTS,
            "now": board_now().strftime("%H:%M"),
            "date": board_now().strftime("%A, %b %-d %Y"),
            "note": "Collection points only. Outreach locations are never shown "
                    "to donors."}


@app.get("/api/board/agency/{agency_id}/offers")
def agency_offers(agency_id: str):
    """Reports this agency could take, and the ones it already has."""
    b = board()
    col = next((c for c in dispatch.collectors(b["agencies"], b["pantries"])
                if c["id"] == agency_id), None)
    if col is None:
        raise HTTPException(404, "no such collecting agency")

    offers, mine = [], []
    for s in b["suppliers"]:
        if not s["report"]:
            continue
        st = _status(s["id"])
        if st == "delivered":
            continue
        holder = claims_mod.CLAIMS.holder(s["id"])
        if holder and holder != agency_id:
            continue          # somebody else already took it

        r = dispatch.compute(s, b["agencies"], b["pantries"], b["hotspots"],
                             board_now())
        best = next((p for p in r["pairs"] if p["collector"]["id"] == agency_id), None)
        row = {
            "supplier": {k: s[k] for k in ("id", "name", "type", "address",
                                           "lat", "lon", "surplus")},
            "report": s["report"],
            "status": st,
            "viable": best is not None,
            "net": best["net"] if best else None,
            "miles": best["miles"] if best else None,
            "deferred": best.get("deferred") if best else None,
            "deliversAt": best.get("deliversAt") if best else None,
            "target": (best["hotspot"]["location"] if best and best["hotspot"]
                       else None),
            "whyNot": (None if best else
                       (r["rejections"][0]["example"] if r["rejections"] else
                        "no viable run")),
        }
        (mine if st == "accepted" else offers).append(row)

    offers.sort(key=lambda o: (o["net"] is None, -(o["net"] or 0)))
    return {"agency": {k: col[k] for k in ("id", "name", "kind", "lat", "lon",
                                           "capacityLbs") if k in col},
            "offers": offers, "accepted": mine,
            "acceptedLbs": round(sum(m["report"]["lbs"] for m in mine), 1)}


@app.post("/api/board/agency/{agency_id}/accept/{supplier_id}")
def accept_offer(agency_id: str, supplier_id: str):
    b = board()
    if not any(c["id"] == agency_id
               for c in dispatch.collectors(b["agencies"], b["pantries"])):
        raise HTTPException(404, "no such collecting agency")
    s = _find_supplier(supplier_id)
    holder = claims_mod.CLAIMS.holder(supplier_id)
    if holder and holder != agency_id:
        raise HTTPException(409, f"{s['name']} has already been taken by "
                                 f"another agency")
    rec = claims_mod.CLAIMS.accept(supplier_id, agency_id, board_now())
    return {"accepted": rec, "supplier": s["name"]}


@app.post("/api/board/agency/{agency_id}/release/{supplier_id}")
def release_offer(agency_id: str, supplier_id: str):
    if claims_mod.CLAIMS.holder(supplier_id) != agency_id:
        raise HTTPException(404, "this agency has not accepted that pickup")
    claims_mod.CLAIMS.release(supplier_id)
    return {"released": supplier_id}


@app.post("/api/board/agency/{agency_id}/plan")
def plan_combined_run(agency_id: str, supplier_ids: str | None = None):
    """One vehicle, several accepted pickups, in the shortest order."""
    b = board()
    col = next((c for c in dispatch.collectors(b["agencies"], b["pantries"])
                if c["id"] == agency_id), None)
    if col is None:
        raise HTTPException(404, "no such collecting agency")

    wanted = ([x for x in supplier_ids.split(",") if x] if supplier_ids
              else claims_mod.CLAIMS.for_agency(agency_id))
    sups = [s for s in b["suppliers"] if s["id"] in wanted and s["report"]]
    if not sups:
        raise HTTPException(422, "no accepted pickups to plan")

    return dispatch.combine_run(col, sups, b["hotspots"], board_now())


@app.post("/api/board/agency/{agency_id}/preview")
def preview_run(agency_id: str, supplier_ids: str = ""):
    """Plan a run over a candidate set WITHOUT taking any of it.

    Separate from accepting on purpose: an agency wants to see what a
    combination looks like before it commits to any of it, and a claim that has
    to be undone to try a different pairing is a claim that discourages trying.
    """
    b = board()
    col = next((c for c in dispatch.collectors(b["agencies"], b["pantries"])
                if c["id"] == agency_id), None)
    if col is None:
        raise HTTPException(404, "no such collecting agency")

    wanted = [x for x in supplier_ids.split(",") if x]
    sups = [s for s in b["suppliers"] if s["id"] in wanted and s["report"]]
    if not sups:
        return {"feasible": False, "reason": "nothing selected yet"}

    taken = [s["name"] for s in sups
             if (claims_mod.CLAIMS.holder(s["id"]) or agency_id) != agency_id]
    if taken:
        return {"feasible": False,
                "reason": f"already taken by another agency: {', '.join(taken)}"}
    return dispatch.combine_run(col, sups, b["hotspots"], board_now())


@app.post("/api/board/agency/{agency_id}/accept-run")
def accept_run(agency_id: str, supplier_ids: str = ""):
    """Take the whole run: claim every pickup and book it into the ledger."""
    b = board()
    col = next((c for c in dispatch.collectors(b["agencies"], b["pantries"])
                if c["id"] == agency_id), None)
    if col is None:
        raise HTTPException(404, "no such collecting agency")

    wanted = [x for x in supplier_ids.split(",") if x]
    sups = [s for s in b["suppliers"] if s["id"] in wanted and s["report"]]
    if not sups:
        raise HTTPException(422, "nothing selected")
    for s in sups:
        holder = claims_mod.CLAIMS.holder(s["id"])
        if holder and holder != agency_id:
            raise HTTPException(409, f"{s['name']} has already been taken")

    plan = dispatch.combine_run(col, sups, b["hotspots"], board_now())
    if not plan.get("feasible"):
        raise HTTPException(422, plan.get("reason", "no viable run"))

    receipts = dispatch.LEDGER.confirm_run(plan, sups, demo_data.CONSTANTS,
                                           board_now())
    # a pickup left behind for capacity stays on offer -- it has not been
    # collected, so claiming it would strand it
    collected = {p["id"] for p in plan["pickups"]}
    for s in sups:
        if s["id"] in collected:
            claims_mod.CLAIMS.accept(s["id"], agency_id, board_now())
    return {"plan": plan, "receipts": receipts,
            "leftOnOffer": [s["name"] for s in sups if s["id"] not in collected],
            "tonight": dispatch.LEDGER.deliveries}


@app.get("/api/board/pantries")
def board_pantries(lat: float | None = None, lon: float | None = None,
                   max_km: float = 5.0):
    """Public view: where a person can get food. Pantries only, never hotspots."""
    b = board()
    out = []
    for p in b["pantries"]:
        row = {k: p[k] for k in ("id", "name", "operator", "lat", "lon",
                                 "program", "schedule", "daysPerWeek",
                                 "availableTonight", "whyNot")}
        row["openTonight"] = bool(p["availableTonight"])
        row["kind"] = "mobile pantry"
        out.append(row)
    for a in b["agencies"]:
        if a.get("mobileCapable", True):
            continue          # a depot is not somewhere to turn up for a meal
        out.append({"id": a["id"], "name": a["name"], "operator": a.get("program", ""),
                    "lat": a["lat"], "lon": a["lon"], "program": a.get("program", ""),
                    "schedule": "", "daysPerWeek": None,
                    "availableTonight": True, "openTonight": True,
                    "whyNot": None, "kind": "walk-in pantry",
                    "address": a.get("address", ""), "phone": a.get("phone", "")})

    if lat is not None and lon is not None:
        for r in out:
            km = haversine_km(lat, lon, r["lat"], r["lon"])
            r["distanceKm"] = round(km, 2)
            r["walkMinutes"] = round(km / 5.0 * 60)
        out = [r for r in out if r["distanceKm"] <= max_km]
        out.sort(key=lambda r: (not r["openTonight"], r["distanceKm"]))

    return {"view": "public", "count": len(out), "pantries": out,
            "openNow": sum(1 for r in out if r["openTonight"]),
            "note": "Pantry locations only. Outreach locations are never shown here."}


@app.get("/api/geocode")
def geocode(address: str = Query(..., min_length=3)):
    """Resolve a street address so nobody has to know their own coordinates."""
    hit = geo.lookup(address)
    if hit is None:
        raise HTTPException(404, f"Could not find '{address}'. Try adding the "
                                 f"city and ZIP.")
    return hit


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "board" / "index.html")
