"use strict";

const $ = (s) => document.querySelector(s);
const MAX_RPM = 9000;
// Both come from the car's model profile (/api/status); these are fallbacks.
let REDLINE = 7200;
let RANGE_FROM = [7300, 7600, 7900, 8200, 8500, 8800];
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const NS = "http://www.w3.org/2000/svg";

const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

let status = null;
let overRev = null;
let ident = null;
let units = store.get("pdiag-units", "us");

async function api(path) {
  const r = await fetch(path);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(body.error || r.statusText);
    e.kind = body.kind;
    throw e;
  }
  return body;
}

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = (n, d = 0) => Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

// ---------------------------------------------------------------- tach
const CX = 200, CY = 200;
const ang = (rpm) => ((-135 + (Math.min(Math.max(rpm, 0), MAX_RPM) / MAX_RPM) * 270) * Math.PI) / 180;
const pt = (rpm, r) => [CX + r * Math.sin(ang(rpm)), CY - r * Math.cos(ang(rpm))];

function svg(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

function sector(r0, r1, a, b) {
  const [x0, y0] = pt(a, r1), [x1, y1] = pt(b, r1), [x2, y2] = pt(b, r0), [x3, y3] = pt(a, r0);
  return `M${x0} ${y0} A${r1} ${r1} 0 0 1 ${x1} ${y1} L${x2} ${y2} A${r0} ${r0} 0 0 0 ${x3} ${y3}Z`;
}

function severity(range, count) {
  if (!count) return "none";
  if (range <= 2) return "ok";
  return range === 3 ? "caution" : "serious";
}

const defaultRanges = () => RANGE_FROM.map((f, i, a) => ({
  range: i + 1, rpm_from: f, rpm_to: a[i + 1] ?? null, ignitions: null,  // not read
}));

function applyProfile() {
  const p = status?.profile;
  if (!p) return;
  if (p.redline === REDLINE && String(p.over_rev_from) === String(RANGE_FROM)) return;
  REDLINE = p.redline;
  RANGE_FROM = p.over_rev_from;
  buildTach();
}

let needle, redArc;
let targetRpm = 0, shownRpm = 0;

function buildTach() {
  const s = $("#tach");
  s.replaceChildren();
  svg("circle", { cx: CX, cy: CY, r: 197, class: "t-bezel" }, s);
  svg("circle", { cx: CX, cy: CY, r: 192, class: "t-face" }, s);

  const [rx0, ry0] = pt(REDLINE, 160), [rx1, ry1] = pt(MAX_RPM, 160);
  redArc = svg("path", { d: `M${rx0} ${ry0} A160 160 0 0 1 ${rx1} ${ry1}`, class: "t-red" }, s);

  for (let r = 0; r <= MAX_RPM; r += 250) {
    const major = r % 1000 === 0;
    const [x0, y0] = pt(r, 153), [x1, y1] = pt(r, major ? 131 : 144);
    svg("line", { x1: x0, y1: y0, x2: x1, y2: y1, class: "t-tick", "stroke-width": major ? 3.5 : 1.5 }, s);
    if (major) {
      const [tx, ty] = pt(r, 110);
      const t = svg("text", { x: tx, y: ty + 10, "text-anchor": "middle", class: "t-num" }, s);
      t.textContent = r / 1000;
    }
  }
  const x = svg("text", { x: CX, y: CY - 62, "text-anchor": "middle", class: "t-x" }, s);
  x.textContent = "×1000 rpm";

  const bands = svg("g", { id: "bands" }, s);
  drawBands(bands, overRev?.ranges ?? defaultRanges());

  needle = svg("g", {}, s);
  svg("line", { x1: CX, y1: CY + 26, x2: CX, y2: CY - 150, class: "t-needle" }, needle);
  svg("circle", { cx: CX, cy: CY, r: 13, class: "t-hub" }, s);
  setNeedle(shownRpm);
}

function drawBands(g, ranges) {
  g.replaceChildren();
  for (const r of ranges) {
    const to = r.rpm_to ?? MAX_RPM;
    const sev = severity(r.range, r.ignitions);
    const p = svg("path", { d: sector(169, 187, r.rpm_from + 15, to - 15), class: "t-band", "data-sev": sev }, g);
    const title = svg("title", {}, p);
    title.textContent = r.ignitions == null ? `Range ${r.range}: not read yet`
      : `Range ${r.range}: ${fmt(r.ignitions)} ignitions`;
    const [tx, ty] = pt((r.rpm_from + to) / 2, 178);
    const t = svg("text", { x: tx, y: ty + 4, "text-anchor": "middle", class: "t-band-num", "data-sev": sev }, g);
    t.textContent = r.range;
  }
}

function setNeedle(rpm) {
  const deg = -135 + (Math.min(Math.max(rpm, 0), MAX_RPM) / MAX_RPM) * 270;
  needle.setAttribute("transform", `rotate(${deg} ${CX} ${CY})`);
  redArc.classList.toggle("hot", rpm >= REDLINE - 150);
}

// Readings arrive ~3 times a second. Ease toward each one over about that long, so
// the needle keeps moving instead of snapping and waiting. Frame-rate independent.
const NEEDLE_TAU = 0.18;
let lastFrame = null, shownText = "";
function animate(now) {
  const dt = lastFrame == null ? 0 : Math.min(0.1, (now - lastFrame) / 1000);
  lastFrame = now;
  shownRpm = reduceMotion ? targetRpm : shownRpm + (targetRpm - shownRpm) * (1 - Math.exp(-dt / NEEDLE_TAU));
  setNeedle(shownRpm);
  const text = fmt(Math.round(shownRpm / 10) * 10);  // the digits roll with the needle
  if (text !== shownText) { $("#rpm-num").textContent = text; shownText = text; }
  requestAnimationFrame(animate);
}

// ---------------------------------------------------------------- readouts
const READOUTS = [
  { key: "coolant_temp", label: "Coolant temperature", unit: () => (units === "us" ? "°F" : "°C"),
    conv: (v) => (units === "us" ? v * 1.8 + 32 : v), dec: 0, scale: [40, 120], band: [80, 100] },
  { key: "timing_advance", label: "Ignition timing", unit: () => "° advance", dec: 1, scale: [-10, 45] },
  { key: "throttle", label: "Throttle", unit: () => "%", dec: 0, scale: [0, 100] },
  { key: "engine_load", label: "Engine load", unit: () => "%", dec: 0, scale: [0, 100] },
  { key: "maf", label: "Air flow", unit: () => "g/s", dec: 1, scale: [0, 200] },
  { key: "speed", label: "Road speed", unit: () => (units === "us" ? "mph" : "km/h"),
    conv: (v) => (units === "us" ? v * 0.6214 : v), dec: 0, scale: [0, 160] },
  { key: "stft_b1", label: "Short-term fuel trim", unit: () => "%", dec: 1, scale: [-25, 25], band: [-10, 10] },
  { key: "ltft_b1", label: "Long-term fuel trim", unit: () => "%", dec: 1, scale: [-25, 25], band: [-10, 10] },
];
const HISTORY = 150;
const hist = {};

function buildReadouts() {
  const box = $("#readouts");
  box.innerHTML = READOUTS.map((r) => `
    <div class="ro" id="ro-${r.key}">
      <div class="ro-label">${r.label}</div>
      <div class="ro-value"><span class="ro-num">–</span><span class="ro-unit">${r.unit()}</span></div>
      <svg viewBox="0 0 ${HISTORY} 28" preserveAspectRatio="none" aria-hidden="true">
        ${r.band ? `<rect class="band" x="0" width="${HISTORY}" y="${yOf(r, r.band[1])}" height="${yOf(r, r.band[0]) - yOf(r, r.band[1])}"/>` : ""}
        <polyline class="spark" points=""/>
      </svg>
    </div>`).join("");
}

function yOf(r, raw) {
  const [lo, hi] = r.scale;
  return 28 - ((Math.min(Math.max(raw, lo), hi) - lo) / (hi - lo)) * 28;
}

function updateReadouts(values) {
  for (const r of READOUTS) {
    const raw = values[r.key];
    const node = $(`#ro-${r.key}`);
    if (raw == null) {
      // Each message is a full snapshot: a missing value isn't known (yet), or
      // the car stopped reporting it. Don't leave an old number looking live.
      hist[r.key] = [];
      node.querySelector(".ro-num").textContent = "–";
      node.querySelector(".spark").setAttribute("points", "");
      continue;
    }
    (hist[r.key] ??= []).push(raw);
    if (hist[r.key].length > HISTORY) hist[r.key].shift();
    node.querySelector(".ro-num").textContent = fmt(r.conv ? r.conv(raw) : raw, r.dec);
    const h = hist[r.key];
    const off = HISTORY - h.length;
    node.querySelector(".spark").setAttribute("points", h.map((v, i) => `${off + i},${yOf(r, v).toFixed(1)}`).join(" "));
  }
}

// ---------------------------------------------------------------- all readings
const LABELS = {
  monitor_status: "Fault lamp and readiness", fuel_system: "Fuel system",
  rpm: "Engine speed", engine_load: "Engine load", throttle: "Throttle",
  coolant_temp: "Coolant temperature", intake_temp: "Intake air temperature",
  maf: "Air flow", timing_advance: "Ignition timing", speed: "Road speed",
  stft_b1: "Short-term fuel trim, bank 1", ltft_b1: "Long-term fuel trim, bank 1",
  stft_b2: "Short-term fuel trim, bank 2", ltft_b2: "Long-term fuel trim, bank 2",
  o2_b1s1: "Oxygen sensor, bank 1 front", o2_b1s2: "Oxygen sensor, bank 1 rear",
  o2_b2s1: "Oxygen sensor, bank 2 front", o2_b2s2: "Oxygen sensor, bank 2 rear",
  secondary_air: "Secondary air", o2_sensors: "Oxygen sensors fitted",
  obd_standard: "OBD standard", map: "Manifold pressure", run_time: "Engine run time",
};
const ORDER = Object.keys(LABELS);

function showValue(key, v) {
  if (v == null) return "–";
  if (typeof v === "string") return v;
  const unit = status?.units?.[key] ?? "";
  if (unit === "C") return units === "us" ? `${fmt(v * 1.8 + 32)} °F` : `${fmt(v)} °C`;
  if (unit === "km/h") return units === "us" ? `${fmt(v * 0.6214)} mph` : `${fmt(v)} km/h`;
  const dec = unit === "V" ? 2 : unit === "%" || unit === "g/s" || unit === "deg" ? 1 : 0;
  return `${fmt(v, dec)}${unit === "deg" ? "°" : unit ? ` ${unit}` : ""}`;
}

let allKeys = null, lastValues = {};
function renderAll(values) {
  lastValues = values;
  const keys = Object.keys(values).sort((a, b) =>
    (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99) || a.localeCompare(b));
  const dl = $("#all");
  if (keys.join() !== allKeys) {  // a reading appeared or went away: rebuild the rows once
    allKeys = keys.join();
    dl.replaceChildren(...keys.flatMap((k) => {
      const dt = document.createElement("dt");
      dt.textContent = LABELS[k] ?? k.replaceAll("_", " ");
      const dd = document.createElement("dd");
      dd.dataset.key = k;
      return [dt, dd];
    }));
  }
  for (const dd of dl.querySelectorAll("dd")) {  // otherwise only touch text that changed
    const text = showValue(dd.dataset.key, values[dd.dataset.key]);
    if (dd.textContent !== text) dd.textContent = text;
  }
}

// ---------------------------------------------------------------- live stream
let es = null, liveWanted = false;
function startLive() {
  liveWanted = true;
  if (es) return;
  es = new EventSource("/api/live");
  es.onerror = (ev) => {
    // The numbers are old now. Reconnect ourselves every 2 s: the browser's own
    // retry can back off for a long time after pdiag restarts.
    setLink("err", "<strong>Lost contact with pdiag.</strong> Reconnecting.");
    setStale(true);
    ev.target.close();
    if (es === ev.target) es = null;
    setTimeout(() => { if (liveWanted) startLive(); }, 2000);
  };
  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.error) {
      setLink("err", msg.kind === "internal"
        ? `<strong>pdiag hit an error.</strong> ${esc(msg.error)}`
        : `<strong>No answer from the car.</strong> Retrying.`);
      setStale(true);
      return;
    }
    setStale(false);
    if (status) { status.connected = true; renderLink(); }
    targetRpm = msg.values.rpm;
    updateReadouts(msg.values);
    renderAll(msg.values);
  };
}
function setStale(on) {
  $(".live").classList.toggle("stale", on);
  $("#all").classList.toggle("stale", on);
}
function stopLive() {
  liveWanted = false;
  if (es) { es.close(); es = null; }
}

