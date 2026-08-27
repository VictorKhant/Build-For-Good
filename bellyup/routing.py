"""Real street distances and real street geometry.

Everything here replaces one line of arithmetic:

    miles = haversine(a, b) * 1.3

That approximation is defensible for triage and indefensible on a map. It
draws a route straight through the Convention Center, it cannot see a one-way
pair on Front St, and it is symmetric when downtown driving is not: getting
from A to B and back from B to A are different distances here.

So distances come from OSRM over the real OpenStreetMap road graph -- the same
class of engine behind a rideshare ETA -- and the drawn line is the actual
geometry the vehicle would follow.

Three things make that survivable inside a scoring loop that evaluates
thousands of collector x block pairs per click:

  1. **A precomputed matrix.** `data/road_matrix.npz` holds every ordered pair
     among the static points -- collectors, hotspot blocks, drop-off sites,
     the curated businesses. Built once by `build_road_cache.py`, committed,
     read-only at runtime. A lookup is an array index, not a network call.

  2. **Bulk warming for anything new.** A restaurant that registered tonight
     is not in the matrix. `warm()` fetches its row and column against the
     known points in a handful of `/table` calls and memoises them, rather
     than issuing one HTTP request per leg.

  3. **A fallback that is never a failure.** If the router is unreachable, or
     the pair was never warmed, the old haversine x ROAD_FACTOR estimate is
     returned and the response says so. The board keeps working on a bad
     conference wifi; it just stops claiming the distance is routed.

`status()` reports which of those three answered, and the API passes it to the
UI, because "35.8 mi" computed two different ways should not look identical to
the person deciding whether to send a truck.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import OrderedDict

import numpy as np
import requests

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

# Public OSRM demo server by default: no key, no account, nothing to expire
# mid-demo. Point OSRM_URL at a self-hosted instance (or any OSRM-compatible
# endpoint) for production traffic -- the demo server asks not to be leaned on.
OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
OSRM_PROFILE = os.environ.get("OSRM_PROFILE", "driving")
# Only set this against an OSRM that actually carries a foot profile. The
# public demo server answers /foot/ with the car graph rather than an error,
# which is worse than not asking.
WALK_PROFILE = os.environ.get("OSRM_WALK_PROFILE") or None

# Set BELLYUP_ROUTING=off to force the straight-line model -- useful offline,
# and useful for showing on stage what routing actually changed.
ENABLED = os.environ.get("BELLYUP_ROUTING", "on").lower() not in ("off", "0", "false")

TABLE_MAX = 100          # OSRM demo server's hard cap on /table coordinates
HTTP_TIMEOUT = 12.0      # seconds; a slow router must not hang a dispatch
LIVE_WARM_BUDGET = 8     # /table calls one request may spend before giving up
PATH_CACHE_MAX = 256     # drawn routes held in memory

# Fallback constants. Kept here rather than imported from demo_data so this
# module has no dependency on the demo dataset and can be tested alone.
R_EARTH_MI = 3958.76
FALLBACK_ROAD_FACTOR = 1.3
FALLBACK_MPH = 18.0

_HERE = os.path.dirname(os.path.abspath(__file__))
MATRIX_PATH = os.path.join(_HERE, "data", "road_matrix.npz")

# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

_lock = threading.RLock()

_index: dict[str, int] = {}        # point key -> row in the matrix
_dist_m: np.ndarray | None = None  # metres,  [i][j]
_dur_s: np.ndarray | None = None   # seconds, [i][j]

_extra: dict[tuple[str, str], tuple[float, float]] = {}   # warmed at runtime
_paths: OrderedDict[str, dict] = OrderedDict()

_stats = {"lookups": 0, "matrix": 0, "warmed": 0, "estimated": 0,
          "table_calls": 0, "route_calls": 0, "errors": 0, "last_error": None}


def key(p: dict | tuple) -> str:
    """Quantised to ~1 m. Two records of the same address round to one key."""
    lat, lon = (p["lat"], p["lon"]) if isinstance(p, dict) else (p[0], p[1])
    return f"{float(lat):.5f},{float(lon):.5f}"


def _coords(p) -> tuple[float, float]:
    return ((p["lat"], p["lon"]) if isinstance(p, dict) else (p[0], p[1]))


# --------------------------------------------------------------------------
# the precomputed matrix
# --------------------------------------------------------------------------

def _load_matrix() -> None:
    global _dist_m, _dur_s, _index
    if not os.path.exists(MATRIX_PATH):
        return
    try:
        z = np.load(MATRIX_PATH, allow_pickle=False)
        keys = [str(k) for k in z["keys"]]
        _dist_m = z["dist_m"].astype(np.float32)
        _dur_s = z["dur_s"].astype(np.float32)
        _index = {k: i for i, k in enumerate(keys)}
    except Exception as exc:      # a corrupt cache must not stop the board
        _stats["last_error"] = f"matrix load failed: {exc}"
        _dist_m = _dur_s = None
        _index = {}


_load_matrix()


def has_matrix() -> bool:
    return _dist_m is not None and len(_index) > 0


# --------------------------------------------------------------------------
# the straight-line fallback -- what this module exists to replace
# --------------------------------------------------------------------------

def haversine_mi(a, b) -> float:
    (lat1, lon1), (lat2, lon2) = _coords(a), _coords(b)
    rad = math.pi / 180
    dlat, dlon = (lat2 - lat1) * rad, (lon2 - lon1) * rad
    s = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * rad) * math.cos(lat2 * rad) * math.sin(dlon / 2) ** 2)
    return 2 * R_EARTH_MI * math.asin(math.sqrt(s))


def _estimate(a, b) -> tuple[float, float]:
    """(miles, minutes) the old way."""
    mi_ = haversine_mi(a, b) * FALLBACK_ROAD_FACTOR
    return mi_, mi_ / FALLBACK_MPH * 60.0


# --------------------------------------------------------------------------
# lookups
# --------------------------------------------------------------------------

def _pair(a, b) -> tuple[float, float, str]:
    """(miles, minutes, source) for one ordered pair. Never raises, never
    blocks: the network is only touched by warm(), never by a lookup."""
    ka, kb = key(a), key(b)
    _stats["lookups"] += 1
    if ka == kb:
        return 0.0, 0.0, "matrix"

    if _dist_m is not None:
        ia, ib = _index.get(ka), _index.get(kb)
        if ia is not None and ib is not None:
            d = float(_dist_m[ia, ib])
            if d >= 0:
                _stats["matrix"] += 1
                return d / 1609.344, float(_dur_s[ia, ib]) / 60.0, "matrix"

    hit = _extra.get((ka, kb))
    if hit is not None:
        _stats["warmed"] += 1
        return hit[0] / 1609.344, hit[1] / 60.0, "warmed"

    _stats["estimated"] += 1
    mi_, min_ = _estimate(a, b)
    return mi_, min_, "estimated"


def mi(a, b) -> float:
    """Road miles between two points, in that direction."""
    return _pair(a, b)[0]


def minutes(a, b) -> float:
    """Driving minutes between two points, in that direction."""
    return _pair(a, b)[1]


def leg(a, b) -> tuple[float, float]:
    """(miles, minutes) in one lookup, for callers that need both."""
    m, t, _ = _pair(a, b)
    return m, t


def is_routed(a, b) -> bool:
    return _pair(a, b)[2] != "estimated"


def walk_mi(a, b) -> float:
    """Road miles on foot -- the shorter of the two directions.

    The public demo router only serves the car profile, and a car profile
    makes a pedestrian obey one-way streets. Someone walking to a pantry does
    not, so a westbound-only block should not add a lap of the building to
    their walk. Taking the shorter direction removes that artefact without
    pretending we have a sidewalk graph.

    Set OSRM_WALK_PROFILE (against a self-hosted OSRM carrying `foot`) and
    this becomes a real pedestrian route instead of an approximation.
    """
    return min(mi(a, b), mi(b, a))


# --------------------------------------------------------------------------
# warming: fetch what the matrix does not already know
# --------------------------------------------------------------------------

def _table(points: list[tuple[float, float]], sources=None, destinations=None):
    """One OSRM /table call. Returns (distances, durations) or None."""
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
    params = {"annotations": "distance,duration"}
    if sources is not None:
        params["sources"] = ";".join(str(i) for i in sources)
    if destinations is not None:
        params["destinations"] = ";".join(str(i) for i in destinations)
    try:
        _stats["table_calls"] += 1
        r = requests.get(f"{OSRM_URL}/table/v1/{OSRM_PROFILE}/{coords}",
                         params=params, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            _stats["errors"] += 1
            _stats["last_error"] = f"table {r.status_code}: {r.text[:120]}"
            return None
        js = r.json()
        if js.get("code") != "Ok":
            _stats["errors"] += 1
            _stats["last_error"] = f"table {js.get('code')}"
            return None
        return js["distances"], js["durations"]
    except Exception as exc:
        _stats["errors"] += 1
        _stats["last_error"] = f"table: {exc}"
        return None


def warm(points: list, budget: int = LIVE_WARM_BUDGET) -> bool:
    """Make sure every ordered pair among `points` can be answered without an
    estimate. Returns True if the set is fully covered afterwards.

    Only the points the matrix has never seen cost anything: for a board of
    230 known points plus one restaurant that registered five minutes ago,
    this is three /table calls, not 53,000.
    """
    if not ENABLED or not points:
        return _coverage(points) >= 1.0

    with _lock:
        seen: dict[str, tuple[float, float]] = {}
        for p in points:
            seen.setdefault(key(p), _coords(p))
        keys = list(seen)
        known = [k for k in keys if k in _index]
        unknown = [k for k in keys if k not in _index
                   and not any((k, o) in _extra for o in keys if o != k)]
        if not unknown:
            return True

        spent = 0
        # Each call carries the unknown points plus as many known ones as will
        # fit, and we keep the whole sub-matrix it returns -- unknown-to-known,
        # known-to-unknown and unknown-to-unknown all arrive in one response.
        head = unknown[:TABLE_MAX - 1]
        others = [k for k in keys if k not in head]
        for start in range(0, max(len(others), 1), TABLE_MAX - len(head)):
            if spent >= budget:
                return False
            chunk = others[start:start + (TABLE_MAX - len(head))]
            group = head + chunk
            got = _table([seen[k] for k in group])
            spent += 1
            if got is None:
                return False
            dist, dur = got
            for i, ka in enumerate(group):
                for j, kb in enumerate(group):
                    if i == j:
                        continue
                    d, t = dist[i][j], dur[i][j]
                    if d is None or t is None:
                        continue
                    _extra[(ka, kb)] = (float(d), float(t))
            if not chunk:
                break
        return _coverage(points) >= 1.0


def _coverage(points: list) -> float:
    """Share of ordered pairs among `points` answerable without estimating."""
    if not points:
        return 1.0
    keys = list({key(p) for p in points})
    if len(keys) < 2:
        return 1.0
    total = hit = 0
    for ka in keys:
        for kb in keys:
            if ka == kb:
                continue
            total += 1
            if (ka in _index and kb in _index
                    and _dist_m is not None and _dist_m[_index[ka], _index[kb]] >= 0):
                hit += 1
            elif (ka, kb) in _extra:
                hit += 1
    return hit / total if total else 1.0


# --------------------------------------------------------------------------
# geometry: the line actually drawn on the map
# --------------------------------------------------------------------------

def path(points: list, profile: str | None = None) -> dict:
    """The real route through `points`, in the order given.

    Returns per-leg geometry, so the board can keep colouring the collection
    phase differently from the delivery phase, and `routed` so it can say
    which kind of line the viewer is looking at.

        {"routed": bool, "miles": float, "minutes": float,
         "legs": [{"coords": [[lat, lon], ...], "miles": .., "minutes": ..}]}
    """
    pts = []
    for p in points:
        c = _coords(p)
        if not pts or pts[-1] != c:
            pts.append(c)              # OSRM rejects a zero-length leg
    if len(pts) < 2:
        return {"routed": False, "miles": 0.0, "minutes": 0.0, "legs": []}

    prof = profile or OSRM_PROFILE
    ck = prof + "|" + ";".join(f"{a:.5f},{b:.5f}" for a, b in pts)
    with _lock:
        if ck in _paths:
            _paths.move_to_end(ck)
            return _paths[ck]

    out = _fetch_path(pts, prof) if ENABLED else None
    if out is None:
        out = _straight_path(points)

    with _lock:
        _paths[ck] = out
        while len(_paths) > PATH_CACHE_MAX:
            _paths.popitem(last=False)
    return out


def _fetch_path(pts: list[tuple[float, float]], profile: str) -> dict | None:
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in pts)
    try:
        _stats["route_calls"] += 1
        r = requests.get(
            f"{OSRM_URL}/route/v1/{profile}/{coords}",
            params={
                "overview": "full",
                "geometries": "geojson",
                # per-leg geometry, so collection and delivery stay separable
                "steps": "true",
                # no forced U-turn at a waypoint just to preserve heading; this
                # is what stops a multi-stop plan doubling back on itself
                "continue_straight": "false",
            },
            timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            _stats["errors"] += 1
            _stats["last_error"] = f"route {r.status_code}: {r.text[:120]}"
            return None
        js = r.json()
        if js.get("code") != "Ok" or not js.get("routes"):
            _stats["errors"] += 1
            _stats["last_error"] = f"route {js.get('code')}"
            return None
    except Exception as exc:
        _stats["errors"] += 1
        _stats["last_error"] = f"route: {exc}"
        return None

    route = js["routes"][0]
    legs = []
    for lg in route["legs"]:
        line: list[list[float]] = []
        for st in lg.get("steps", []):
            for lon, lat in st.get("geometry", {}).get("coordinates", []):
                pt = [round(lat, 6), round(lon, 6)]
                if not line or line[-1] != pt:
                    line.append(pt)
        legs.append({"coords": line,
                     "miles": round(lg["distance"] / 1609.344, 3),
                     "minutes": round(lg["duration"] / 60.0, 2)})
    return {"routed": True,
            "miles": round(route["distance"] / 1609.344, 3),
            "minutes": round(route["duration"] / 60.0, 2),
            "legs": legs}


def _straight_path(points: list) -> dict:
    """What the board drew before: a line per leg, corner to corner."""
    legs = []
    for a, b in zip(points, points[1:]):
        m, t = _estimate(a, b)
        ca, cb = _coords(a), _coords(b)
        legs.append({"coords": [[ca[0], ca[1]], [cb[0], cb[1]]],
                     "miles": round(m, 3), "minutes": round(t, 2)})
    return {"routed": False,
            "miles": round(sum(l["miles"] for l in legs), 3),
            "minutes": round(sum(l["minutes"] for l in legs), 2),
            "legs": legs}


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def status() -> dict:
    est = _stats["estimated"]
    total = max(_stats["lookups"], 1)
    return {
        "enabled": ENABLED,
        "engine": "osrm" if ENABLED else "straight-line",
        "url": OSRM_URL if ENABLED else None,
        "profile": OSRM_PROFILE,
        "matrixPoints": len(_index),
        "matrixLoaded": has_matrix(),
        "warmedPairs": len(_extra),
        "routedShare": round(1 - est / total, 4),
        "lastError": _stats["last_error"],
        **{k: _stats[k] for k in ("lookups", "matrix", "warmed", "estimated",
                                  "table_calls", "route_calls", "errors")},
    }
