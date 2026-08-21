"""Block-level need and area forecast, derived from the DSDP count record.

Everything here follows the data rules in the build spec section 3.4:
  1. never sum `total` with the individual/tent/vehicle components
  2. Panel261 only for anything measured across time
  3. join on `area`, never on the raw neighborhood label
  4. `Outside Perimeter` is null-not-zero before 2021-04
  5. 2025 is missing Jul, Aug, Oct and Nov -- dropped, never interpolated
  6. polygons, not centroids, for point-in-block work
  7. PRE2017 rows carry `total` only and stay out of component work
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from shapely.geometry import Point, shape
from shapely.ops import transform

DATA_DIR = Path(__file__).resolve().parent.parent / "dataset"

# The build spec proposed 800 m ("half a mile, a ten minute walk"). On this
# grid that is wrong: downtown San Diego blocks run roughly 90x60 m, so an
# 800 m radius sweeps in 120-190 of the 382 cells and the score ends up
# measuring how central a point is rather than who it serves -- it actually
# inverts the ranking (Rachel's Women's Center places 1st at 800 m and 8th at
# 200 m), and the trend term cancels to near zero. 300 m is still a 3-4 minute
# walk, yields ~36 blocks per destination, and reproduces the known high-need
# East Village sites at the top. Tunable live via CONFIG["SERVICE_RADIUS_M"].
SERVICE_RADIUS_M = 300
TREND_WINDOW = 5        # count dates in the block-level slope
FORECAST_MONTHS = 12

# Local equirectangular frame centred on downtown San Diego. Over a grid this
# small the distortion is far below the precision of a visual street count, and
# it keeps pyproj/geopandas out of the dependency list.
_LON0, _LAT0 = -117.1553, 32.7100
_M_PER_DEG_LAT = 110_574.0
_M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(_LAT0))


def to_meters(lon: float, lat: float) -> tuple[float, float]:
    return (lon - _LON0) * _M_PER_DEG_LON, (lat - _LAT0) * _M_PER_DEG_LAT


def _project(x, y, z=None):
    return (x - _LON0) * _M_PER_DEG_LON, (y - _LAT0) * _M_PER_DEG_LAT


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Block:
    block_id: str
    area: str
    lon: float
    lat: float
    streets: tuple[str, str, str, str]  # N, E, S, W
    polygon_m: object                   # shapely polygon in the metre frame
    need_now: float = 0.0
    need_trend: float = 0.0

    @property
    def label(self) -> str:
        n, e, s, w = self.streets
        pretty = lambda s: (s or "").replace("_", " ").title()
        return f"{pretty(e)} & {pretty(n)}"


# --------------------------------------------------------------------------
# block geometry
# --------------------------------------------------------------------------

def load_block_geometry() -> dict[str, Block]:
    """382 grid polygons, keyed by block_id.

    The GeoJSON carries `neighborhood` (the raw label). Rule 3 says join on
    `area`, so the canonical area comes from Downtown_BlockGrid.csv instead.
    """
    grid = pd.read_csv(DATA_DIR / "Downtown_BlockGrid.csv")
    area_by_id = dict(zip(grid.block_id, grid.area))

    with open(DATA_DIR / "Downtown_BlockGrid.geojson") as fh:
        fc = json.load(fh)

    blocks: dict[str, Block] = {}
    for feat in fc["features"]:
        p = feat["properties"]
        bid = p["block_id"]
        poly = transform(_project, shape(feat["geometry"]))
        blocks[bid] = Block(
            block_id=bid,
            area=area_by_id.get(bid, p.get("neighborhood", "")),
            lon=float(p["lon"]),
            lat=float(p["lat"]),
            streets=(p.get("st_north"), p.get("st_east"), p.get("st_south"), p.get("st_west")),
            polygon_m=poly,
        )
    return blocks


# --------------------------------------------------------------------------
# 3.1 block need
# --------------------------------------------------------------------------

def load_panel() -> pd.DataFrame:
    """Panel261 on the raw observed basis: individuals + tents + vehicles.

    Rule 1: this is the *component* basis. It is deliberately not the adjusted
    `total`, and the two must never be added together.
    """
    df = pd.read_csv(DATA_DIR / "BlockLevel_Counts_Panel261.csv")
    df["count_date"] = pd.to_datetime(df["count_date"])
    df["observed"] = df.individuals + df.tents_structures + df.vehicles
    return df


def _slope_per_year(dates: pd.Series, values: pd.Series) -> float:
    """OLS slope in persons per year. Returns 0.0 if it cannot be fit."""
    if len(dates) < 2:
        return 0.0
    t = (dates - dates.min()).dt.total_seconds().to_numpy() / (365.25 * 86400)
    y = values.to_numpy(dtype=float)
    ok = ~np.isnan(y)
    if ok.sum() < 2 or np.ptp(t[ok]) == 0:
        return 0.0
    return float(np.polyfit(t[ok], y[ok], 1)[0])


def block_need(panel: pd.DataFrame | None = None) -> pd.DataFrame:
    """need_now (latest count date) and need_trend (persons/year over last 5)."""
    panel = load_panel() if panel is None else panel
    dates = sorted(panel.count_date.unique())
    latest = dates[-1]
    window = dates[-TREND_WINDOW:]

    now = (
        panel[panel.count_date == latest]
        .set_index("block_id")["observed"]
        .rename("need_now")
    )

    recent = panel[panel.count_date.isin(window)].sort_values("count_date")
    trend = (
        recent.groupby("block_id")
        .apply(lambda g: _slope_per_year(g.count_date, g.observed), include_groups=False)
        .rename("need_trend")
    )

    area = panel.groupby("block_id")["area"].first()
    out = pd.concat([now, trend, area], axis=1).reset_index()
    out.attrs["latest_count_date"] = pd.Timestamp(latest)
    out.attrs["trend_window"] = [pd.Timestamp(d) for d in window]
    return out


# --------------------------------------------------------------------------
# 3.2 area forecast
# --------------------------------------------------------------------------

def load_monthly() -> pd.DataFrame:
    """Neighborhood-level adjusted totals, cleaned per rules 4, 5 and 7."""
    m = pd.read_csv(DATA_DIR / "DowntownCounts_Monthly.csv")
    m["date"] = pd.to_datetime(m["date"])
    m = m[(m.area_type == "neighborhood") & (m.component == "total")].copy()

    # rule 7 -- PRE2017 has no component breakdown
    m = m[m.method != "PRE2017"]

    # rule 4 -- Outside Perimeter was not in the program before April 2021.
    # Joining it mid-series as zero fabricates a step change.
    op_pre = (m.area == "Outside Perimeter") & (m.date < "2021-04-01")
    m = m[~op_pre]

    # rule 5 -- 2025 Jul/Aug/Oct/Nov are not reported. Dropped, not filled.
    m = m[m["count"].notna()]

    m["fellowship_month"] = m["fellowship_month"].astype(bool)
    return m.sort_values(["area", "date"])


def _design_matrix(dates: pd.Series, fellowship: pd.Series, t0: pd.Timestamp) -> np.ndarray:
    """[intercept, trend_years, month_2..month_12, fellowship]."""
    t = ((dates - t0).dt.days.to_numpy() / 365.25).reshape(-1, 1)
    months = dates.dt.month.to_numpy()
    dummies = np.zeros((len(dates), 11))
    for i, mo in enumerate(months):
        if mo >= 2:
            dummies[i, mo - 2] = 1.0
    fell = fellowship.to_numpy(dtype=float).reshape(-1, 1)
    return np.hstack([np.ones((len(dates), 1)), t, dummies, fell])


def area_forecast(monthly: pd.DataFrame | None = None) -> dict[str, dict]:
    """Per area: linear trend + month-of-year seasonality + fellowship regressor.

    `fellowship_month` marks months when Fellowship volunteers swelled the
    counting crew -- 10 months a year in 2017, none after 2020. Without the
    regressor that staffing change is absorbed into the trend and the long
    decline reads far steeper than it is. Forecasts are produced with the
    regressor held at 0 (present-day staffing).
    """
    monthly = load_monthly() if monthly is None else monthly
    out: dict[str, dict] = {}

    for area, g in monthly.groupby("area"):
        g = g.sort_values("date")
        t0 = g.date.min()
        X = _design_matrix(g.date, g.fellowship_month, t0)
        y = g["count"].to_numpy(dtype=float)

        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        fitted = X @ beta
        resid = y - fitted
        ss_tot = float(((y - y.mean()) ** 2).sum())
        r2 = 1.0 - float((resid**2).sum()) / ss_tot if ss_tot > 0 else float("nan")

        last_date = g.date.max()
        future = pd.date_range(
            last_date + pd.DateOffset(months=1), periods=FORECAST_MONTHS, freq="MS"
        )
        Xf = _design_matrix(
            pd.Series(future), pd.Series([False] * FORECAST_MONTHS), t0
        )
        proj = Xf @ beta

        latest_actual = float(g["count"].iloc[-1])
        out[area] = {
            "area": area,
            "n_obs": int(len(g)),
            "r2": r2,
            "trend_per_year": float(beta[1]),
            "fellowship_effect": float(beta[-1]),
            "latest_date": last_date,
            "latest_actual": latest_actual,
            "history": [
                {"date": d.date().isoformat(), "count": float(c)}
                for d, c in zip(g.date, g["count"])
            ],
            "forecast": [
                {"date": d.date().isoformat(), "count": float(v)}
                for d, v in zip(future, proj)
            ],
            "forecast_delta": float(proj[-1] - latest_actual),
        }
    return out


# --------------------------------------------------------------------------
# 3.3 destination need
# --------------------------------------------------------------------------

class NeedIndex:
    """Everything a destination or donor needs to be scored against the grid."""

    def __init__(self) -> None:
        self.blocks = load_block_geometry()
        self.panel = load_panel()
        needs = block_need(self.panel)
        self.latest_count_date: pd.Timestamp = needs.attrs["latest_count_date"]
        self.trend_window: list = needs.attrs["trend_window"]

        for row in needs.itertuples():
            b = self.blocks.get(row.block_id)
            if b is not None:
                b.need_now = float(row.need_now)
                b.need_trend = float(row.need_trend)

        # 261 of the 382 grid cells are in the comparable panel; the other 121
        # joined the footprint in 2022 and carry need 0 by construction.
        self.panel_block_ids = set(needs.block_id)
        self.forecast = area_forecast()
        self._profile_cache: dict[tuple[float, float], list[tuple[float, Block]]] = {}

    # -- lookups ---------------------------------------------------------
    def block_at(self, lat: float, lon: float) -> Block | None:
        """Point-in-polygon, per rule 6. Centroids are not used."""
        pt = Point(*to_meters(lon, lat))
        for b in self.blocks.values():
            if b.polygon_m.contains(pt):
                return b
        return None

    def distance_profile(self, lat: float, lon: float) -> list[tuple[float, Block]]:
        """Every block with its polygon distance in metres, nearest first.

        Computed once per location and cached, so changing the service radius
        live is a filter over this list rather than 382 fresh polygon distance
        computations per destination.
        """
        key = (round(lat, 6), round(lon, 6))
        hit = self._profile_cache.get(key)
        if hit is None:
            pt = Point(*to_meters(lon, lat))
            hit = sorted(
                ((b.polygon_m.distance(pt), b) for b in self.blocks.values()),
                key=lambda t: t[0],
            )
            self._profile_cache[key] = hit
        return hit

    def blocks_within(self, lat: float, lon: float, radius_m: float = SERVICE_RADIUS_M) -> list[Block]:
        """Grid cells whose polygon comes within radius_m of the point."""
        out = []
        for dist, b in self.distance_profile(lat, lon):
            if dist > radius_m:
                break  # profile is sorted, so the rest are further still
            out.append(b)
        return out

    def area_of(self, lat: float, lon: float) -> str | None:
        b = self.block_at(lat, lon)
        if b:
            return b.area
        near = self.blocks_within(lat, lon, SERVICE_RADIUS_M)
        if not near:
            return None
        pt = Point(*to_meters(lon, lat))
        return min(near, key=lambda b: b.polygon_m.distance(pt)).area

    def score(self, lat: float, lon: float, radius_m: float = SERVICE_RADIUS_M) -> dict:
        """need_now / need_trend for a delivery point.

        A destination outside the grid scores 0 and ranks last. That is the
        correct answer, not a bug: it does not serve downtown.
        """
        served = self.blocks_within(lat, lon, radius_m)
        need_now = sum(b.need_now for b in served)
        need_trend = sum(b.need_trend for b in served)

        area = self.area_of(lat, lon)
        delta = self.forecast.get(area, {}).get("forecast_delta", 0.0) if area else 0.0

        return {
            "area": area,
            "served_block_ids": [b.block_id for b in served],
            "n_blocks": len(served),
            "need_now": float(need_now),
            "need_trend": float(need_trend + delta),
            "block_trend": float(need_trend),
            "forecast_delta": float(delta),
        }


@lru_cache(maxsize=1)
def get_index() -> NeedIndex:
    return NeedIndex()


def rescore(dests: list[dict], radius_m: float, idx: "NeedIndex | None" = None) -> list[dict]:
    """Recompute every destination's need at a new service radius.

    Returns new dicts; the caller's list is left alone. Hubspots keep their
    single-block basis -- a hubspot *is* one block, so a radius does not apply.
    """
    idx = idx or get_index()
    out = []
    for d in dests:
        if d.get("dest_type") == "hubspot":
            out.append(dict(d))
            continue
        s = idx.score(d["lat"], d["lon"], radius_m)
        out.append({**d,
                    "served_block_ids": s["served_block_ids"],
                    "n_blocks": s["n_blocks"],
                    "need_now": s["need_now"],
                    "need_trend": s["need_trend"],
                    "area": s["area"]})
    return out