// ---------------------------------------------------------------- Porsche data blocks
let dmeEs = null, dmeWanted = false;
const dmePrev = {}, dmeMoved = {};

function startDme() {
  dmeWanted = true;
  if (dmeEs) return;
  const body = $("#dme-body");
  if (!body.children.length) body.innerHTML = waiting("Waking the engine computer. This takes a few seconds.");
  dmeEs = new EventSource("/api/dme-live");
  dmeEs.onerror = (ev) => {
    ev.target.close();
    if (dmeEs === ev.target) dmeEs = null;
    setTimeout(() => { if (dmeWanted) startDme(); }, 2000);
  };
  dmeEs.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.error) {
      setLink("err", msg.kind === "kline"
        ? "<strong>No answer from the engine computer.</strong> Retrying."
        : `<strong>${esc(msg.error)}</strong>`);
      return;
    }
    if (status) { status.connected = true; renderLink(); }
    renderBlocks(msg.blocks, msg.new);
  };
}

function stopDme() {
  dmeWanted = false;
  if (dmeEs) { dmeEs.close(); dmeEs = null; }
}

function renderBlocks(blocks, fresh) {
  const body = $("#dme-body");
  if (body.querySelector(".wait")) body.replaceChildren();
  for (const lid of Object.keys(blocks).sort()) {
    let el = document.getElementById(`lid-${lid}`);
    if (!el) {  // first time we see this block: add its row, in order
      el = document.createElement("div");
      el.className = "lid";
      el.id = `lid-${lid}`;
      el.innerHTML = `<div class="lid-name">Block ${lid.toUpperCase()}<small></small></div><div class="bytes"></div>`;
      body.insertBefore(el, [...body.children].find((c) => c.id > el.id) ?? null);
    } else if (lid !== fresh) {
      continue;  // only the block just read changes
    }
    const bytes = blocks[lid].match(/../g) ?? [];
    const prev = dmePrev[lid], moved = (dmeMoved[lid] ??= new Set()), hot = new Set();
    if (prev) bytes.forEach((b, i) => { if (prev[i] !== b) { hot.add(i); moved.add(i); } });
    dmePrev[lid] = bytes;
    el.querySelector("small").textContent = `${bytes.length} bytes, ${moved.size} moving`;
    el.querySelector(".bytes").innerHTML = bytes.map((b, i) =>
      `<span class="${hot.has(i) ? "hot" : moved.has(i) ? "moved" : ""}" title="byte ${i}">${b}</span>`).join("");
  }
}

