"""FastAPI interface for BellyUp SQL data and optimization methods."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from calc.database import connect, read_table, table_names
from calc.sync_live_suppliers import sync_live_suppliers


def records(frame) -> list[dict]:
    """Return JSON-safe rows (numeric NaN must become JSON null)."""
    return frame.astype(object).where(frame.notna(), None).to_dict("records")

app = FastAPI(title="BellyUp Optimization API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class LiveBoardPayload(BaseModel):
    suppliers: list[dict]

PUBLIC_TABLES = {
    "businesses", "agencies", "hotspots", "mobile_pantries",
    "donation_reports", "supplier_supply", "agency_capacity",
    "simulation_entities", "route_matrix",
    "standard_baseline_allocations", "standard_optimized_allocations",
    "standard_agency_summary", "standard_hotspot_summary",
    "simulation_baseline_allocations", "simulation_optimized_allocations",
    "simulation_agency_summary", "simulation_hotspot_summary",
    "simulation_greedy_vehicle_routes", "simulation_greedy_vehicle_route_stops",
    "simulation_optimized_vehicle_routes", "simulation_optimized_vehicle_route_stops",
    "simulation_route_optimized_allocations",
    "simulation_route_optimized_vehicle_routes", "simulation_route_optimized_vehicle_route_stops",
}

@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/ui/")

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "database": "calc/sql/bellyup.db"}

@app.get("/tables")
def tables() -> dict:
    names = [name for name in table_names() if name in PUBLIC_TABLES]
    with connect() as connection:
        counts = {
            name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in names
        }
    return {"tables": [{"name": name, "rows": counts[name]} for name in names]}

@app.get("/data/{table_name}")
def data(
    table_name: str,
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict:
    if table_name not in PUBLIC_TABLES or table_name not in table_names():
        raise HTTPException(status_code=404, detail="Unknown or unavailable table")
    with connect() as connection:
        frame = __import__("pandas").read_sql_query(
            f'SELECT * FROM "{table_name}" LIMIT ? OFFSET ?', connection,
            params=(limit, offset),
        )
        total = connection.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
    return {"table": table_name, "total": total, "rows": records(frame)}

@app.post("/optimize/{mode}")
def optimize(mode: str) -> dict:
    if mode not in {"standard", "simulation"}:
        raise HTTPException(status_code=400, detail="mode must be standard or simulation")
    command = [sys.executable, str(PROJECT_ROOT / "optim" / "optimize_allocations.py")]
    if mode == "simulation":
        command.append("--simulation")
    completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True)
    if completed.returncode:
        raise HTTPException(status_code=500, detail=completed.stderr or completed.stdout)
    if mode == "simulation":
        for method in ("greedy", "optimized"):
            route_run = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "optim" / "build_vehicle_routes.py"), "--method", method],
                cwd=PROJECT_ROOT, capture_output=True, text=True,
            )
            if route_run.returncode:
                raise HTTPException(status_code=500, detail=route_run.stderr or route_run.stdout)
        sequential_run = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "optim" / "optimize_sequential_routes.py")],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )
        if sequential_run.returncode:
            raise HTTPException(status_code=500, detail=sequential_run.stderr or sequential_run.stdout)
        route_run = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "optim" / "build_vehicle_routes.py"), "--method", "route_optimized"],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )
        if route_run.returncode:
            raise HTTPException(status_code=500, detail=route_run.stderr or route_run.stdout)
    output = PROJECT_ROOT / "optim" / "output"
    if mode == "simulation":
        output /= "simulation"
    comparison = json.loads((output / "comparison.json").read_text())
    return {"mode": mode, "comparison": comparison}

@app.post("/simulation/live-suppliers")
def live_suppliers(payload: LiveBoardPayload) -> dict:
    return sync_live_suppliers(payload.suppliers)

@app.get("/results/{mode}/{method}")
def results(mode: str, method: str, limit: int = Query(500, ge=1, le=5000)) -> dict:
    if mode not in {"standard", "simulation"} or method not in {"greedy", "optimized", "route_optimized"}:
        raise HTTPException(status_code=400, detail="invalid mode or method")
    table = (
        f"{mode}_baseline_allocations" if method == "greedy"
        else f"{mode}_route_optimized_allocations" if method == "route_optimized"
        else f"{mode}_optimized_allocations"
    )
    if table not in table_names():
        raise HTTPException(status_code=404, detail="Run optimization first")
    frame = read_table(table).head(limit)
    return {"mode": mode, "method": method, "total": len(read_table(table)), "rows": records(frame)}

@app.get("/routes/simulation/{method}")
def simulation_routes(method: str) -> dict:
    if method not in {"greedy", "optimized", "route_optimized"}:
        raise HTTPException(status_code=400, detail="method must be greedy, optimized, or route_optimized")
    route_table = f"simulation_{method}_vehicle_routes"
    stop_table = f"simulation_{method}_vehicle_route_stops"
    if route_table not in table_names():
        raise HTTPException(status_code=404, detail="Run simulation optimization first")
    routes = read_table(route_table)
    stops = read_table(stop_table)
    rows = []
    for route in routes.to_dict("records"):
        geometry = route.pop("geometry_geojson", None)
        route["geometry"] = json.loads(geometry) if geometry else None
        route["stops"] = records(stops[stops.route_id.eq(route["route_id"])].sort_values("stop_sequence"))
        rows.append(route)
    return {"total": len(rows), "routes": rows}

app.mount("/ui", StaticFiles(directory=PROJECT_ROOT / "ui", html=True), name="ui")
