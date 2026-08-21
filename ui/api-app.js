"use strict";

const API = window.BELLYUP_API || "";
const $ = id => document.getElementById(id);
const map = L.map("map", { zoomControl: true, attributionControl: true }).setView([32.715, -117.155], 13);
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; CARTO', maxZoom: 19,
}).addTo(map);
const baseLayer = L.layerGroup().addTo(map);
const networkLayer = L.layerGroup().addTo(map);
let suppliers = [], agencies = [], hotspots = [], allocations = [], vehicleRoutes = [];
let activeMethod = "optimized", totalAssigned = 0;
const supplierMarkers = new Map();
const methodName = method => ({
  greedy: "Greedy baseline",
  optimized: "Global LP",
  route_optimized: "Sequential miles optimized",
})[method] || method;
function weightedPercentile(rows, field, percentile) {
  const sorted = [...rows].sort((a,b) => a[field] - b[field]);
  const target = sorted.reduce((n,x)=>n+x.meals_delivered,0) * percentile;
  let cumulative = 0;
  for (const row of sorted) { cumulative += row.meals_delivered; if (cumulative >= target) return row[field]; }
  return 0;
}

async function table(name) {
  const response = await fetch(`${API}/data/${name}?limit=5000`);
  if (!response.ok) throw new Error(`${name}: ${response.status}`);
  return (await response.json()).rows;
}

function marker(lat, lon, className, tooltip) {
  if (lat == null || lon == null) return null;
  const result = L.marker([lat, lon], {
    icon: L.divIcon({ className: "", html: `<div class="${className}"></div>`, iconSize: [14, 14], iconAnchor: [7, 7] }),
  }).addTo(baseLayer);
  result.bindTooltip(tooltip, { className: "hs-tip", direction: "top" });
  return result;
}

function renderBase() {
  hotspots.filter(h => h.historical_demand >= 1).forEach(h => {
    L.circleMarker([h.lat, h.lon], {
      radius: 3 + Math.sqrt(h.historical_demand) * 1.7, color: "#d55181", weight: 1,
      fillColor: "#d55181", fillOpacity: .22,
    }).bindTooltip(`<b>${h.location}</b><div class="tip-k">need ${Number(h.historical_demand).toFixed(1)} · rank #${h.rank}</div>`,
      { className: "hs-tip", direction: "top" }).addTo(baseLayer);
  });
  agencies.filter(a => a.usable_for_routing).forEach(a => marker(
    a.lat, a.lon, "mk-agency", `<b>${a.agency_name}</b><div class="tip-k">capacity ${a.capacity_meals_per_day} meals/day</div>`
  ));
  suppliers.forEach(s => {
    const m = marker(s.lat, s.lon, "mk-supplier", `<b>${s.supplier_name}</b><div class="tip-k">${s.available_food_lbs} lbs · ${Number(s.available_meals).toFixed(1)} meals</div>`);
    if (m) { m.on("click", () => focusSupplier(s.supplier_id)); supplierMarkers.set(s.supplier_id, m); }
  });
  map.fitBounds(L.latLngBounds(hotspots.map(h => [h.lat, h.lon])).pad(.15));
  $("feed").innerHTML = suppliers.map(s => `<div class="report-card" data-id="${s.supplier_id}">
    <div class="rc-top"><span>🍽</span><span class="rc-name">${s.supplier_name}</span><span class="rc-time">${s.reported_at.slice(11,16)}</span></div>
    <div class="rc-mid"><span class="rc-lbs">${s.available_food_lbs} lbs</span><span class="rc-meals">≈ ${Math.round(s.available_meals)} meals</span></div>
    <div class="rc-items">${s.facility_type} · ${s.food_type}</div></div>`).join("");
  document.querySelectorAll(".report-card").forEach(el => el.onclick = () => focusSupplier(el.dataset.id));
}