// ---------------------------------------------------------------- header
let linkNow = "";
function setLink(state, html) {
  if (linkNow === state + html) return;
  linkNow = state + html;
  const n = $("#link-state");
  n.dataset.state = state;
  n.innerHTML = `<span class="lamp" aria-hidden="true"></span><span>${html}</span>`;
}

function renderLink() {
  if (status.mode === "mock") setLink("sim", "<strong>Simulated car.</strong> No cable needed.");
  else setLink(status.connected ? "ok" : "", `Cable on <strong>${esc(status.port.replace("/dev/", ""))}</strong>`);
}

const VIN_YEARS = { V: 1997, W: 1998, X: 1999, Y: 2000, 1: 2001, 2: 2002, 3: 2003, 4: 2004, 5: 2005, 6: 2006, 7: 2007, 8: 2008, 9: 2009 };

function renderHeader() {
  applyProfile();
  $("#car-name").textContent = status?.car ?? "Porsche";
  const year = ident?.vin && VIN_YEARS[ident.vin[9]];
  const sub = [];
  if (year) sub.push(`${year} model year`);
  if (ident?.vin) sub.push(`VIN ${ident.vin}`);
  $("#car-sub").textContent = sub.length ? sub.join(", ") : status?.mode === "mock" ? "" : "Open Car info to read the VIN.";
  document.title = `${status?.car ?? "pdiag"} diagnostics`;
}

