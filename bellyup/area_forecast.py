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

import math
from datetime import datetime as _dt

_CACHE: dict[str, dict] = {}


# --------------------------------------------------------------- t-test tail
# scipy is one call away from being a hard dependency of the WHOLE board:
# load_hotspots() calls area_trends(), so `from scipy import stats` failing
# took out /api/board entirely -- every restaurant, every agency, every
# pantry -- to decorate a handful of blocks with a reinforced/contradicted
# tag. The p-value gate is worth keeping exactly, the dependency is not, so
# the tail is computed here and scipy is used only when it happens to be
# installed.


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Numerical Recipes 6.4)."""
    MAXIT, EPS, FPMIN = 300, 3.0e-16, 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                  + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_sf_two_sided(t_stat: float, dof: float) -> float:
    """P(|T| > |t_stat|) for Student's t with `dof` degrees of freedom.

    Same number as 2 * scipy.stats.t.sf(abs(t), dof). Checked against scipy
    1.17 over 208 (t, dof) points spanning dof 1-5000: worst relative error
    4.3e-08, and that is at p ~= 1, nowhere near the gate. The p < 0.05
    verdict this actually feeds agreed on all 7800 points tested.
    """
    if dof <= 0:
        return 1.0
    try:
        from scipy import stats           # exact same number, just faster
        return float(2.0 * stats.t.sf(abs(t_stat), dof))
    except Exception:
        pass
    return float(_betainc(dof / 2.0, 0.5, dof / (dof + t_stat * t_stat)))


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
        p = t_sf_two_sided(t_stat, dof)
        significant = p < 0.05

        _CACHE[area] = {
            "trendPerMonth": round(trend, 2),
            "pValue": round(p, 4),
            "significant": significant,
            "direction": ("up" if significant and trend > 0 else
                         "down" if significant and trend < 0 else "flat"),
        }
    return _CACHE
