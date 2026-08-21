/* BellyUp — voluntary surplus reports in, collector→hotspot dispatch out.

   Data and scoring both come from the API (they used to be baked into
   data.js). That is what lets a restaurant register mid-demo and lets the
   engine apply expiry, pickup windows and a server-side ledger — none of
   which a static file can do. The map, animation, ledger and result panel
   are unchanged. */

"use strict";

let HOTSPOTS = [], SUPPLIERS = [], AGENCIES = [], PANTRIES = [], C = {};
let HISTORY = [], tonight = [], OPTED_OUT = 0;
let CANDIDATES = [], REPORTING = [], COLLECTORS = [];

const api = (path, opts) => fetch(path, opts).then(async r => {
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
});

async function loadBoard() {
  const b = await api("/api/board");
  HOTSPOTS = b.hotspots; SUPPLIERS = b.suppliers;
  AGENCIES = b.agencies; PANTRIES = b.pantries; C = b.constants;
  HISTORY = b.history || []; tonight = b.tonight || [];
  OPTED_OUT = b.optedOut || 0;
  CANDIDATES = HOTSPOTS.filter(h => h.need >= C.MIN_CANDIDATE_NEED);
  REPORTING = SUPPLIERS.filter(s => s.report);
  COLLECTORS = [
    ...AGENCIES.map(a => ({ ...a, kind: "agency", capacityLbs: C.AGENCY_CAPACITY_LBS })),
    ...PANTRIES.filter(p => p.dispatchable).map(p => ({ ...p, kind: "pantry", capacityLbs: C.PANTRY_CAPACITY_LBS })),
  ];
  return b;
}

/* ---------------------------------------------------------------- helpers */
const $ = id => document.getElementById(id);
const fmt$ = v => (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2);
const fmtInt = v => Math.round(v).toLocaleString();
const agShort = a => a.name
  .replace("Jacobs & Cushman San Diego Food Bank", "SD Food Bank")
  .replace("Catholic Charities Diocese of San Diego", "Catholic Charities")
  .replace("Stepping Higher Incorporated", "Stepping Higher");

function haversineMi(a, b) {
  const R = 3958.76, rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
  const s = Math.sin(dLat / 2) ** 2 +
    Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}
const roadMi = (a, b) => haversineMi(a, b) * C.ROAD_FACTOR;

/* ------------------------------------------------------------------ theme */
/* Light is the default. The choice persists per browser. Map geometry reads
   the --c-* tokens so both themes drive colors from styles.css. */
const TILE_URLS = {
  light: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  dark: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
};
let theme = "light";
try { theme = localStorage.getItem("bellyup.theme") || "light"; } catch (e) { /* private mode */ }
applyThemeAttr();

function applyThemeAttr() {
  if (theme === "dark") document.documentElement.setAttribute("data-theme", "dark");
  else document.documentElement.removeAttribute("data-theme");
}
function themeColor(token) {
  return getComputedStyle(document.documentElement).getPropertyValue(token).trim();
}

/* ----------------------------------------------------------------- ledger */
/* HISTORY is the past week, read-only. `tonight` is what has been CONFIRMED
   this evening; both come from the server, so a reload does not lose the
   evening and two people watching see the same board. */
const dispatchedSupplierIds = () => new Set(tonight.map(r => r.supplierId));
const servedMealsTonight = hid =>
  tonight.filter(r => r.hotspotId === hid).reduce((t, r) => t + r.servedMeals, 0);
const dropsTonight = hid => tonight.filter(r => r.hotspotId === hid).length;
function hotspotClosed(h) {
  if (h.need < C.MIN_CANDIDATE_NEED) return false;   // never in the pool
  return dropsTonight(h.id) >= C.MAX_DROPS_PER_NIGHT ||
    h.need - servedMealsTonight(h.id) < 1;
}

/* ------------------------------------------------------- matching engine */
/* Lives on the server now: bellyup/dispatch.py. Same reward−cost shape, with
   freshness decay, expiry and pickup-window constraints layered on, plus the
   serving limits (need met, or MAX_DROPS_PER_NIGHT deliveries to one block).

     net    = reward − cost
     reward = served × MEAL_VALUE × accessBoost × freshness
              + surplus × MEAL_VALUE × 0.5
     cost   = (drive + handling) × WAGE_PER_HR + road miles × COST_PER_MILE  */
const dispatchFor = s => api(`/api/board/dispatch/${s.id}`, { method: "POST" });

/* ------------------------------------------------------------------- map */
const map = L.map("map", { zoomControl: true, attributionControl: true });
const tiles = L.tileLayer(TILE_URLS[theme], {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
  maxZoom: 19,
}).addTo(map);

const fxLayer = L.layerGroup().addTo(map);   // scan lines, routes, radar
const baseLayer = L.layerGroup().addTo(map); // hotspots, collectors, suppliers

const TRUCK_SVG = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 5h11v9H3zM14 8h4l3 3v3h-7zM6 18a2 2 0 1 0 0-4 2 2 0 0 0 0 4zm11 0a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"/></svg>';
const typeIcon = { grocery: "🛒", hotel: "🏨", venue: "🏟", health: "🏥", restaurant: "🍽" };

/* hotspot circles — magenta, sized by need; aqua once served/at limit */
const hotspotMarkers = {}, agencyMarkers = {}, pantryMarkers = {}, supplierMarkers = {};
function hotspotStyle(h) {
  const closed = hotspotClosed(h);
  const col = closed ? themeColor("--c-route") : themeColor("--c-hotspot");
  return {
    radius: 3 + Math.sqrt(h.need) * 2.1,
    color: col,
    weight: 1.4,
    opacity: closed ? 0.9 : 0.8,
    fillColor: col,
    fillOpacity: closed ? 0.3 : 0.16 + Math.min(h.need / 40, 0.34),
  };
}
function hotspotTip(h) {
  const servedNow = servedMealsTonight(h.id);
  const status = hotspotClosed(h)
    ? `<br><b style="color:${themeColor("--c-route")}">served tonight — off the candidate list</b>`
    : servedNow > 0 ? `<br>${fmtInt(servedNow)} meals delivered tonight; ${(h.need - servedNow).toFixed(1)} need remaining` : "";
  return `<b>${h.location}</b> &middot; ${h.area}` +
    `<div class="tip-k">need ${h.need.toFixed(1)} person-equivalents &middot; rank #${h.rank}` +
    `<br>food access ${h.accessDays.toFixed(h.accessDays % 1 ? 1 : 0)} days/week${status}</div>`;
}
function refreshHotspots() {
  for (const h of HOTSPOTS) {
    if (!hotspotMarkers[h.id]) continue;
    hotspotMarkers[h.id].setStyle(hotspotStyle(h));
    hotspotMarkers[h.id].setTooltipContent(hotspotTip(h));
  }
}

