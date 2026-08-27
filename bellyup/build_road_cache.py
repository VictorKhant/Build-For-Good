"""Build `data/road_matrix.npz` -- every ordered road distance the board needs.

Run once, commit the result:

    ../.venv/bin/python build_road_cache.py

Why precompute at all: a single dispatch click scores roughly 8 collectors
against 161 candidate blocks, four legs each. Asking a routing service for
those one at a time would be ~5,000 HTTP requests per click. Asking for them
in bulk, once, and shipping the answer with the app makes a lookup an array
index -- so the board is routed *and* instant, and works with the router
unreachable.

The set is every point that exists before anyone touches the board: hotspot
blocks, agency depots, mobile pantry sites, drop-off sites and the curated
businesses. Restaurants that register during a session are not here by
definition; `routing.warm()` handles those live, in a few calls.

Distances are directed. d(a, b) != d(b, a) downtown -- Front and First are a
one-way pair -- so the full square is stored, not a triangle.
"""

from __future__ import annotations

import sys
import time

import numpy as np

import demo_data as dd
import dispatch
import routing

CHUNK = 50          # 2 chunks per request = 100 coords, OSRM's cap
PAUSE = 0.6         # be a good citizen on the public demo server
RETRIES = 3


def static_points() -> list[dict]:
    """Everything with a fixed location, deduplicated by rounded coordinate."""
    hotspots = dd.load_hotspots()
    agencies = dd.load_agencies()
    pantries = dd.load_pantries()
    suppliers = dd.load_suppliers(hotspots)

    groups = [("hotspot", hotspots), ("agency", agencies),
              ("pantry", pantries), ("supplier", suppliers),
              ("dropoff", dispatch.dropoff_sites(agencies))]

    out: dict[str, dict] = {}
    for kind, rows in groups:
        for r in rows:
            if r.get("lat") is None or r.get("lon") is None:
                continue
            k = routing.key(r)
            out.setdefault(k, {"key": k, "lat": float(r["lat"]),
                               "lon": float(r["lon"]),
                               "kind": kind,
                               "name": r.get("name") or r.get("location") or k})
    return list(out.values())


def fetch(points: list[dict], a: list[int], b: list[int]):
    """Full sub-matrix over chunks a+b, retried."""
    idx = a + b
    coords = [(points[i]["lat"], points[i]["lon"]) for i in idx]
    for attempt in range(RETRIES):
        got = routing._table(coords)
        if got is not None:
            return idx, got
        time.sleep(1.5 * (attempt + 1))
    return idx, None


def main() -> int:
    pts = static_points()
    n = len(pts)
    print(f"{n} static points "
          + ", ".join(f"{k}={sum(1 for p in pts if p['kind'] == k)}"
                      for k in ("hotspot", "agency", "pantry", "supplier", "dropoff")))

    dist = np.full((n, n), -1.0, dtype=np.float32)
    dur = np.full((n, n), -1.0, dtype=np.float32)
    np.fill_diagonal(dist, 0.0)
    np.fill_diagonal(dur, 0.0)

    chunks = [list(range(s, min(s + CHUNK, n))) for s in range(0, n, CHUNK)]
    todo = [(i, j) for i in range(len(chunks)) for j in range(i, len(chunks))]
    failed = 0

    for step, (ci, cj) in enumerate(todo, 1):
        a = chunks[ci]
        b = chunks[cj] if cj != ci else []
        idx, got = fetch(pts, a, b)
        pct = step / len(todo) * 100
        if got is None:
            failed += 1
            print(f"  [{pct:5.1f}%] chunk {ci}x{cj}: FAILED "
                  f"({routing._stats['last_error']})")
            continue
        d, t = got
        for x, gi in enumerate(idx):
            for y, gj in enumerate(idx):
                dv, tv = d[x][y], t[x][y]
                if dv is not None and tv is not None:
                    dist[gi, gj] = dv
                    dur[gi, gj] = tv
        print(f"  [{pct:5.1f}%] chunk {ci}x{cj}: {len(idx)} coords")
        time.sleep(PAUSE)

    filled = int((dist >= 0).sum())
    total = n * n
    print(f"\n{filled}/{total} ordered pairs routed ({filled / total * 100:.1f}%)"
          + (f", {failed} chunk requests failed" if failed else ""))

    if filled / total < 0.9:
        print("Too sparse to ship -- refusing to overwrite the existing cache.")
        return 1

    # How much the straight-line model was off by, since that is the whole
    # claim this change makes.
    sample = [(i, j) for i in range(0, n, 7) for j in range(0, n, 11)
              if i != j and dist[i, j] > 0]
    if sample:
        ratio = [dist[i, j] / 1609.344
                 / (routing.haversine_mi(pts[i], pts[j]) or 1e-9) for i, j in sample]
        ratio.sort()
        print(f"road / straight-line over {len(ratio)} pairs: "
              f"median {ratio[len(ratio) // 2]:.2f}x, "
              f"p90 {ratio[int(len(ratio) * 0.9)]:.2f}x, "
              f"max {ratio[-1]:.2f}x   (the old constant was "
              f"{routing.FALLBACK_ROAD_FACTOR}x, flat)")

    np.savez_compressed(
        routing.MATRIX_PATH,
        keys=np.array([p["key"] for p in pts]),
        dist_m=dist, dur_s=dur,
        names=np.array([p["name"] for p in pts]),
        kinds=np.array([p["kind"] for p in pts]))
    import os
    print(f"wrote {routing.MATRIX_PATH} "
          f"({os.path.getsize(routing.MATRIX_PATH) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
