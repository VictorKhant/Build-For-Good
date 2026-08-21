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
const supplierMarkers = new Map();

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
    if (showStops) route.stops.forEach(stop => {
      L.circleMarker([stop.lat,stop.lon], {radius:stop.stop_type==="hotspot"?7:9,color,weight:2,fillColor:"#0d0d0d",fillOpacity:.9})
        .bindTooltip(`#${stop.stop_sequence} ${stop.stop_name}${stop.stop_type==="hotspot"?` · ${Number(stop.meals_delivered).toFixed(1)} meals`:""}`, {className:"hs-tip"})
        .addTo(networkLayer);
    });
  });
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
  $("runNetwork").disabled = true; $("apiStatus").textContent = "Running two-stage LP…";
  const response = await fetch(`${API}/optimize/simulation`, {method:"POST"});
  if (!response.ok) throw new Error(await response.text());
  const result = await fetch(`${API}/results/simulation/optimized?limit=5000`);
  allocations = (await result.json()).rows;
  const routeResult = await fetch(`${API}/routes/simulation`);
  vehicleRoutes = (await routeResult.json()).routes;
  drawVehicleRoutes(vehicleRoutes);
  const assigned = allocations.reduce((n,x)=>n+x.meals_allocated,0);
  $("apiStatus").textContent = `Optimal · ${assigned.toFixed(1)} meals · ${vehicleRoutes.length} truck trips`;
  $("resultEmpty").style.display = "none"; $("resultBody").hidden = false;
  const routeMiles = vehicleRoutes.reduce((n,x)=>n+(x.distance_miles||0),0);
  $("resultBody").innerHTML = `<div class="rb-eyebrow">Sequential truck routes optimized</div><div class="rb-source">Capacity-constrained SQL simulation</div><div class="outcomes"><div class="oc oc-people"><div class="v">${assigned.toFixed(1)}</div><div class="k">people fed</div></div><div class="oc"><div class="v">${vehicleRoutes.length}</div><div class="k">truck trips</div></div><div class="oc"><div class="v">${routeMiles.toFixed(1)}</div><div class="k">route miles</div></div></div><div class="rb-note">Each colored line is one continuous OSRM road route: Agency → Supplier → Hotspot 1 → Hotspot 2 → … . Click a supplier to display numbered stops.</div>`;
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
init();