/* Everything below used to run at load. It now runs from buildLayers() once
   /api/board has answered, and again whenever the roster changes. */
function buildLayers() {
  baseLayer.clearLayers();
  for (const k of [hotspotMarkers, agencyMarkers, pantryMarkers, supplierMarkers])
    for (const id in k) delete k[id];

  map.fitBounds(L.latLngBounds(HOTSPOTS.map(h => [h.lat, h.lon])).pad(0.12));

for (const h of HOTSPOTS) {
  const m = L.circleMarker([h.lat, h.lon], { ...hotspotStyle(h), className: "hs-path" }).addTo(baseLayer);
  m.bindTooltip(hotspotTip(h), { className: "hs-tip", direction: "top", opacity: 1 });
  hotspotMarkers[h.id] = m;
}

/* agency HQ markers */
const TRUCK_SVG = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 5h11v9H3zM14 8h4l3 3v3h-7zM6 18a2 2 0 1 0 0-4 2 2 0 0 0 0 4zm11 0a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"/></svg>';
/* A truck only for agencies that can actually be dispatched. The rest are
   fixed sites that receive donations -- drawing them with a truck would
   promise a fleet they do not have. */
for (const a of AGENCIES) {
  const collects = a.mobileCapable !== false;
  const html = collects
    ? `<div class="mk-agency" id="col-${a.id}">${TRUCK_SVG}</div>`
    : `<div class="mk-dropoff" id="col-${a.id}"></div>`;
  L.marker([a.lat, a.lon], {
    icon: L.divIcon({ className: "", html,
      iconSize: collects ? [26, 26] : [14, 14],
      iconAnchor: collects ? [13, 13] : [7, 7] }),
    zIndexOffset: collects ? 500 : 300,
  }).addTo(baseLayer).bindTooltip(
    `<b>${a.name}</b><div class="tip-k">${a.program}` +
    `<br>${a.acceptsPrepared ? "accepts prepared food" : "packaged/produce"}` +
    (collects ? "" : "<br>fixed drop-off site — no collection vehicle") + `</div>`,
    { className: "hs-tip", direction: "top", opacity: 1 }
  );
}

/* mobile pantry sites — violet diamonds; solid = unit available tonight */
for (const p of PANTRIES) {
  const cls = p.dispatchable ? "mk-pantry" : "mk-pantry idle";
  const status = p.dispatchable
    ? '<br><b class="tip-pantry">mobile unit available tonight</b>'
    : `<br>${p.whyNot}`;
  L.marker([p.lat, p.lon], {
    icon: L.divIcon({ className: "", html: `<div class="${cls}" id="col-${p.id}"></div>`, iconSize: [16, 16], iconAnchor: [8, 8] }),
    zIndexOffset: 400,
  }).addTo(baseLayer).bindTooltip(
    `<b>${p.name}</b><div class="tip-k">${p.operator} &middot; ${p.program}` +
    `<br>runs ${p.schedule}${status}</div>`,
    { className: "hs-tip", direction: "top", opacity: 1 }
  );
}

/* supplier markers — the "input markers", always on the map */
for (const s of SUPPLIERS) {
  const cls = s.report ? "mk-supplier" : "mk-supplier quiet";
  const m = L.marker([s.lat, s.lon], {
    icon: L.divIcon({ className: "", html: `<div class="${cls}" id="sp-${s.id}"></div>`, iconSize: [14, 14], iconAnchor: [7, 7] }),
    zIndexOffset: 700,
  }).addTo(baseLayer);
  const rep = s.report
    ? `<br><b class="tip-supplier">${s.report.lbs} lbs</b> ${s.surplus} &middot; reported ${s.report.time}`
    : "<br>no surplus reported tonight";
  m.bindTooltip(
    `<b>${s.name}</b><div class="tip-k">${s.type}${rep}</div>`,
    { className: "hs-tip", direction: "top", opacity: 1 }
  );
  m.on("click", () => {
    if (!s.report) return openForm(s);
    if (dispatchedSupplierIds().has(s.id)) openLedger(); else selectReport(s);
  });
}

}   /* ---- end buildLayers() ---- */

/* ------------------------------------------------------------------ feed */
const rcWindow = r => {
  const bits = [];
  if (r.pickupTo) bits.push(`pickup <b>${r.pickupFrom}\u2013${r.pickupTo}</b>`);
  if (r.expiresAt) bits.push(`good until <b>${r.expiresAt}</b>`);
  return bits.length ? `<div class="rc-window">${bits.join(" &middot; ")}</div>` : "";
};