// ---------------------------------------------------------------- states
const waiting = (msg) => `<div class="wait">${msg}</div>`;

function errorState(e) {
  if (e.kind === "kline") {
    return `<div class="state"><h3>The car didn't answer</h3>
      <p>Check that the cable is plugged into the OBD port under the dashboard and the ignition is on, then select Read again.</p>
      <details><summary>Technical detail</summary>${esc(e.message)}</details></div>`;
  }
  return `<div class="state"><h3>Couldn't read the car</h3><p>${esc(e.message)}</p></div>`;
}

// ---------------------------------------------------------------- report
async function loadReport() {
  const body = $("#report-body");
  body.innerHTML = waiting("Reading the over-rev counters.");
  try {
    overRev = await api("/api/over-rev");
    drawBands($("#bands"), overRev.ranges);
    if (!ident) await loadIdent().catch(() => {});
    body.innerHTML = reportHtml(overRev);
  } catch (e) {
    body.innerHTML = e.kind === "not_decoded"
      ? `<div class="state"><h3>Over-rev counters can't be read yet</h3><p>${esc(e.message)}</p>
         <p>Live data and fault codes work today.</p></div>`
      : errorState(e);
  }
}

function reportHtml(d) {
  const by = Object.fromEntries(d.ranges.map((r) => [r.range, r]));
  const sum = (ids) => ids.reduce((a, i) => a + (by[i]?.ignitions ?? 0), 0);
  const low = sum([1, 2]), r3 = sum([3]), high = sum([4, 5, 6]);
  const ago = (r) => (r?.last_hour == null ? null : d.engine_hours - r.last_hour);

  let sev, headline, sub;
  if (high) {
    const agos = [4, 5, 6].map((i) => ago(by[i])).filter((x) => x != null);
    sev = "serious";
    headline = `${fmt(high)} ignitions recorded in ranges 4 to 6.`;
    sub = `${agos.length ? `The most recent was ${fmt(Math.min(...agos), 1)} engine hours ago. ` : ""}These usually come from a missed downshift that spins the engine past its limit. Have the valvetrain inspected before buying or tracking this car.`;
  } else if (r3) {
    sev = "caution";
    headline = `No serious over-revs, but ${fmt(r3)} ignitions in range 3.`;
    sub = `${ago(by[3]) == null ? "" : `The last one was ${fmt(ago(by[3]), 1)} engine hours ago. `}Range 3 is past the rev limiter, so the accelerator alone didn't put the engine there. Ask the owner about it.`;
  } else {
    sev = "ok";
    headline = "Rev limiter use only. No mechanical over-revs recorded.";
    sub = `${fmt(low)} ignitions in ranges 1 and 2, which is what hitting the limiter under power looks like.`;
  }

  const max = Math.max(...d.ranges.map((r) => r.ignitions), 1);
  const rows = d.ranges.map((r) => {
    const s = severity(r.range, r.ignitions);
    const w = r.ignitions ? Math.max(2, Math.sqrt(r.ignitions / max) * 100) : 0;
    const span = r.rpm_to ? `${fmt(r.rpm_from)}–${fmt(r.rpm_to)}` : `Over ${fmt(r.rpm_from)}`;
    const a = ago(r);
    return `<tr>
      <td class="r">${r.range}<small class="speed-m">${span} rpm</small></td>
      <td class="speed">${span} rpm</td>
      <td class="count">${r.ignitions
        ? `<div class="bar"><i style="width:${w}%" data-sev="${s}"></i><b>${fmt(r.ignitions)}</b></div>`
        : `<span class="zero">None</span>`}</td>
      <td>${a == null ? `<span class="zero">Never</span>`
        : `${fmt(a, 1)} h ago<span class="when-full"> <span class="zero">at ${fmt(r.last_hour, 1)} h</span></span>`}</td>
    </tr>`;
  }).join("");

  const read = [`Read at ${fmt(d.engine_hours, 1)} engine hours`];
  if (ident?.vin) read.push(`VIN ${esc(ident.vin)}`);

  return `
    ${status?.mode === "mock" ? `<p class="sim-note">This report uses simulated data. The range boundaries are placeholders until they're decoded from a real engine computer.</p>` : ""}
    <div class="verdict" data-sev="${sev}">
      <div><p class="headline">${headline}</p><p class="sub">${sub}</p><p class="sub">${read.join(", ")}.</p></div>
    </div>
    <div class="table-scroll"><table class="ranges">
      <thead><tr><th>Range</th><th class="speed">Engine speed</th><th>Ignitions</th><th>Last recorded</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <div class="explain">
      <p>The engine computer counts every ignition that happens above the rev limit, in six speed ranges.</p>
      <p>Ranges 1 and 2 come from hitting the limiter under power. That's normal driving. Ranges 3 to 6 can't be reached with the accelerator, because the limiter cuts fuel first. They mean the wheels drove the engine faster, usually through a missed downshift.</p>
    </div>`;
}

// ---------------------------------------------------------------- codes
async function loadCodes() {
  const body = $("#codes-body");
  const btn = $("#codes-btn");
  btn.disabled = true;
  body.innerHTML = waiting(status?.mode === "mock" ? "Reading fault codes." : "Waking the engine computer. This takes a few seconds.");
  try {
    const codes = await api("/api/dtcs");
    if (!codes.length) {
      body.innerHTML = `<div class="state"><h3>No fault codes stored</h3><p>The engine computer's fault memory is empty.</p></div>`;
      return;
    }
    body.innerHTML = `<ul class="codes">${codes.map((c) => `
      <li>
        <div class="code">${esc(c.code)}</div>
        <div>
          <div class="code-text${c.text ? "" : " unknown"}">${c.text ? esc(c.text) : "No description in pdiag yet."}</div>
          <div class="chips">
            ${c.lamp ? `<span class="chip lamp">Turns on the check engine light</span>` : ""}
            <span class="chip${c.present ? " now" : ""}">${c.present ? "Present now" : "Stored, not present now"}</span>
            <span class="raw">Raw ${esc(c.raw)}, status ${esc(c.status)}</span>
          </div>
        </div>
      </li>`).join("")}</ul>`;
  } catch (e) {
    body.innerHTML = errorState(e);
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- info
async function loadIdent() {
  ident = await api("/api/ident");
  status = { ...status, ...ident };
  renderHeader();
  renderLink();
  return ident;
}

async function loadInfo() {
  const body = $("#info-body");
  const btn = $("#info-btn");
  btn.disabled = true;
  body.innerHTML = waiting("Reading the engine computer.");
  try {
    const d = await loadIdent();
    const rows = [
      ["VIN", d.vin ?? "Not reported", "big"],
      ["Engine computer software", d.software ?? "Not reported"],
      ["Engine computer address", d.dme_address],
      ["Engine computer keybytes", d.dme_keybytes],
      ["Generic OBD protocol", d.generic_protocol ?? "Not checked yet. Open Live to check it."],
      ["Connection", d.mode === "mock" ? "Simulated car" : `KKL cable on ${d.port}`],
    ];
    const extra = Object.entries(d.raw ?? {}).filter(([k]) => !["90", "9B"].includes(k));
    for (const [k, v] of extra) rows.push([`Identification ${k}`, v]);
    body.innerHTML = `<dl class="info">${rows.map(([k, v, cls]) =>
      `<dt>${esc(k)}</dt><dd${cls ? ` class="${cls}"` : ""}>${esc(v ?? "–")}</dd>`).join("")}</dl>`;
  } catch (e) {
    body.innerHTML = errorState(e);
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- router
const loaded = {};
const VIEWS = { live: null, report: loadReport, codes: loadCodes, dme: null, info: loadInfo };

function route() {
  const v = location.hash.slice(1) in VIEWS ? location.hash.slice(1) : "live";
  for (const k in VIEWS) $(`#view-${k}`).hidden = k !== v;
  document.querySelectorAll(".tabs a").forEach((a) => {
    if (a.dataset.view === v) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  // One K-line session at a time: only stream while Live is open.
  if (v === "live") startLive(); else stopLive();
  if (v === "dme") startDme(); else stopDme();
  if (VIEWS[v] && !loaded[v]) { loaded[v] = true; VIEWS[v](); }
}

function unitToggle() {
  const note = $("#live-note");
  const b = document.createElement("button");
  b.type = "button";
  b.className = "btn";
  b.style.marginLeft = "12px";
  const label = () => (b.textContent = units === "us" ? "Show °C and km/h" : "Show °F and mph");
  label();
  b.onclick = () => {
    units = units === "us" ? "metric" : "us";
    store.set("pdiag-units", units);
    label();
    buildReadouts();
    for (const k in hist) hist[k] = [];
    renderAll(lastValues);
  };
  note.after(b);
}

async function init() {
  buildTach();
  buildReadouts();
  unitToggle();
  requestAnimationFrame(animate);
  $("#print-btn").onclick = () => window.print();
  $("#codes-btn").onclick = loadCodes;
  $("#info-btn").onclick = loadInfo;
  window.addEventListener("hashchange", route);
  try {
    status = await api("/api/status");
    renderLink();
    renderHeader();
  } catch {
    setLink("err", "<strong>pdiag isn't running.</strong> Start it with <code>pdiag ui</code>.");
  }
  if (status?.mode === "mock") {
    // Simulated car answers instantly; fill the header and dial bands up front.
    api("/api/over-rev").then((d) => { overRev = d; drawBands($("#bands"), d.ranges); }).catch(() => {});
    loadIdent().catch(() => {});
  }
  route();
}

init();
