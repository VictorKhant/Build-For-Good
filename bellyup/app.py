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
import claims as claims_mod
import collection
import demand as demand_mod
import demo_data
import dispatch
import distribution
import geocode as geo
import needs as needs_mod
import pantry_finder
import pipeline
import registry
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
    """Clock for the role-scoped views and their rehearsed scenarios."""
    return _state.get("clock", scenarios.DEMO_NOW)


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
        # Who each report is addressed to. Computed once: a report is matched
        # to exactly one collector, and only that collector may act on it.
        _board["targets"] = dispatch.assign_targets(
            [x for x in _board["suppliers"] if x.get("report")],
            _board["agencies"], _board["pantries"], _board["hotspots"],
            board_now())
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


def _lifecycle(s: dict, b: dict) -> dict:
    """The request-lifecycle fields for one supplier row.

    Shared because /api/board and /api/board/business both need them and drifted
    once already: the board omitted `status`, the UI read `label[s.status]`, and
    the whole right-hand panel died on a supplier the business view would have
    described fine. Anything a donor may see about its own request belongs here,
    and nothing hotspot-shaped ever does.
    """
    if not s.get("report"):
        return {"reporting": False, "status": None}
    out = {"reporting": True, "status": _status(s["id"]),
           "lbs": s["report"]["lbs"]}
    tgt = b["targets"].get(s["id"])
    out["matchedTo"] = _target_name(tgt)
    out["matchedToId"] = tgt
    req = claims_mod.REQUESTS.get(s["id"])
    if req:
        out["requestedAt"] = req["requested_at"]
        out["declinedBy"] = [_target_name(x) for x in req["declined_by"]]
        out["openToAll"] = req["open_to_all"]
        out["allowFallback"] = req.get("allow_fallback", True)
    out["windowClosed"] = _window_closed(s["report"], board_now())
    holder = claims_mod.CLAIMS.holder(s["id"])
    if holder:
        out["acceptedBy"] = _target_name(holder)
    return out