function renderFeed() {
  const done = dispatchedSupplierIds();
  $("feed").innerHTML = REPORTING.map(s => {
    const rec = tonight.find(r => r.supplierId === s.id);
    return `
    <div class="report-card ${done.has(s.id) ? "done" : ""}" id="card-${s.id}">
      <div class="rc-top">
        <span>${typeIcon[s.type] || "🍽"}</span>
        <span class="rc-name">${s.registered ? '<span class="rc-saved">SAVED</span>' : ""}${
          s.report.updated ? '<span class="rc-upd">UPDATED</span>' : ""}${s.name}</span>
        <span class="rc-time">${s.report.time}</span>
        <button class="rc-edit" data-edit="${s.id}" title="Surplus differs every night — update tonight's numbers">Update</button>
      </div>
      <div class="rc-mid">
        <span class="rc-lbs">${s.report.lbs} lbs</span>
        <span class="rc-meals">&asymp; ${fmtInt(s.report.lbs / C.LBS_PER_MEAL)} meals</span>
        <span class="rc-chip ${s.surplus === "prepared" ? "prepared" : ""}">${s.surplus}</span>
      </div>
      <div class="rc-items">${s.report.items}</div>
      ${rcWindow(s.report)}
      ${rec ? `<div class="rc-done">✓ dispatched &middot; ${agShort({ name: rec.collector })} &rarr; ${rec.hotspot}</div>` : ""}
    </div>`;
  }).join("");
  /* Partners with nothing tonight are not a dead list — tomorrow they may
     have something, and reporting it is the whole point of the platform. */
  const quietOnes = SUPPLIERS.filter(s => !s.report);
  if (quietOnes.length) {
    $("feed").insertAdjacentHTML("beforeend", `
      <div class="quiet-list">
        <div class="quiet-head">${quietOnes.length} partners &mdash; no surplus tonight</div>
        ${quietOnes.map(s => `
          <button class="quiet-row" data-edit="${s.id}">
            <span>${typeIcon[s.type] || "🍽"}</span>
            <span class="qr-name">${s.name}</span>
            <span class="qr-act">report&nbsp;+</span>
          </button>`).join("")}
      </div>`);
  }

  for (const s of REPORTING) {
    $("card-" + s.id).addEventListener("click", ev => {
      if (ev.target.dataset.edit) return;          /* Update handles itself */
      if (dispatchedSupplierIds().has(s.id)) openLedger(); else selectReport(s);
    });
  }
  $("feed").querySelectorAll("[data-edit]").forEach(btn => {
    btn.addEventListener("click", ev => {
      ev.stopPropagation();
      openForm(SUPPLIERS.find(x => x.id === btn.dataset.edit));
    });
  });
  for (const s of REPORTING) {
    const el = $("sp-" + s.id);
    if (el) el.classList.toggle("dispatched", done.has(s.id));
  }
}