function drawNetwork(rows) {
  networkLayer.clearLayers();
  const supplierById = Object.fromEntries(suppliers.map(x => [x.supplier_id, x]));
  const agencyById = Object.fromEntries(agencies.map(x => [x.agency_id, x]));
  const hotspotById = Object.fromEntries(hotspots.map(x => [x.block_id, x]));
  const pickupSeen = new Set();
  rows.forEach(x => {
    const s = supplierById[x.business_id], a = agencyById[x.agency_id], h = hotspotById[x.hotspot_block_id];
    if (!s || !a || !h || a.lat == null) return;
    const pickupKey = `${a.agency_id}|${s.supplier_id}`;
    if (!pickupSeen.has(pickupKey)) {
      pickupSeen.add(pickupKey);
      L.polyline([[a.lat,a.lon],[s.lat,s.lon]], {color:"#3987e5",weight:2,opacity:.45,dashArray:"7 8"}).addTo(networkLayer);
    }
    L.polyline([[s.lat,s.lon],[h.lat,h.lon]], {
      color:"#1baf7a", weight:1 + Math.sqrt(x.meals_allocated) / 2.4, opacity:.42,
    }).bindTooltip(`${Number(x.meals_allocated).toFixed(1)} meals · ${Number(x.route_total_miles).toFixed(2)} road mi`,
      {className:"hs-tip"}).addTo(networkLayer);
  });
}

function drawVehicleRoutes(routes, showStops=false) {
  networkLayer.clearLayers();
  const colors = ["#1baf7a", "#3987e5", "#d55181", "#eb6834", "#9085e9", "#54c7ec"];
  routes.forEach((route, index) => {
    if (!route.geometry) return;
    const color = colors[index % colors.length];
    L.geoJSON(route.geometry, {style:{color, weight:3.2, opacity:.75}})
      .bindTooltip(`${route.route_id} · ${route.hotspot_count} sequential stops · ${Number(route.distance_miles).toFixed(1)} mi`, {className:"hs-tip"})
      .addTo(networkLayer);
    addRouteArrows(route.geometry, color);
    if (showStops) route.stops.forEach(stop => {
      L.circleMarker([stop.lat,stop.lon], {radius:stop.stop_type==="hotspot"?7:9,color,weight:2,fillColor:"#0d0d0d",fillOpacity:.9})
        .bindTooltip(`#${stop.stop_sequence} ${stop.stop_name}${stop.stop_type==="hotspot"?` · ${Number(stop.meals_delivered).toFixed(1)} meals`:""}`, {className:"hs-tip"})
        .addTo(networkLayer);
    });
  });
}

function addRouteArrows(geometry, color) {
  const coordinates = geometry && geometry.coordinates;
  if (!coordinates || coordinates.length < 2) return;
  const interval = Math.max(10, Math.floor(coordinates.length / 7));
  for (let i = interval; i < coordinates.length - 1; i += interval) {
    const from = coordinates[i-1], to = coordinates[i];
    const angle = Math.atan2(to[1]-from[1], to[0]-from[0]) * 180 / Math.PI;
    L.marker([to[1],to[0]], {interactive:false, icon:L.divIcon({className:"", html:`<span class="route-arrow" style="color:${color};transform:rotate(${-angle}deg)">➤</span>`, iconSize:[16,16], iconAnchor:[8,8]})}).addTo(networkLayer);
  }
}

function focusRoute(routeId) {
  const route = vehicleRoutes.find(x => x.route_id === routeId);
  if (!route) return;
  drawVehicleRoutes([route], true);
  const bounds = L.latLngBounds(route.stops.map(x => [x.lat, x.lon]));
  map.flyToBounds(bounds.pad(.12), {duration:.7});
  document.querySelectorAll(".route-card").forEach(x => x.classList.toggle("active", x.dataset.routeId === routeId));
  $("restoreNetwork").hidden = false;
}