@app.get("/api/board")
def get_board():
    """Everything the front end needs to draw the map and the feed."""
    b = board()
    return {
        "hotspots": b["hotspots"],
        "suppliers": [{**x, **_lifecycle(x, b)} for x in b["suppliers"]],
        "agencies": b["agencies"],
        "pantries": b["pantries"],
        "constants": demo_data.CONSTANTS,
        "claims": claims_mod.CLAIMS.all(),
        "requests": claims_mod.REQUESTS.all(),
        "targets": b["targets"],
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
    claims_mod.REQUESTS.reset()
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
    claims_mod.REQUESTS.reset()
    _board.clear()
    return {"reset": True, "registered_kept": registry.count()}


# --------------------------------------------------------------------------
# ROLE-SCOPED VIEWS
# --------------------------------------------------------------------------
# What each role may see is a product decision, not a UI one, so it is
# enforced here. A business never receives hotspot coordinates; the public
# view never receives them either.

def _status(sid: str) -> str:
    """Where a report is in the pipeline.

      reported   surplus exists; nobody has been asked. Business view only.
      requested  the business asked its matched collector to come.
      declined   asked, told no, and the donor did not allow a fallback.
      accepted   a collector took it.
      delivered  it reached people; in the ledger.
    """
    if sid in {d["supplierId"] for d in dispatch.LEDGER.deliveries}:
        return "delivered"
    if claims_mod.CLAIMS.is_claimed(sid):
        return "accepted"
    if claims_mod.REQUESTS.is_withdrawn(sid):
        return "declined"
    return "requested" if claims_mod.REQUESTS.is_open(sid) else "reported"


def _window_closed(report: dict, now: datetime) -> bool:
    """Has the donor's pickup window already shut?

    A request nobody took before the dock closed is not actionable, so it comes
    off the collectors' boards rather than sitting there being declined by
    everyone. The business still sees it, marked expired -- it is their food.
    """
    to = report.get("pickupTo")
    if not to:
        return False
    try:
        h, m = (int(x) for x in to.split(":"))
    except ValueError:
        return False
    close = now.replace(hour=h, minute=m, second=0, microsecond=0)
    # a window ending after midnight belongs to tomorrow, not six hours ago
    if close < now - timedelta(hours=6):
        close += timedelta(days=1)
    return close < now


def _target_name(cid: str | None) -> str | None:
    if not cid:
        return None
    b = board()
    for x in b["agencies"] + b["pantries"]:
        if x["id"] == cid:
            return x["name"]
    return cid


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
        row.update(_lifecycle(s, b))
        if s["report"]:
            row["report"] = s["report"]
        if lat is not None and lon is not None and max_km:
            if needs_mod.haversine_km(lat, lon, s["lat"], s["lon"]) > max_km:
                continue
        sups.append(row)

    return {"view": "business", "agencies": ags, "pantries": pans,
            "suppliers": sups, "constants": demo_data.CONSTANTS,
            "now": board_now().strftime("%H:%M"),
            "date": board_now().strftime("%A, %b %-d %Y"),
            "note": "Collection points only. Outreach locations are never shown "
                    "to donors."}


@app.post("/api/board/request/{supplier_id}")
def request_pickup(supplier_id: str, allow_fallback: bool = True):
    """A business asks its matched collector to come.

    This is the step the board was missing. Having surplus is not asking for a
    pickup, and until a business asks, no collector sees the report -- a donor
    should not have a van assigned to it without saying it wants one.
    """
    b = board()
    s = _find_supplier(supplier_id)
    if claims_mod.CLAIMS.is_claimed(supplier_id):
        raise HTTPException(409, f"{s['name']} has already been accepted")
    if claims_mod.REQUESTS.is_open(supplier_id) \
            and not claims_mod.REQUESTS.is_withdrawn(supplier_id):
        raise HTTPException(409, f"{s['name']} has already requested a pickup")

    target = b["targets"].get(supplier_id)
    if target is None:
        # nothing viable when the board loaded -- re-check, the clock has moved
        fresh = dispatch.assign_targets([s], b["agencies"], b["pantries"],
                                        b["hotspots"], board_now())
        target = fresh.get(supplier_id)
    if target is None:
        raise HTTPException(422, "no collector can take this — nothing to request")

    rec = claims_mod.REQUESTS.open(supplier_id, target, board_now(),
                                   allow_fallback=allow_fallback)
    return {"request": rec, "supplier": s["name"],
            "sentTo": _target_name(target), "status": _status(supplier_id)}


@app.post("/api/board/request/{supplier_id}/cancel")
def cancel_pickup(supplier_id: str):
    """Withdraw a request that nobody has accepted yet."""
    if claims_mod.CLAIMS.is_claimed(supplier_id):
        raise HTTPException(409, "already accepted — too late to cancel")
    if not claims_mod.REQUESTS.cancel(supplier_id):
        raise HTTPException(404, "no open request for that supplier")
    return {"cancelled": supplier_id, "status": _status(supplier_id)}


@app.post("/api/board/agency/{agency_id}/decline/{supplier_id}")
def decline_offer(agency_id: str, supplier_id: str):
    """A collector says no.

    Whether that ends the request is the donor's decision, taken when they
    asked. With a fallback allowed it opens to every other collector minus this
    one; without, it leaves every board. Either way it is off this one.
    """
    s = _find_supplier(supplier_id)
    if not claims_mod.REQUESTS.visible_to(supplier_id, agency_id):
        raise HTTPException(404, "that request is not on this collector's board")
    rec = claims_mod.REQUESTS.decline(supplier_id, agency_id)
    return {"declined": supplier_id, "supplier": s["name"],
            "nowOpenToAll": rec["open_to_all"],
            "requestEnded": rec["withdrawn"], "status": _status(supplier_id),
            "declinedBy": [_target_name(x) for x in rec["declined_by"]]}


@app.get("/api/board/agency/{agency_id}/offers")
def agency_offers(agency_id: str):
    """Reports this agency could take, and the ones it already has."""
    b = board()
    col = next((c for c in dispatch.request_targets(b["agencies"], b["pantries"])
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
        # A REPORT is not an offer. Only a request the business actually made,
        # and only one addressed to this collector (or released to everyone
        # after a decline), belongs on this board.
        if st == "reported":
            continue
        if not claims_mod.REQUESTS.visible_to(s["id"], agency_id):
            continue
        if _window_closed(s["report"], board_now()):
            continue          # dock already shut; not actionable tonight
        holder = claims_mod.CLAIMS.holder(s["id"])
        if holder and holder != agency_id:
            continue          # somebody else already took it

        r = dispatch.compute(s, b["agencies"], b["pantries"], b["hotspots"],
                             board_now())
        best = next((p for p in r["pairs"] if p["collector"]["id"] == agency_id), None)
        req = claims_mod.REQUESTS.get(s["id"]) or {}
        row = {
            "supplier": {k: s[k] for k in ("id", "name", "type", "address",
                                           "lat", "lon", "surplus")},
            "report": s["report"],
            "status": st,
            # What this collector's "no" would do. Declining is a different act
            # depending on the donor's choice, and it should not have to guess.
            "allowFallback": req.get("allow_fallback", True),
            "exclusiveToMe": req.get("target") == agency_id
                             and not req.get("open_to_all"),
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
               for c in dispatch.request_targets(b["agencies"], b["pantries"])):
        raise HTTPException(404, "no such collecting agency")
    s = _find_supplier(supplier_id)
    if not claims_mod.REQUESTS.visible_to(supplier_id, agency_id):
        raise HTTPException(409, f"{s['name']} has not requested a pickup from "
                                 f"this collector")
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
    col = next((c for c in dispatch.request_targets(b["agencies"], b["pantries"])
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
            km = needs_mod.haversine_km(lat, lon, r["lat"], r["lon"])
            r["distanceKm"] = round(km, 2)
            r["walkMinutes"] = round(km / 5.0 * 60)
        out = [r for r in out if r["distanceKm"] <= max_km]
        out.sort(key=lambda r: (not r["openTonight"], r["distanceKm"]))

    return {"view": "public", "count": len(out), "pantries": out,
            "openNow": sum(1 for r in out if r["openTonight"]),
            "note": "Pantry locations only. Outreach locations are never shown here."}


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
    dispatch.LEDGER.reset()
    # Claims and requests are board state too. Clearing the ledger but leaving
    # them would rebuild `targets` while every supplier kept a status pointing
    # at the old assignment.
    claims_mod.CLAIMS.reset()
    claims_mod.REQUESTS.reset()
    _board.clear()
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
    """The merged dispatch board is the front door now."""
    return FileResponse(HERE / "static" / "board" / "index.html")


@app.get("/roles")
def roles_view():
    """The earlier three-role view, kept for the agency and pantry-finder tools."""
    return FileResponse(HERE / "static" / "index.html")