/* --------------------------------------------------------------- topbar */
function renderStats() {
  const totLbs = REPORTING.reduce((t, s) => t + s.report.lbs, 0);
  const fed = tonight.reduce((t, r) => t + r.servedMeals, 0);
  $("topstats").innerHTML = [
    [fmtInt(totLbs) + " lbs", "reported tonight"],
    ["~" + fmtInt(totLbs / C.LBS_PER_MEAL), "potential meals"],
    [REPORTING.length - tonight.length, "pending reports"],
    [fmtInt(fed), "people fed tonight"],
  ].map(([v, k]) => `<div class="stat-chip"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
  $("ledgerCount").textContent = tonight.length;
}
$("topdate").textContent = new Date().toLocaleDateString("en-US",
  { weekday: "short", month: "short", day: "numeric", year: "numeric" });

/* ------------------------------------------------- selection + animation */
let animToken = 0;
let selectedId = null;
let currentMatch = null;   // { supplier, result } for the confirm button

function clearFx() {
  fxLayer.clearLayers();
  $("calcOverlay").classList.remove("show");
  document.querySelectorAll(".mk-agency.winner, .mk-pantry.winner").forEach(el => el.classList.remove("winner"));
  document.querySelectorAll(".mk-supplier.selected").forEach(el => el.classList.remove("selected"));
  for (const id in hotspotMarkers) {
    const el = hotspotMarkers[id].getElement();
    if (el) el.classList.remove("hs-winner", "hs-scanned");
  }
}

async function selectReport(s, opts = {}) {
  if (selectedId === s.id && !opts.force) return;
  selectedId = s.id;
  const token = ++animToken;
  clearFx();

  document.querySelectorAll(".report-card.active").forEach(el => el.classList.remove("active"));
  const card = $("card-" + s.id);
  if (card) {
    card.classList.add("active");
    card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  const pin = $("sp-" + s.id);
  if (pin) pin.classList.add("selected");

  /* Scoring is a round trip now. On a local server the answer lands long
     before the scan animation would finish; the token guard covers the rest. */
  let result;
  try {
    result = await dispatchFor(s);
  } catch (err) {
    $("resultEmpty").style.display = "";
    $("resultBody").hidden = true;
    $("calcOverlay").classList.remove("show");
    return;
  }
  if (token !== animToken) return;
  currentMatch = { supplier: s, result };
  runTriangulation(s, result, token);
}

function runTriangulation(s, result, token) {
  /* No viable pair is a real answer, not a failure. Refusing a run that costs
     more than the food is worth is the point -- so say so plainly rather than
     animating toward a recommendation that does not exist. */
  if (!result.pairs.length) return renderRefusal(s, result);

  const best = result.pairs[0];
  const live = () => token === animToken;

  /* hide any previous result while we "compute" */
  $("resultBody").hidden = true;
  $("resultEmpty").style.display = "none";

  const scanBounds = L.latLngBounds(HOTSPOTS.map(h => [h.lat, h.lon])).extend([s.lat, s.lon]);
  map.flyToBounds(scanBounds.pad(0.1), { duration: 0.55 });

  /* radar pulse at the supplier */
  fxLayer.addLayer(L.marker([s.lat, s.lon], {
    icon: L.divIcon({ className: "", html: '<div class="radar"><span></span><span></span><span></span></div>', iconSize: [12, 12], iconAnchor: [6, 6] }),
    interactive: false, zIndexOffset: 900,
  }));

  /* calc overlay */
  const constraint = result.prepared
    ? `constraint: prepared food &rarr; ${result.eligibleCount} of ${result.collectorCount} collectors eligible`
    : `${result.eligibleCount} collectors &times; ${result.candidateCount} open blocks`;
  $("calcTitle").textContent = "TRIANGULATING";
  $("calcLine").innerHTML = `${s.name} &middot; ${constraint}`;
  $("calcOverlay").classList.add("show");

  const SCAN_MS = 1350, SHORTLIST_MS = 850;

  /* pair counter counts up through the scan */
  const t0 = performance.now();
  (function tick(now) {
    if (!live()) return;
    const p = Math.min((now - t0) / SCAN_MS, 1);
    $("calcCount").textContent = fmtInt(result.evaluated * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(tick);
  })(t0);

  /* scan lines: quick flicks from the supplier to sampled candidate blocks */
  const openIds = new Set(result.pairs.map(p => p.hotspot.id));
  const sample = CANDIDATES.filter(h => openIds.has(h.id)).sort(() => Math.random() - 0.5).slice(0, 26);
  sample.forEach((h, i) => {
    setTimeout(() => {
      if (!live()) return;
      const line = L.polyline([[s.lat, s.lon], [h.lat, h.lon]], {
        color: themeColor("--c-supplier"), weight: 1.3, opacity: 0.9,
        className: "scan-line", interactive: false,
      });
      fxLayer.addLayer(line);
      setTimeout(() => fxLayer.removeLayer(line), 520);
      const el = hotspotMarkers[h.id].getElement();
      if (el) { el.classList.remove("hs-scanned"); void el.getBBox; el.classList.add("hs-scanned"); }
    }, 90 + i * (SCAN_MS - 200) / sample.length);
  });

  /* shortlist: top-3 hotspots pulse while collectors are weighed */
  setTimeout(() => {
    if (!live()) return;
    $("calcTitle").textContent = "SHORTLISTING";
    $("calcLine").innerHTML = "weighing need, access gap, serving limits and deployment cost";
    const seen = new Set(), top = [];
    for (const p of result.pairs) {
      if (!seen.has(p.hotspot.id)) { seen.add(p.hotspot.id); top.push(p.hotspot); }
      if (top.length === 3) break;
    }
    for (const h of top) {
      fxLayer.addLayer(L.polyline([[s.lat, s.lon], [h.lat, h.lon]], {
        color: themeColor("--c-hotspot"), weight: 2, opacity: 0.85,
        className: "short-line", interactive: false,
      }));
    }
  }, SCAN_MS);

  /* lock: draw the winning route, light up the pair, show the result */
  setTimeout(() => {
    if (!live()) return;
    $("calcTitle").textContent = "DISPATCH LOCKED";
    $("calcLine").innerHTML =
      `${agShort(best.collector)} &rarr; pickup &rarr; ${best.hotspot.location}`;
    $("calcCount").textContent = fmtInt(result.evaluated);

    /* keep only radar + winning graphics */
    fxLayer.eachLayer(l => { if (l.options.className === "short-line") fxLayer.removeLayer(l); });

    const a = best.collector, h = best.hotspot;
    const role = best.dropoff ? "--c-agency"
      : (a.kind === "pantry" ? "--c-pantry" : "--c-agency");

    /* A drop-off is one leg: the food goes to the site and people come to it.
       Drawing a second leg would imply a delivery run that never happens. */
    fxLayer.addLayer(L.polyline([[s.lat, s.lon], [a.lat, a.lon]], {
      color: themeColor(role), bellyRole: role,
      weight: best.dropoff ? 3.5 : 2.5, opacity: 0.9,
      className: best.dropoff ? "route-leg2" : "route-leg1", interactive: false,
    }));
    if (h) {
      fxLayer.addLayer(L.polyline([[s.lat, s.lon], [h.lat, h.lon]], {
        color: themeColor("--c-route"), bellyRole: "--c-route",
        weight: 3.5, opacity: 0.95, className: "route-leg2", interactive: false,
      }));
    }
    const colEl = $("col-" + a.id);
    if (colEl) colEl.classList.add("winner");
    if (h && hotspotMarkers[h.id]) {
      const hEl = hotspotMarkers[h.id].getElement();
      if (hEl) hEl.classList.add("hs-winner");
    }

    const bounds = [[a.lat, a.lon], [s.lat, s.lon]];
    if (h) bounds.push([h.lat, h.lon]);
    map.flyToBounds(L.latLngBounds(bounds).pad(0.18), { duration: 0.8 });

    renderResult(s, result);
    setTimeout(() => { if (live()) $("calcOverlay").classList.remove("show"); }, 2600);
  }, SCAN_MS + SHORTLIST_MS);
}

function renderRefusal(s, result) {
  clearFx();
  $("calcOverlay").classList.remove("show");
  $("resultEmpty").style.display = "none";

  const top = result.rejections.slice(0, 5);
  const meals = fmtInt(result.meals);
  $("resultBody").innerHTML = `
    <div class="rb-eyebrow rb-refused">No viable dispatch</div>
    <div class="rb-source">from <b>${s.name}</b> &middot; ${fmtInt(s.report.lbs)} lbs
      ${s.surplus} &middot; ${meals} meals &middot; reported ${result.reportedAt}
      <br>pickup window ${result.window.from}&ndash;${result.window.to}
      &middot; good until ${result.expiresAt}</div>

    <div class="refused-box">
      <div class="rf-title">Nothing here is worth the run.</div>
      <div class="rf-sub">Every collector and every open block was checked, and
        none of them covers its own fuel and staff time for ${meals} meals.
        Holding this for a larger pickup moves more food per mile than driving
        for it now.</div>
    </div>

    <div class="rb-h">Why each option failed
      (${fmtInt(result.evaluated + result.rejections.reduce((t, r) => t + r.count, 0))} evaluated)</div>
    ${top.map(r => `<div class="alt-row">
      <span class="alt-rank">&times;${r.count}</span>
      <span class="alt-pair">${r.example}</span></div>`).join("")}`;
  $("resultBody").hidden = false;
}

/* ---------------------------------------------------------- result panel */
function renderResult(s, result) {
  const b = result.pairs[0];
  const h = b.hotspot, a = b.collector;
  const isPantry = a.kind === "pantry";
  const boostPct = Math.round((b.boost - 1) * 100);
  const fmv = result.fmv;
  const freshPct = Math.round(b.freshness * 100);
  const servedBefore = h ? servedMealsTonight(h.id) : 0;

  /* alternates: next best pairs with a distinct collector or hotspot */
  const alts = [];
  for (const p of result.pairs.slice(1)) {
    if (alts.length === 3) break;
    /* compare by id: these came off the wire, not from the same objects */
    const sameTarget = p.hotspot && h ? p.hotspot.id === h.id : (!p.hotspot && !h);
    if (p.collector.id !== a.id || !sameTarget) alts.push(p);
  }

  $("resultBody").innerHTML = `
    <div class="rb-eyebrow">Dispatch recommendation</div>
    <div class="rb-source">from <b>${s.name}</b> &middot; ${s.report.lbs} lbs ${s.surplus}
      &middot; ${fmtInt(result.meals)} meals &middot; reported ${result.reportedAt}
      <br>pickup window ${result.window.from}&ndash;${result.window.to}
      &middot; good until ${result.expiresAt}
      &middot; collected ${b.pickupAt}, served ${b.arrivesAt}</div>

    <div class="pair">
      <div class="pair-node ${isPantry ? "pn-pantryunit" : "pn-agency"}">
        <div class="pn-badge">${TRUCK_SVG.replace("<svg", '<svg width="17" height="17"')}</div>
        <div>
          <div class="pn-role">${isPantry ? "Mobile pantry unit" : "Collecting agency"}</div>
          <div class="pn-name">${a.name}</div>
          <div class="pn-sub">${isPantry ? `${a.operator} &middot; ${a.program}` : a.program}</div>
        </div>
      </div>
      <div class="pair-arrow"></div>
      <div class="pair-node pn-pickup">
        <div class="pn-badge">${typeIcon[s.type] || "🍽"}</div>
        <div>
          <div class="pn-role">Pickup</div>
          <div class="pn-name">${s.name}</div>
          <div class="pn-sub">${s.report.items}</div>
        </div>
      </div>
      <div class="pair-arrow"></div>
      ${h ? `<div class="pair-node pn-hotspot">
        <div class="pn-badge">📍</div>
        <div>
          <div class="pn-role">Distribution hotspot</div>
          <div class="pn-name">${h.location} &middot; ${h.area}</div>
          <div class="pn-sub">need ${h.need.toFixed(1)} &middot; rank #${h.rank} of 382
            &middot; food access ${h.accessDays.toFixed(h.accessDays % 1 ? 1 : 0)} d/wk${
              servedBefore > 0 ? ` &middot; ${fmtInt(servedBefore)} meals already tonight` : ""}</div>
        </div>
      </div>` : `<div class="pair-node pn-dropoff">
        <div class="pn-badge">🏛</div>
        <div>
          <div class="pn-role">Fixed drop-off</div>
          <div class="pn-name">${a.name}</div>
          <div class="pn-sub">people collect here — no distribution run tonight</div>
        </div>
      </div>`}
    </div>

    <div class="outcomes">
      <div class="oc oc-people"><div class="v" data-count="${b.served}">0</div><div class="k">people fed</div></div>
      <div class="oc"><div class="v" data-count="${b.miles}" data-dec="1">0</div><div class="k">route miles</div></div>
      <div class="oc oc-net ${b.net < 0 ? "neg" : ""}"><div class="v" data-count="${b.net}" data-money="1">0</div><div class="k">net benefit</div></div>
    </div>

    <div class="score-viz">
      <div class="sv-row"><span class="sv-label">Freshness</span>
        <span class="sv-track"><span class="sv-fill sv-fresh" data-pct="${freshPct}"></span></span>
        <span class="sv-val">${freshPct}%</span></div>
      <div class="sv-row sv-reward"><span class="sv-label">Reward</span>
        <span class="sv-track"><span class="sv-fill" data-w="${b.reward}"></span></span>
        <span class="sv-val">${fmt$(b.reward)}</span></div>
      <div class="sv-row sv-cost"><span class="sv-label">Op. cost</span>
        <span class="sv-track"><span class="sv-fill" data-w="${b.cost}"></span></span>
        <span class="sv-val">&minus;${fmt$(b.cost).slice(1)}</span></div>
      <div class="sv-row sv-net"><span class="sv-label">Net</span>
        <span class="sv-track"><span class="sv-fill" data-w="${Math.max(b.net, 0)}"></span></span>
        <span class="sv-val">${fmt$(b.net)}</span></div>
    </div>

    <button class="btn-confirm" id="confirmBtn">✓ Confirm dispatch &amp; log receipt</button>

    ${b.dropoff ? `<div class="rb-note">🏛 <b>Fixed drop-off — one leg, no distribution run</b>
      — ${a.name} has no collection vehicle, so this is scored on the
      ${b.miles.toFixed(1)} mi between the donor and the site alone. Stocking a
      pantry is credited at ${Math.round(C.DROPOFF_CREDIT * 100)}% of feeding a
      counted block tonight, so this only wins when no hotspot run was worth
      making.</div>` : ""}
    <div class="rb-note">⏱ <b>${b.hoursToPeople}h from report to served</b> —
      arrives ${b.arrivesAt}, ${freshPct}% of its value intact against a
      ${result.expiresAt} expiry. Reward is scaled by that.</div>
    ${result.prepared ? `<div class="rb-note">🍛 <b>Prepared food</b> — only collectors that accept
      prepared meals were considered (${result.eligibleCount} of ${result.collectorCount}).</div>` : ""}
    ${servedBefore > 0 ? `<div class="rb-note">🎯 <b>Serving limit</b> — this block already received
      ${fmtInt(servedBefore)} meals tonight; only its remaining need of ${b.remaining.toFixed(1)}
      counts toward the reward.</div>` : ""}
    ${h && boostPct > 0 ? `<div class="rb-note">⚡ <b>Access-gap boost +${boostPct}%</b> — this block has
      scheduled food access only ${h.accessDays.toFixed(h.accessDays % 1 ? 1 : 0)} days/week, so its
      reward is weighted up.</div>` : ""}
    ${b.uncollectedLbs >= 1 ? `<div class="rb-note">🚐 <b>Unit capacity ${a.capacityLbs} lbs</b> —
      the pantry van collects ${fmtInt(b.collectedLbs)} lbs; ${fmtInt(b.uncollectedLbs)} lbs stay
      with the donor for a second pickup.</div>` : ""}
    ${b.surplus >= 1 ? `<div class="rb-note">📦 Block need absorbs ${fmtInt(b.served)} meals;
      remaining <b>${fmtInt(b.surplus)} meals</b> ${isPantry
        ? `stock ${agShort(a)}&rsquo;s next scheduled distribution`
        : `ride along to ${agShort(a)}&rsquo;s pantry network`}.</div>` : ""}
    <div class="rb-note tax">🧾 Confirming logs a receipt for <b>${s.name}</b> — est. deductible
      fair market value <b>${fmt$(fmv)}</b> (${s.report.lbs} lbs &times; $${C.FMV_PER_LB}/lb).</div>

    <table class="rb-table">
      <tr><td>${isPantry ? "Site" : "HQ"} &rarr; pickup &rarr; hotspot</td><td>${b.leg1.toFixed(1)} + ${b.leg2.toFixed(1)} mi</td></tr>
      <tr><td>Drive + handling time</td><td>${fmtInt(b.driveMin)} + ${C.HANDLING_MIN} min</td></tr>
      <tr><td>Personnel ($${C.WAGE_PER_HR}/hr)</td><td>${fmt$(b.labor)}</td></tr>
      <tr><td>Vehicle ($${C.COST_PER_MILE}/mi)</td><td>${fmt$(b.mileage)}</td></tr>
      <tr class="total"><td>Deployment cost</td><td>${fmt$(b.cost)}</td></tr>
    </table>

    ${result.rejections.length ? `<div class="rb-h">Ruled out</div>
      ${result.rejections.map(r => `<div class="alt-row">
        <span class="alt-rank">&times;${r.count}</span>
        <span class="alt-pair">${r.example}</span></div>`).join("")}` : ""}

    <div class="rb-h">Runners-up (${fmtInt(result.evaluated)} pairs evaluated)</div>
    ${alts.map((p, i) => `
      <div class="alt-row"><span class="alt-rank">${i + 2}.</span>
        <span class="alt-pair"><b>${p.collector.kind === "pantry" ? "🚐 " : ""}${agShort(p.collector)}</b>
          &rarr; ${p.hotspot.location}</span>
        <span class="alt-net">${fmt$(p.net)}</span></div>`).join("")}

    <details class="model">
      <summary>How this was scored — the reward&#8209;cost model</summary>
      <div class="model-body"><em>net = reward − cost</em>
collectors = agency trucks (${C.AGENCY_CAPACITY_LBS} lb) + pantry
  units on site tonight (${C.PANTRY_CAPACITY_LBS} lb van)
serving limit = ${C.MAX_DROPS_PER_NIGHT} deliveries/hotspot/night; served
  blocks leave the pool, partial blocks count remaining need
reward = min(collected meals, remaining need) × $${C.MEAL_VALUE}/meal × accessBoost
         + overflow meals × $${C.MEAL_VALUE} × 0.5 (pantry)
accessBoost = 1 + ${C.ACCESS_BOOST_MAX} × (7 − access days/wk)/7
cost = (drive + ${C.HANDLING_MIN} min) × $${C.WAGE_PER_HR}/hr        [SD min. wage 2026]
       + road miles × $${C.COST_PER_MILE}/mi              [IRS rate 2026]
meals = lbs ÷ ${C.LBS_PER_MEAL}                        [Feeding America]
distances: haversine × ${C.ROAD_FACTOR} at ${C.AVG_SPEED_MPH} mph city speed</div>
    </details>`;

  $("resultBody").hidden = false;
  $("confirmBtn").addEventListener("click", confirmDispatch);

  /* count-up + bar-grow animations */
  const maxBar = Math.max(b.reward, b.cost, 1);
  requestAnimationFrame(() => {
    document.querySelectorAll(".sv-fill").forEach(el => {
      el.style.width = el.dataset.pct !== undefined
        ? el.dataset.pct + "%"                      /* freshness is already a % */
        : (parseFloat(el.dataset.w) / maxBar * 100) + "%";
    });
  });
  document.querySelectorAll("#resultBody .v[data-count]").forEach(el => {
    const target = parseFloat(el.dataset.count);
    const money = el.dataset.money, dec = el.dataset.dec ? 1 : 0;
    const start = performance.now();
    (function step(now) {
      const p = Math.min((now - start) / 900, 1);
      const v = target * (1 - Math.pow(1 - p, 3));
      el.textContent = money ? fmt$(v) : v.toFixed(dec ? 1 : 0);
      if (p < 1) requestAnimationFrame(step);
    })(start);
  });
}

/* ------------------------------------------------------------ confirming */
async function confirmDispatch() {
  if (!currentMatch) return;
  const { supplier: s } = currentMatch;
  if (dispatchedSupplierIds().has(s.id)) return;

  const btn = $("confirmBtn");
  if (btn) { btn.disabled = true; btn.textContent = "Confirming…"; }

  let res;
  try {
    res = await api(`/api/board/confirm/${s.id}`, { method: "POST" });
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "Confirm dispatch"; }
    return;
  }

  tonight = res.tonight;
  refreshHotspots();
  renderFeed();
  renderStats();
  $("ledgerCount").textContent = tonight.length;

  const h = res.hotspot;
  if (btn) btn.outerHTML = `<div class="confirmed-note">✓ Dispatched — receipt ${res.receipt.receipt} logged
    <div class="cn-sub">${h.location} ${h.closed
      ? (h.closedWhy === "drops"
          ? `has had its ${C.MAX_DROPS_PER_NIGHT} deliveries for tonight and leaves the candidate pool`
          : "is now fully served tonight and leaves the candidate pool")
      : `has ${h.remaining} need remaining tonight`}</div></div>`;
}

/* ---------------------------------------------------------------- ledger */
function renderLedger() {
  const all = [...tonight].reverse().map(r => ({ ...r, isTonight: true }))
    .concat([...HISTORY].reverse());
  const grand = [...HISTORY, ...tonight];

  const totalLbs = grand.reduce((t, r) => t + r.lbs, 0);
  const totalFmv = grand.reduce((t, r) => t + r.fmv, 0);
  const totalFed = grand.reduce((t, r) => t + r.servedMeals, 0);
  $("ledgerTiles").innerHTML = [
    [fmtInt(grand.length), "deliveries logged", ""],
    [fmtInt(tonight.length), "tonight", "lt-route"],
    [fmtInt(totalLbs) + " lbs", "food recovered", ""],
    [fmtInt(totalFed), "people fed", "lt-route"],
    ["$" + fmtInt(totalFmv), "est. FMV deductions", "lt-good"],
  ].map(([v, k, cls]) => `<div class="lt ${cls}"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");

  $("ledgerLog").innerHTML = all.length ? all.map(r => `
    <div class="receipt ${r.isTonight ? "tonight" : ""}">
      <div class="rt-top">
        <span class="rt-id">${r.receipt}</span>
        ${r.isTonight ? '<span class="rt-badge">tonight</span>' : ""}
        <span class="rt-when">${r.date} &middot; ${r.time}</span>
      </div>
      <div class="rt-flow"><b>${r.supplier}</b> <span class="rt-arrow">&rarr;</span>
        ${r.kind === "pantry" ? "🚐" : "🚚"} ${agShort({ name: r.collector })}
        <span class="rt-arrow">&rarr;</span> 📍 ${r.hotspot}</div>
      <div class="rt-nums">
        <span><b>${r.lbs}</b> lbs donated</span>
        <span><b>${r.servedMeals}</b> people fed</span>
        <span class="fmv">est. FMV <b>${fmt$(r.fmv)}</b></span>
        <span>net ${fmt$(r.net)}</span>
      </div>
    </div>`).join("") : '<div class="served-empty">No deliveries logged yet.</div>';

  /* donor tax summary */
  const byDonor = {};
  for (const r of grand) {
    (byDonor[r.supplier] ||= { n: 0, lbs: 0, fmv: 0 });
    byDonor[r.supplier].n++; byDonor[r.supplier].lbs += r.lbs; byDonor[r.supplier].fmv += r.fmv;
  }
  const donors = Object.entries(byDonor).sort((a, b) => b[1].fmv - a[1].fmv);
  $("ledgerDonors").innerHTML = `
    <table class="donor-table">
      <tr><th>Donor</th><th>Deliveries</th><th>Lbs</th><th>Est. FMV</th></tr>
      ${donors.map(([name, d]) => `
        <tr><td>${name}</td><td>${d.n}</td><td>${fmtInt(d.lbs)}</td>
        <td class="fmv">${fmt$(d.fmv)}</td></tr>`).join("")}
    </table>`;

  /* hotspots served tonight */
  $("limitLabel").textContent = C.MAX_DROPS_PER_NIGHT;
  const servedIds = [...new Set(tonight.map(r => r.hotspotId))];
  $("ledgerServed").innerHTML = servedIds.length ? servedIds.map(hid => {
    const h = HOTSPOTS.find(x => x.id === hid);
    const meals = servedMealsTonight(hid), drops = dropsTonight(hid);
    const closed = hotspotClosed(h);
    return `
    <div class="served-row">
      <div class="sr-top">
        <span class="sr-loc">${h.location}</span>
        ${closed ? '<span class="sr-limit">at limit</span>' : ""}
        <span class="sr-stat">${fmtInt(meals)}/${h.need.toFixed(0)} meals &middot; ${drops}/${C.MAX_DROPS_PER_NIGHT} drops</span>
      </div>
      <div class="sr-bar"><span style="width:${Math.min(meals / h.need * 100, 100)}%"></span></div>
    </div>`;
  }).join("") : '<div class="served-empty">Nothing served yet tonight — every block is still in the pool.</div>';
}

function openLedger() {
  renderLedger();
  $("ledger").hidden = false;
  document.body.classList.add("ledger-open");
}
function closeLedger() {
  $("ledger").hidden = true;
  document.body.classList.remove("ledger-open");
}
$("ledgerBtn").addEventListener("click", openLedger);
$("ledgerClose").addEventListener("click", closeLedger);
$("ledger").addEventListener("click", e => { if (e.target === $("ledger")) closeLedger(); });
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !$("ledger").hidden) closeLedger();
});
$("resetBtn").addEventListener("click", async () => {
  await api("/api/board/ledger/reset", { method: "POST" });
  location.reload();
});

