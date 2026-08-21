"""Area-level need forecast: linear trend + month-of-year seasonality + a
fellowship-program control, fit by OLS on real monthly history.

Answers a different question than spatial.py's Gi* (is this block a real
cluster right now) and demo_data.py's per-block trend (a block's own last 5
sparse counts): is an entire NEIGHBORHOOD trending up or down, with enough
monthly history -- up to 108 months per area -- to actually control for
seasonality and a known one-off program effect, rather than reading noise
into a handful of block-level points.

This is the build spec's own already-documented area-forecast methodology
(CLAUDE.md 3.2), reintroduced after it was dropped along with the earlier,
now-superseded /roles system's needs.py.
"""

from __future__ import annotations

from datetime import datetime as _dt

_CACHE: dict[str, dict] = {}


def area_trends(rows_fn) -> dict[str, dict]:
    """area -> {trendPerMonth, pValue, significant, direction}.

    `rows_fn` is demo_data._rows, passed in rather than imported, so this
    module has no import-time dependency on demo_data's own module state.

    A trend is only ever reported as up/down when it clears p < 0.05 --
    anything weaker is `direction: "flat"` and callers should treat it as no
    signal at all, not a weak one. Real result on this data: East Village and
    Gaslamp do NOT clear that bar once seasonality and the fellowship program
    are controlled for, even though several of their individual blocks show
    strong Gi* clusters and trends -- a block-level pattern is not the same
    claim as a neighbourhood-wide one, and this keeps the two from being
    conflated.
    """
    if _CACHE:
        return _CACHE

    import numpy as np
    from scipy import stats
    from collections import defaultdict

    rows = [r for r in rows_fn("DowntownCounts_Monthly.csv")
            if r["area_type"] == "neighborhood" and r["component"] == "total"
            and r["method"] != "PRE2017" and r["count"] != ""]

    by_area: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_area[r["area"]].append(r)

    for area, recs in by_area.items():
        recs = sorted(recs, key=lambda r: r["date"])
        n = len(recs)
        if n < 24:            # too little history to fit seasonality on
            continue
        dates = [_dt.fromisoformat(r["date"]) for r in recs]
        t0 = dates[0]
        x_trend = np.array([(d - t0).days / 30.4375 for d in dates])
        y = np.array([float(r["count"]) for r in recs])
        months = np.array([d.month for d in dates])
        fellowship = np.array([1.0 if r["fellowship_month"] == "True" else 0.0
                               for r in recs])

        cols = [np.ones(n), x_trend]
        for m in range(2, 13):
            cols.append((months == m).astype(float))
        cols.append(fellowship)
        X = np.array(cols).T

        coef, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ coef
        dof = n - X.shape[1]
        if dof <= 0:
            continue
        sigma2 = (resid @ resid) / dof
        se = np.sqrt(np.diag(sigma2 * np.linalg.pinv(X.T @ X)))

        trend = float(coef[1])
        t_stat = trend / se[1] if se[1] > 0 else 0.0
        p = float(2 * stats.t.sf(abs(t_stat), dof))
        significant = p < 0.05

        _CACHE[area] = {
            "trendPerMonth": round(trend, 2),
            "pValue": round(p, 4),
            "significant": significant,
            "direction": ("up" if significant and trend > 0 else
                         "down" if significant and trend < 0 else "flat"),
        }
    return _CACHE