function renderRoutePanel(assigned) {
  const routeMiles = vehicleRoutes.reduce((n,x)=>n+(x.distance_miles||0),0);
  const uniqueHotspots = new Set(allocations.map(x=>x.hotspot_block_id)).size;
  const uniqueSuppliers = new Set(allocations.map(x=>x.business_id)).size;
  const deliveryStops = vehicleRoutes.flatMap(x=>x.stops).filter(x=>x.stop_type === "hotspot");
  const mealDistance = deliveryStops.reduce((n,x)=>n+x.meals_delivered*x.meal_distance_from_pickup_miles,0) / assigned;
  const mealTransit = deliveryStops.reduce((n,x)=>n+x.meals_delivered*x.meal_transit_from_pickup_minutes,0) / assigned;
  const p95Transit = weightedPercentile(deliveryStops, "meal_transit_from_pickup_minutes", .95);
  const maxTransit = Math.max(...deliveryStops.map(x=>x.meal_transit_from_pickup_minutes));
  const mealsPerMile = assigned / routeMiles;
  const cards = vehicleRoutes.map(route => {
    const itinerary = route.stops.map(stop => `<li><span class="stop-seq">${stop.stop_sequence}</span><span><b>${stop.stop_type}</b> · ${stop.stop_name}${stop.stop_type === "hotspot" ? ` <em>${Number(stop.meals_delivered).toFixed(1)} meals · ${Number(stop.meal_distance_from_pickup_miles).toFixed(1)} mi · ${Math.round(stop.meal_transit_from_pickup_minutes)} min</em>` : ""}</span></li>`).join("");
    return `<article class="route-card" data-route-id="${route.route_id}">
      <div class="route-card-head"><span class="route-id">Truck ${String(route.truck_sequence).padStart(3,"0")}</span><span>${Number(route.distance_miles).toFixed(1)} mi · ${Math.round(route.duration_minutes)} min</span></div>
      <div class="route-path"><b>${route.agency_name}</b><span>→ pickup</span><b>${route.supplier_name}</b><span>→ ${route.hotspot_count} hotspots</span></div>
      <div class="route-metrics"><span><b>${Number(route.meals_loaded).toFixed(1)}</b> meals</span><span><b>${Number(route.meals_per_truck_mile).toFixed(2)}</b> meals/mi</span><span><b>${route.hotspot_count}</b> hotspots</span></div>
      <details><summary>Show stop-by-stop itinerary</summary><ol class="stop-list">${itinerary}</ol></details>
    </article>`;
  }).join("");
  const methodLabel = methodName(activeMethod);
  $("resultBody").innerHTML = `<div class="rb-eyebrow">${methodLabel} · sequential routes</div><div class="rb-source">Capacity-constrained allocation + OSRM road paths</div>
    <div class="network-kpis"><div><b>${routeMiles.toFixed(1)}</b><span>truck miles</span></div><div><b>${mealsPerMile.toFixed(2)}</b><span>meals per mile</span></div><div><b>${mealDistance.toFixed(1)} mi</b><span>avg meal distance</span></div><div><b>${mealTransit.toFixed(0)} min</b><span>avg meal transit</span></div><div><b>${p95Transit.toFixed(0)} min</b><span>P95 meal transit</span></div><div><b>${maxTransit.toFixed(0)} min</b><span>max meal transit</span></div></div>
    <div class="rb-note">${assigned.toFixed(1)} meals · ${uniqueHotspots} hotspots · ${allocations.length} allocation arcs · ${vehicleRoutes.length} truck trips · ${uniqueSuppliers} suppliers. Meal distance/time begins at pickup; Agency → Supplier travel counts only toward truck miles.</div>
    <div class="rb-h">Truck routes</div><div class="route-list">${cards}</div>`;
  document.querySelectorAll(".route-card").forEach(card => card.addEventListener("click", event => {
    if (event.target.closest("details")) return;
    focusRoute(card.dataset.routeId);
  }));
}