/* ----------------------------------------------------------- theme toggle */
function setThemeButton() {
  $("themeBtn").textContent = theme === "dark" ? "☀️" : "🌙";
  $("themeBtn").title = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
}
$("themeBtn").addEventListener("click", () => {
  theme = theme === "dark" ? "light" : "dark";
  try { localStorage.setItem("bellyup.theme", theme); } catch (e) { /* best effort */ }
  applyThemeAttr();
  setThemeButton();
  tiles.setUrl(TILE_URLS[theme]);
  refreshHotspots();
  fxLayer.eachLayer(l => {
    if (l.options.bellyRole && l.setStyle) l.setStyle({ color: themeColor(l.options.bellyRole) });
  });
});

/* ============================================ report in / update / boot */
/* The form does two jobs. A restaurant not yet on the platform registers and
   reports in one step; one already on it revises TONIGHT's numbers. Surplus
   differs every night — a fixed quantity per partner would make the feed a
   fixture rather than a report. */

let formMode = "register";
let formTarget = null;

function openForm(supplier) {
  const form = $("regForm");
  formTarget = supplier || null;
  formMode = supplier ? "edit" : "register";

  $("regOpen").hidden = true;
  form.hidden = false;
  $("regErr").hidden = true;
  delete form.dataset.lat;
  delete form.dataset.lon;

  const isEdit = formMode === "edit";
  $("regWho").hidden = isEdit;
  $("regFor").hidden = !isEdit;
  $("regNone").hidden = !(isEdit && supplier && supplier.report);
  $("regRemove").hidden = !isEdit;
  $("regRemove").textContent = supplier && supplier.registered
    ? "Remove this restaurant from the platform"
    : "This business has left the platform";
  $("regTitle").textContent = isEdit
    ? "Tonight's surplus"
    : "Register & report tonight's surplus";
  $("regSubmit").textContent = isEdit ? "Update & re-match" : "Find a collector";

  if (isEdit) {
    const r = supplier.report;
    $("regFor").innerHTML = `<b>${supplier.name}</b><br>${supplier.address}`
      + (supplier.registered ? "<br>saved to the restaurant dataset" : "")
      + (r ? "" : "<br>no surplus reported yet tonight");
    $("regKind").value = supplier.surplus || "prepared";
    $("regLbs").value = r ? r.lbs : 40;
    $("regItems").value = r ? (r.items || "") : "";
    $("regFrom").value = (r && r.pickupFrom) || "18:30";
    $("regTo").value = (r && r.pickupTo) || "20:30";
    $("regExp").value = (r && r.expiresAt) || "22:00";
    $("regFresh").value = (r && r.freshness) || "fresh";
    $("regLbs").focus();
    $("regLbs").select();
  } else {
    $("regName").value = "";
    $("regAddr").value = "";
    $("regItems").value = "";
    $("regLbs").value = 40;
    $("regGeo").className = "reg-hint";
    $("regGeo").textContent = "Type your address and press Enter.";
    $("regName").focus();
  }
  form.scrollIntoView({ block: "start", behavior: "smooth" });
}

