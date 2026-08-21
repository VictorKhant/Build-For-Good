#!/usr/bin/env python3
"""Build a self-contained-data Leaflet map for hotspots, agencies, and suppliers."""

from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def read_csv(name: str) -> list[dict[str, str]]:
    with (ROOT / name).open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    hotspots = [
        {
            "block_id": row["block_id"],
            "area": row["area"],
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "demand": float(row["demand"]),
            "rank": int(row["demand_rank"]),
            "date": row["count_date"],
        }
        for row in read_csv("demand_points.csv")
        if float(row["demand"]) > 0
    ]
    agencies = [
        {
            "id": row["agency_id"],
            "name": row["name"],
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "status": row["data_status"],
        }
        for row in read_csv("agency_points.csv")
    ]
    suppliers = [
        {
            "id": row["donation_id"],
            "name": row["supplier_name"],
            "type": row["supplier_type"],
            "lat": float(row["lat"]),
            "lon": float(row["lon"]),
            "food_type": row["food_type"],
            "quantity_lbs": float(row["quantity_lbs"]),
        }
        for row in read_csv("demo_donation_inputs.csv")
    ]
    routes = [
        {
            "donation_id": row["donation_id"],
            "supplier_name": row["supplier_name"],
            "agency_name": row["agency_name"],
            "hotspot_block_id": row["hotspot_block_id"],
            "miles": float(row["route_total_miles"]),
            "minutes": float(row["route_duration_minutes"]),
            "geometry": json.loads(row["route_geojson"]),
        }
        for row in read_csv("demo_donation_routes.csv")
        if row.get("distance_method") == "osrm_openstreetmap_driving"
    ]

    template = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>San Diego Food Support Hotspots</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map { height: 100%; margin: 0; }
    body { font-family: system-ui, sans-serif; }
    .panel { position:absolute; z-index:1000; top:14px; left:54px; width:310px;
      background:rgba(255,255,255,.96); padding:14px 16px; border-radius:12px;
      box-shadow:0 4px 18px #0003; }
    .panel h1 { font-size:18px; margin:0 0 6px; }
    .panel p { font-size:12px; line-height:1.4; margin:5px 0; color:#333; }
    .legend { display:grid; grid-template-columns:14px 1fr; gap:6px 8px; align-items:center; }
    .dot { width:12px; height:12px; border-radius:50%; display:inline-block; }
    .warning { color:#8a3b00 !important; font-weight:600; }
  </style>
</head>
<body>
<div id="map"></div>
<section class="panel">
  <h1>Downtown Food Support Map</h1>
  <p>Circle size/color = adjusted homeless demand. Click any point for details.</p>
  <div class="legend">
    <span class="dot" style="background:#d73027"></span><span>High demand</span>
    <span class="dot" style="background:#fc8d59"></span><span>Medium demand</span>
    <span class="dot" style="background:#91cf60"></span><span>Lower demand</span>
    <span class="dot" style="background:#2463eb"></span><span>Agency proxy</span>
    <span class="dot" style="background:#7c3aed"></span><span>Demo supplier</span>
    <span style="border-top:3px solid #0891b2;width:14px"></span><span>Actual road route</span>
  </div>
  <p class="warning">Demand is not real-time. Latest block observation: 2025-01-31.</p>
  <p>Agency and supplier points are demo/proxy data, not verified operating locations.</p>
</section>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const hotspots = __HOTSPOTS__;
const agencies = __AGENCIES__;
const suppliers = __SUPPLIERS__;
const routes = __ROUTES__;
const map = L.map('map').setView([32.7132, -117.1585], 15);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

function demandColor(value) {
  if (value >= 30) return '#d73027';
  if (value >= 15) return '#fc8d59';
  return '#91cf60';
}
const hotspotLayer = L.layerGroup().addTo(map);
hotspots.forEach(h => {
  L.circleMarker([h.lat, h.lon], {
    radius: Math.max(4, Math.min(18, 3 + Math.sqrt(h.demand) * 1.5)),
    color: demandColor(h.demand), fillColor: demandColor(h.demand),
    fillOpacity: .64, weight: 1
  }).bindPopup(`<b>#${h.rank} ${h.block_id}</b><br>${h.area}<br>Demand: ${h.demand}<br>Date: ${h.date}`)
    .addTo(hotspotLayer);
});

const agencyLayer = L.layerGroup().addTo(map);
agencies.forEach(a => L.circleMarker([a.lat, a.lon], {
  radius: 7, color:'#173c91', fillColor:'#2463eb', fillOpacity:.9, weight:2
}).bindPopup(`<b>${a.name}</b><br>${a.id}<br>Status: ${a.status}`).addTo(agencyLayer));

const supplierLayer = L.layerGroup().addTo(map);
suppliers.forEach(s => L.circleMarker([s.lat, s.lon], {
  radius: 7, color:'#4c1d95', fillColor:'#7c3aed', fillOpacity:.9, weight:2
}).bindPopup(`<b>${s.name}</b><br>${s.type}<br>${s.food_type}: ${s.quantity_lbs} lbs<br>Status: fictional demo`)
  .addTo(supplierLayer));

const routeLayer = L.layerGroup().addTo(map);
routes.forEach(r => L.geoJSON(r.geometry, {
  style: {color:'#0891b2', weight:4, opacity:.72}
}).bindPopup(`<b>${r.donation_id}</b><br>${r.agency_name} → ${r.supplier_name} → ${r.hotspot_block_id}<br>${r.miles} road miles · ${r.minutes} min`)
  .addTo(routeLayer));

L.control.layers({}, {
  'Demand hotspots': hotspotLayer,
  'Agency proxies': agencyLayer,
  'Demo suppliers': supplierLayer
  ,'Actual road routes': routeLayer
}, {collapsed:false, position:'bottomright'}).addTo(map);
</script>
</body>
</html>'''
    html = template.replace("__HOTSPOTS__", json.dumps(hotspots, separators=(",", ":")))
    html = html.replace("__AGENCIES__", json.dumps(agencies, separators=(",", ":")))
    html = html.replace("__SUPPLIERS__", json.dumps(suppliers, separators=(",", ":")))
    html = html.replace("__ROUTES__", json.dumps(routes, separators=(",", ":")))
    output = ROOT / "hotspot_map.html"
    output.write_text(html, encoding="utf-8")
    print(f"Wrote {output} with {len(hotspots)} hotspots, {len(agencies)} agencies, {len(suppliers)} suppliers, and {len(routes)} road routes")


if __name__ == "__main__":
    main()