function focusSupplier(id) {
  if (!allocations.length) return;
  document.querySelectorAll(".report-card").forEach(x => x.classList.toggle("active", x.dataset.id === id));
  const rows = allocations.filter(x => x.business_id === id);
  const trips = vehicleRoutes.filter(x => x.supplier_id === id);
  drawVehicleRoutes(trips, true);
  const supplier = suppliers.find(x => x.supplier_id === id);
  const agencyById = Object.fromEntries(agencies.map(x => [x.agency_id, x]));
  const grouped = {};
  rows.forEach(x => grouped[x.agency_id] = (grouped[x.agency_id] || 0) + x.meals_allocated);
  $("resultEmpty").style.display = "none"; $("resultBody").hidden = false;
  $("resultBody").innerHTML = `<div class="rb-eyebrow">Optimized supplier flow</div><div class="rb-source">${supplier.supplier_name} · ${supplier.available_food_lbs} lbs</div>
    <div class="outcomes"><div class="oc oc-people"><div class="v">${rows.reduce((n,x)=>n+x.meals_allocated,0).toFixed(1)}</div><div class="k">meals assigned</div></div><div class="oc"><div class="v">${new Set(rows.map(x=>x.hotspot_block_id)).size}</div><div class="k">hotspots</div></div><div class="oc"><div class="v">${trips.length}</div><div class="k">truck trips</div></div></div>
    <div class="rb-note">Numbered stops show the truck's sequential visit order. It departs the agency, picks up once, then visits each hotspot one by one.</div>
    <div class="rb-h">Agency allocation</div>${Object.entries(grouped).map(([id,v])=>`<div class="alt-row"><span class="alt-pair"><b>${agencyById[id].agency_name}</b></span><span>${v.toFixed(1)} meals</span></div>`).join("")}`;
}

async function runNetwork() {
  activeMethod = $("methodSelect").value;
  const methodLabel = methodName(activeMethod);
  $("runNetwork").disabled = true; $("apiStatus").textContent = `Running ${methodLabel} simulation…`;
  const response = await fetch(`${API}/optimize/simulation`, {method:"POST"});
  if (!response.ok) throw new Error(await response.text());
  const result = await fetch(`${API}/results/simulation/${activeMethod}?limit=5000`);
  allocations = (await result.json()).rows;
  const routeResult = await fetch(`${API}/routes/simulation/${activeMethod}`);
  vehicleRoutes = (await routeResult.json()).routes;
  drawVehicleRoutes(vehicleRoutes);
  const assigned = allocations.reduce((n,x)=>n+x.meals_allocated,0); totalAssigned = assigned;
  $("apiStatus").textContent = `${methodLabel} · ${assigned.toFixed(1)} meals · ${vehicleRoutes.length} truck trips`;
  $("resultEmpty").style.display = "none"; $("resultBody").hidden = false;
  renderRoutePanel(assigned);
  $("restoreNetwork").hidden = true;
  $("runNetwork").disabled = false;
}

async function init() {
  try {
    [suppliers, agencies, hotspots] = await Promise.all([table("supplier_supply"), table("agency_capacity"), table("hotspots")]);
    renderBase();
    const totalLbs = suppliers.reduce((n,x)=>n+x.available_food_lbs,0);
    $("topstats").innerHTML = [[`${totalLbs.toLocaleString()} lbs`,"simulation supply"],[suppliers.length,"active reports"],[agencies.filter(x=>x.usable_for_routing).length,"routable agencies"],[hotspots.filter(x=>x.historical_demand>=1).length,"blocks in need"]].map(([v,k])=>`<div class="stat-chip"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
    $("topdate").textContent = "SQL + FastAPI"; $("apiStatus").textContent = "API connected · ready";
  } catch (error) { $("apiStatus").textContent = `API error: ${error.message}`; }
}
$("runNetwork").onclick = () => runNetwork().catch(e => { $("apiStatus").textContent = `Error: ${e.message}`; $("runNetwork").disabled=false; });
$("restoreNetwork").onclick = () => { drawVehicleRoutes(vehicleRoutes); renderRoutePanel(totalAssigned); $("restoreNetwork").hidden=true; };
init();