function closeForm() {
  $("regForm").hidden = true;
  $("regOpen").hidden = false;
  $("regErr").hidden = true;
  formTarget = null;
  formMode = "register";
}

async function refreshAll() {
  await loadBoard();
  buildLayers();
  renderFeed();
  renderStats();
  refreshHotspots();
  $("ledgerCount").textContent = tonight.length;
}

function wireForm() {
  const form = $("regForm");

  $("regOpen").addEventListener("click", () => openForm(null));
  $("regCancel").addEventListener("click", closeForm);

  /* resolve the address as they leave the field, so a bad one is caught
     before they submit rather than after */
  $("regAddr").addEventListener("change", async e => {
    const q = e.target.value.trim();
    const hint = $("regGeo");
    if (!q) {
      hint.className = "reg-hint";
      hint.textContent = "Type your address and press Enter.";
      return;
    }
    hint.className = "reg-hint";
    hint.textContent = "Looking up…";
    try {
      const d = await api("/api/geocode?address=" + encodeURIComponent(q));
      hint.className = "reg-hint ok";
      hint.textContent = "\u{1F4CD} " + d.matched;
      form.dataset.lat = d.lat;
      form.dataset.lon = d.lon;
    } catch (err) {
      hint.className = "reg-hint bad";
      hint.textContent = err.message;
      delete form.dataset.lat;
      delete form.dataset.lon;
    }
  });

  /* "nothing tonight" is a real answer, not a missing one */
  $("regNone").addEventListener("click", async () => {
    if (!formTarget) return;
    const id = formTarget.id;
    try {
      await api(`/api/board/report/${id}`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ has_surplus: false }),
      });
      closeForm();
    } catch (e) {
      $("regErr").textContent = e.message;
      $("regErr").hidden = false;
    }
    await refreshAll();
    if (selectedId === id) { selectedId = null; clearFx(); showEmpty(); }
  });

  $("regRemove").addEventListener("click", async () => {
    if (!formTarget) return;
    const gone = formTarget.id;
    try {
      await api(`/api/board/supplier/${gone}`, { method: "DELETE" });
      closeForm();
    } catch (e) {
      /* most likely the page holds a supplier the server no longer has */
      $("regErr").textContent = e.message + " — refreshing the feed.";
      $("regErr").hidden = false;
      setTimeout(closeForm, 1400);
    }
    await refreshAll();
    if (selectedId === gone) { selectedId = null; clearFx(); showEmpty(); }
  });

  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const err = $("regErr"), btn = $("regSubmit");
    err.hidden = true;
    btn.disabled = true;
    btn.textContent = formMode === "edit" ? "Updating…" : "Matching…";

    const common = {
      surplus: $("regKind").value,
      lbs: parseFloat($("regLbs").value),
      items: $("regItems").value.trim(),
      pickup_from: $("regFrom").value || null,
      pickup_to: $("regTo").value || null,
      expires_at: $("regExp").value || null,
      freshness: $("regFresh").value,
    };

    try {
      let supplier;
      if (formMode === "edit") {
        const res = await api(`/api/board/report/${formTarget.id}`, {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ has_surplus: true, ...common }),
        });
        supplier = res.supplier;
      } else {
        const body = {
          name: $("regName").value.trim(),
          address: $("regAddr").value.trim(),
          facility_type: "restaurant",
          ...common,
        };
        if (!body.name) throw new Error("Give the restaurant a name.");
        if (form.dataset.lat) {
          body.lat = +form.dataset.lat;
          body.lon = +form.dataset.lon;
        }
        const res = await api("/api/board/register", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        supplier = res.supplier;
      }
      closeForm();
      await refreshAll();
      selectedId = null;
      await selectReport(supplier, { force: true });
    } catch (e2) {
      err.textContent = e2.message;
      err.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = formMode === "edit" ? "Update & re-match" : "Find a collector";
    }
  });
}

function showEmpty() {
  $("resultBody").hidden = true;
  $("resultEmpty").style.display = "";
}

/* ------------------------------------------------------------------- boot */
(async function boot() {
  setThemeButton();
  wireForm();          /* static elements — wire before the first fetch, or
                          the visible button swallows an early click */
  try {
    await loadBoard();
  } catch (err) {
    $("resultEmpty").innerHTML =
      `<div class="re-icon">&#9888;</div><p>Could not reach the API.<br>` +
      `Start it with <code>uvicorn app:app --port 8000</code> and reload.</p>`;
    return;
  }
  buildLayers();
  renderFeed();
  renderStats();
  refreshHotspots();
  $("ledgerCount").textContent = tonight.length;
})();
