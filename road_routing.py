"""Small OSRM HTTP client for real OpenStreetMap road routes."""

from __future__ import annotations

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen


OSRM_BASE_URL = "https://router.project-osrm.org"
METERS_PER_MILE = 1609.344


def _get_json(path: str, params: dict[str, str]) -> dict:
    url = f"{OSRM_BASE_URL}{path}?{urlencode(params, safe=';')}"
    request = Request(url, headers={"User-Agent": "Build-For-Good-Hackathon/1.0"})
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if payload.get("code") != "Ok":
        raise RuntimeError(f"OSRM error: {payload.get('code')} {payload.get('message', '')}")
    return payload


def _coordinate_string(points: list[tuple[float, float]]) -> str:
    return ";".join(f"{lon:.7f},{lat:.7f}" for lat, lon in points)


def road_distances_one_to_many_miles(
    source: tuple[float, float], destinations: list[tuple[float, float]], chunk_size: int = 50
) -> list[float]:
    """Driving distances from one source to many destinations, preserving order."""
    distances = []
    for start in range(0, len(destinations), chunk_size):
        chunk = destinations[start : start + chunk_size]
        points = [source, *chunk]
        destination_indexes = ";".join(str(i) for i in range(1, len(points)))
        payload = _get_json(
            f"/table/v1/driving/{_coordinate_string(points)}",
            {"sources": "0", "destinations": destination_indexes, "annotations": "distance"},
        )
        row = payload["distances"][0]
        if any(value is None for value in row):
            raise RuntimeError("OSRM could not find a road route to one or more destinations")
        distances.extend(value / METERS_PER_MILE for value in row)
    return distances


def road_distances_many_to_one_miles(
    sources: list[tuple[float, float]], destination: tuple[float, float]
) -> list[float]:
    """Driving distances from many sources to one destination, preserving order."""
    points = [*sources, destination]
    source_indexes = ";".join(str(i) for i in range(len(sources)))
    payload = _get_json(
        f"/table/v1/driving/{_coordinate_string(points)}",
        {
            "sources": source_indexes,
            "destinations": str(len(sources)),
            "annotations": "distance",
        },
    )
    values = [row[0] for row in payload["distances"]]
    if any(value is None for value in values):
        raise RuntimeError("OSRM could not find a road route from one or more sources")
    return [value / METERS_PER_MILE for value in values]


def road_route(points: list[tuple[float, float]]) -> dict:
    """Return full driving geometry, total miles/minutes, and per-leg miles."""
    payload = _get_json(
        f"/route/v1/driving/{_coordinate_string(points)}",
        {"steps": "false", "geometries": "geojson", "overview": "full"},
    )
    route = payload["routes"][0]
    return {
        "distance_miles": route["distance"] / METERS_PER_MILE,
        "duration_minutes": route["duration"] / 60,
        "leg_miles": [leg["distance"] / METERS_PER_MILE for leg in route["legs"]],
        "geometry": route["geometry"],
    }
