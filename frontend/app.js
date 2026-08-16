/* BreathCheck — handheld kiosk frontend */
"use strict";

const $ = (sel) => document.querySelector(sel);
const api = async (path, options) => {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
};
const postJson = (path, data, method = "POST") =>
  api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
/* mV·s integrals: two decimals while small, rounded once large */
const fmtVal = (value) => {
  const n = Number(value) || 0;
  if (Math.abs(n) >= 1000) return Math.round(n).toLocaleString("en-US");
  return Math.abs(n) >= 100 ? n.toFixed(1) : n.toFixed(2);
};

const state = {
  screen: "home",
  settings: null,
  scan: null,        // active scan session (from /api/scan/start)
  result: null,      // completed reading
  gpsFix: { gps1: "", gps2: "" },
  photoData: "",     // captured JPEG data-url
  stream: null,
  timers: { countdown: null, gps: null, poll: null },
};

/* ============================== sound ============================== */

let audioCtx = null;
function beep(freq = 880, ms = 70, type = "sine", gainValue = 0.08) {
  if (!state.settings || !state.settings.sound) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = type;
    osc.frequency.value = freq;
    gain.gain.value = gainValue;
    osc.connect(gain).connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + ms / 1000);
  } catch (err) { /* audio unavailable */ }
}
const sndTick = () => beep(880, 60);
const sndShutter = () => beep(1400, 90, "square", 0.05);
const sndPass = () => { beep(660, 120); setTimeout(() => beep(990, 180), 130); };
const sndFail = () => beep(200, 500, "square", 0.1);

/* ============================== toast ============================== */

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("err", isError);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
}

/* ============================== navigation ============================== */

const SCREEN_TITLES = {
  home: "BREATHCHECK", scan: "SCAN", form: "DETAILS", saved: "SAVED",
  database: "DATABASE", gps: "GPS",
};

function showScreen(name) {
  document.querySelectorAll(".screen").forEach((el) => el.classList.remove("active"));
  $(`#screen-${name}`).classList.add("active");
  $("#top-title").textContent = SCREEN_TITLES[name] || "BREATHCHECK";
  $("#btn-back").classList.toggle("hidden", name === "home");
  state.screen = name;

  clearInterval(state.timers.gps);
  if (name !== "scan" && name !== "form") stopCamera();

  if (name === "home") refreshStatus();
  if (name === "scan") enterScanReady();
  if (name === "database") loadRecords($("#db-search").value.trim());
  if (name === "gps") {
    refreshGps();
    state.timers.gps = setInterval(refreshGps, 3000);
  }
}

function goHome() {
  cancelCountdown();
  showScreen("home");
}

/* ============================== clock ============================== */

const IST_TZ = "Asia/Kolkata";   // show IST regardless of the device timezone
function tickClock() {
  const now = new Date();
  const hhmm = now.toLocaleTimeString([], { timeZone: IST_TZ, hour: "2-digit", minute: "2-digit", hour12: false });
  $("#top-clock").textContent = hhmm;
  $("#home-time").textContent = hhmm;
  $("#home-date").textContent = now.toLocaleDateString([], {
    timeZone: IST_TZ, weekday: "long", day: "2-digit", month: "short", year: "numeric",
  });
}

/* ============================== home status ============================== */

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    $("#chip-set").textContent = `SET ${status.set_no || "--"}`;
    $("#chip-records").textContent = `${status.records} RECORD${status.records === 1 ? "" : "S"}`;
    $("#chip-sensor").textContent =
      status.stream_ok === false ? "SENSOR OFFLINE"
        : status.sensor_state === "stabilizing" ? "SENSOR WARM-UP"
        : status.analyzer === "spi" ? "SENSOR LIVE" : "SENSOR DEMO";
  } catch (err) { /* backend not ready yet */ }
}

/* ============================== camera ============================== */
/* getUserMedia can't reach this board's MIPI sensor, so the live preview is
   an MJPEG stream the backend produces (/api/camera/stream); the exhale photo
   is grabbed server-side from that same stream. The <img> feeds share one
   backend stream request via a cache-busting src. */

function startCamera() {
  const url = `/api/camera/stream?t=${Date.now()}`;
  ["#cam", "#cam2"].forEach((sel) => {
    const img = $(sel);
    img.onerror = () => $("#cam-off").classList.remove("hidden");
    img.onload = () => $("#cam-off").classList.add("hidden");
    img.src = url;
  });
  state.stream = true;
  return true;
}

function stopCamera() {
  ["#cam", "#cam2"].forEach((sel) => {
    const img = $(sel);
    img.onerror = null;
    img.onload = null;
    img.removeAttribute("src");
  });
  state.stream = false;
}

/* Photo is captured by the backend from the live stream; nothing to grab in
   the browser on this board. */
function capturePhoto() {
  return "";
}

/* ============================== scan flow ============================== */

function showScanStage(stage) {
  ["scan-ready", "scan-run", "scan-wait", "scan-result", "scan-error"].forEach((id) =>
    $(`#${id}`).classList.toggle("hidden", id !== stage));
}

function enterScanReady() {
  cancelCountdown();
  state.scan = null;
  state.result = null;
  state.photoData = "";
  showScanStage("scan-ready");
  startCamera();
  refreshScanReadyStatus();
  state.timers.poll = setInterval(refreshScanReadyStatus, 500);
}

function cancelCountdown() {
  clearInterval(state.timers.countdown);
  clearInterval(state.timers.poll);
  state.timers.countdown = null;
  state.timers.poll = null;
}

async function refreshScanReadyStatus() {
  if (state.screen !== "scan" || state.scan) return;
  try {
    const status = await api("/api/status");
    const warming = status.sensor_state === "stabilizing";
    const measuring = status.sensor_state === "measuring";
    const offline = status.stream_ok === false;
    const button = $("#btn-start-scan");
    button.disabled = warming || measuring || offline;
    if (offline) {
      button.textContent = "SENSOR OFFLINE";
      $("#scan-ready-hint").textContent = "NO SIGNAL FROM SENSOR — RECONNECTING, PLEASE WAIT";
    } else if (warming) {
      const elapsed = Math.max(0, Math.floor(Number(status.stabilize?.elapsed_s) || 0));
      button.textContent = `WARM-UP ${elapsed}s`;
      $("#scan-ready-hint").textContent = "INITIAL SENSOR WARM-UP — PLEASE WAIT";
    } else if (measuring) {
      button.textContent = "TEST RUNNING";
      $("#scan-ready-hint").textContent = "SENSOR IS FINISHING THE CURRENT TEST — PLEASE WAIT";
    } else {
      const stabilizeSeconds = Math.ceil(Number(status.purge_seconds) || 15);
      const baselineSeconds = Math.ceil(Number(status.baseline_seconds) || 5);
      button.textContent = "START";
      $("#scan-ready-hint").textContent =
        `READY — ${stabilizeSeconds}s STABILIZE + ${baselineSeconds}s BASELINE, THEN BLOW`;
      clearInterval(state.timers.poll);
      state.timers.poll = null;
    }
  } catch (err) { /* keep the scan control usable during a transient status failure */ }
}

async function beginScan() {
  const button = $("#btn-start-scan");
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = "STARTING…";
  clearInterval(state.timers.poll);
  state.timers.poll = null;
  try {
    const [session, gpsFix] = await Promise.all([
      postJson("/api/scan/start", {}),
      api("/api/gps").catch(() => null),
    ]);
    state.scan = session;
    state.gpsFix = {
      gps1: gpsFix && gpsFix.fix ? String(gpsFix.lat) : "",
      gps2: gpsFix && gpsFix.fix ? String(gpsFix.lon) : "",
    };
    trackCycle(session);
  } catch (err) {
    toast(err.message, true);
    button.disabled = false;
    button.textContent = "START";
    refreshScanReadyStatus();
    state.timers.poll = setInterval(refreshScanReadyStatus, 500);
  }
}

/* Measurement cycle (driven by the backend / sensor board):
   purge/stabilize -> baseline -> measure/blow. */
const PHASE_UI = {
  starting: {
    label: "STARTING",
    hint: "STARTING SENSOR — PLEASE WAIT",
    timed: false,
  },
  recovering: {
    label: "RESET",
    hint: "SENSOR RESTARTING — PLEASE WAIT",
    timed: false,
  },
  purge: {
    label: "STABILIZE",
    hint: "SENSOR STABILIZING — DO NOT BLOW YET",
  },
  baseline: {
    label: "BASELINE",
    hint: "SETTING FRESH-AIR BASELINE — DO NOT BLOW YET",
  },
  measure: {
    label: "BLOW",
    hint: "BLOW STEADILY UNTIL THE TIMER ENDS",
  },
};

function trackCycle(session) {
  showScanStage("scan-run");
  $("#photo-tag").classList.add("hidden");
  state.photoData = "";

  const ring = $("#ring-fg");
  const circumference = 2 * Math.PI * 96;
  let lastPhase = "";
  let lastCount = -1;
  let phaseStartedAt = Date.now();
  let photoTaken = false;
  let polling = false;
  let pollFailures = 0;

  const showPhase = (phase, remaining, elapsed, total) => {
    const isMeasure = phase === "measure";
    const phaseUi = PHASE_UI[phase] || PHASE_UI.purge;
    if (phase !== lastPhase) {
      lastPhase = phase;
      lastCount = -1;
      phaseStartedAt = Date.now();
      $("#ring-label").textContent = phaseUi.label;
      $("#scan-phase-hint").textContent = phaseUi.hint;
      ring.classList.toggle("prep", !isMeasure);
      $(".ring-wrap").classList.toggle("prep", !isMeasure);
      beep(isMeasure ? 990 : 660, 120);
    }
    if (phaseUi.timed === false) {
      // No fixed duration — count UP so the screen never looks frozen.
      ring.style.strokeDashoffset = "0";
      const waited = Math.floor((Date.now() - phaseStartedAt) / 1000);
      $("#ring-count").textContent = waited >= 1 ? String(waited) : "…";
      return;
    }
    ring.style.strokeDashoffset = String(circumference * Math.min(1, elapsed / total));
    const count = Math.max(0, Math.ceil(remaining));
    if (count !== lastCount) {
      lastCount = count;
      $("#ring-count").textContent = String(count);
      if (isMeasure && count > 0) sndTick();
    }
  };

  showPhase("starting", 0, 0, 1);
  sndTick();

  const pollCycle = async () => {
    if (polling) return;
    polling = true;
    try {
      const status = await api(`/api/scan/${session.session_id}`);
      pollFailures = 0;
      if (status.status === "done") {
        cancelCountdown();
        beep(660, 400);
        state.result = status.result;
        // The camera can't be driven from the browser on this board — the
        // backend grabs the exhale photo itself; this flags whether it did.
        state.scan.photoCaptured = !!status.photo_captured;
        showResults(status.result);
        return;
      }
      if (status.status === "error") {
        cancelCountdown();
        $("#scan-error-msg").textContent = status.error || "No response from sensor";
        showScanStage("scan-error");
        sndFail();
        return;
      }

      const phase = status.phase || "purge";
      const total = status.phase_total || 1;
      const remaining = status.phase_remaining != null ? status.phase_remaining : total;
      const elapsed = total - remaining;
      const isMeasure = phase === "measure";

      showPhase(phase, remaining, elapsed, total);

      if (isMeasure && !photoTaken && elapsed >= session.photo_second) {
        photoTaken = true;
        // Browser capture (works only where getUserMedia can reach the camera).
        state.photoData = capturePhoto();
        if (state.photoData) {
          $("#flash").classList.remove("go");
          void $("#flash").offsetWidth;
          $("#flash").classList.add("go");
          $("#photo-tag").classList.remove("hidden");
          sndShutter();
        }
      }
      // On this board the photo comes from the backend — confirm when it lands.
      if (status.photo_captured && $("#photo-tag").classList.contains("hidden")) {
        $("#photo-tag").classList.remove("hidden");
        sndShutter();
      }
    } catch (err) {
      // A single missed status poll is not a sensor failure. Keep the live
      // test on screen and only fail after a sustained API interruption.
      pollFailures += 1;
      if (pollFailures >= 12) {
        cancelCountdown();
        $("#scan-error-msg").textContent = err.message;
        showScanStage("scan-error");
      }
    } finally {
      polling = false;
    }
  };

  state.timers.countdown = setInterval(pollCycle, 250);
  void pollCycle();
}

/* Two figures only: %BAC for alcohol, a 0-1 confidence score for cannabis.
   No pass/fail judgement is shown anywhere. */
const fmtBac = (r) => (Number(r.bac_percent) || 0).toFixed(3);
const fmtConfidence = (r) => (Number(r.confidence) || 0).toFixed(3);

function showResults(result) {
  showScanStage("scan-result");
  $("#val-alcohol").textContent = fmtBac(result);
  $("#val-cannabis").textContent = fmtConfidence(result);
  sndPass();
  if (result.baseline_stable === false) toast("BASELINE UNSTABLE — RESULT SUSPECT", true);
}

/* Browser-captured photo (canvas) takes priority; otherwise fall back to the
   photo the backend grabbed itself via ffmpeg during the scan (used on
   boards where getUserMedia can't reach the camera). */
function photoPreviewUrl() {
  if (state.photoData) return state.photoData;
  if (state.scan && state.scan.photoCaptured) {
    return `/photos/${state.scan.receipt_id}.jpg?t=${Date.now()}`;
  }
  return "";
}


/* ============================== form ============================== */

function openForm() {
  const scan = state.scan;
  const result = state.result;
  if (!scan || !result) return;

  const autoItems = [
    ["RECEIPT ID", scan.receipt_id], ["AREA", scan.area || "--"],
    ["VERSION", scan.version], ["SET NO", scan.set_no || "--"],
    ["COUNTER", String(scan.counter)], ["DATE", result.test_date],
    ["TIME", result.test_time], ["CALIBR DATE", scan.calibr_date || "--"],
    ["GPS 1", state.gpsFix.gps1 || "--"], ["GPS 2", state.gpsFix.gps2 || "--"],
    ["MODE", scan.testing_mode],
    ["BAC", `${fmtBac(result)} %`],
    ["CONFIDENCE", fmtConfidence(result)],
  ];
  $("#auto-grid").innerHTML = autoItems.map(([label, value, cls]) =>
    `<div class="auto-item"><span>${label}</span><b class="${cls || ""}">${value}</b></div>`).join("");

  const photo = $("#form-photo");
  const photoUrl = photoPreviewUrl();
  if (photoUrl) {
    photo.src = photoUrl;
    photo.classList.remove("hidden");
    $("#form-nophoto").classList.add("hidden");
  } else {
    photo.classList.add("hidden");
    $("#form-nophoto").classList.remove("hidden");
  }

  ["#f-name", "#f-dl", "#f-vehicle", "#f-mobile", "#f-location", "#f-address"].forEach((sel) => {
    $(sel).value = "";
    $(sel).classList.remove("invalid");
  });
  $("#f-officer").value = scan.officer || "";
  showScreen("form");
}

async function saveRecord() {
  const name = $("#f-name").value.trim();
  if (!name) {
    $("#f-name").classList.add("invalid");
    $("#f-name").focus();
    toast("NAME REQUIRED", true);
    return;
  }
  const scan = state.scan;
  const result = state.result;
  const button = $("#btn-form-save");
  button.disabled = true;
  try {
    const saved = await postJson("/api/records", {
      receipt_id: scan.receipt_id,
      area: scan.area, version: scan.version, set_no: scan.set_no,
      counter: scan.counter,
      test_date: result.test_date, test_time: result.test_time,
      calibr_date: scan.calibr_date,
      gps1: state.gpsFix.gps1, gps2: state.gpsFix.gps2,
      name,
      dl_number: $("#f-dl").value.trim(),
      vehicle_no: $("#f-vehicle").value.trim(),
      test_location: $("#f-location").value.trim(),
      testing_officer: $("#f-officer").value.trim(),
      testing_mode: scan.testing_mode,
      test_result: result.test_result,
      alcohol_bac: result.alcohol_bac, cannabis_ppb: result.cannabis_ppb,
      alcohol_baseline: result.alcohol_baseline || 0, alcohol_peak: result.alcohol_peak || 0,
      cannabis_baseline: result.cannabis_baseline || 0, cannabis_peak: result.cannabis_peak || 0,
      cannabis_ratio: result.cannabis_ratio || 0,
      cannabis_upper: result.cannabis_upper || 0, cannabis_lower: result.cannabis_lower || 0,
      curve_file: result.curve_file || "",
      bac_percent: result.bac_percent || 0, confidence: result.confidence || 0,
      alcohol_flag: result.alcohol_flag, cannabis_flag: result.cannabis_flag,
      mobile_no: $("#f-mobile").value.trim(),
      address: $("#f-address").value.trim(),
      photo_b64: state.photoData,
    });
    buildPrintReceipt(saved.receipt_id, name);
    state.savedId = saved.id;
    $("#saved-receipt").textContent = saved.receipt_id;
    stopCamera();
    showScreen("saved");
    sndPass();
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
  }
}

/* Print on the ESC/POS serial thermal printer (backend-driven; the kiosk
   has no browser-printable output). */
async function printReceipt(recordId, button) {
  if (!recordId) { toast("NOTHING TO PRINT", true); return; }
  const label = button ? button.textContent : "";
  if (button) { button.disabled = true; button.textContent = "PRINTING…"; }
  try {
    const res = await postJson("/api/print", { record_id: recordId });
    toast(res.message || "PRINTED");
    beep(1200, 60);
  } catch (err) {
    toast(err.message, true);
  } finally {
    if (button) { button.disabled = false; button.textContent = label; }
  }
}

function buildPrintReceipt(receiptId, name) {
  const scan = state.scan;
  const result = state.result;
  const rows = [
    ["Receipt", receiptId], ["Area", scan.area], ["Version", scan.version],
    ["Set No", scan.set_no], ["Counter", scan.counter],
    ["Date", result.test_date], ["Time", result.test_time],
    ["Calibr Date", scan.calibr_date],
    ["GPS", `${state.gpsFix.gps1 || "--"} / ${state.gpsFix.gps2 || "--"}`],
    ["Name", name], ["ID No", $("#f-dl").value.trim() || "--"],
    ["Location", $("#f-location").value.trim() || "--"],
    ["Officer", $("#f-officer").value.trim() || "--"],
    ["Mode", scan.testing_mode],
    ["BAC", `${fmtBac(result)} %`],
    ["Confidence Score", fmtConfidence(result)],
  ];
  const printPhotoUrl = photoPreviewUrl();
  $("#print-receipt").innerHTML = `
    <h3>BREATHCHECK</h3>
    <div class="pr-line"></div>
    ${printPhotoUrl ? `<img src="${printPhotoUrl}" alt="">` : ""}
    ${rows.map(([k, v]) => `<div class="pr-row"><span>${k}</span><span>${v}</span></div>`).join("")}
    <div class="pr-line"></div>`;
}

/* ============================== database ============================== */

async function loadRecords(query = "") {
  try {
    const data = await api(`/api/records?q=${encodeURIComponent(query)}`);
    const rows = data.records;
    $("#db-empty").classList.toggle("hidden", rows.length > 0);
    $("#db-body").innerHTML = rows.map((row) => `
      <tr data-id="${row.id}">
        <td>${escapeHtml(row.name) || "--"}</td>
        <td>${escapeHtml(row.dl_number) || "--"}</td>
        <td>${fmtBac(row)}</td>
        <td>${fmtConfidence(row)}</td>
      </tr>`).join("");
  } catch (err) {
    toast(err.message, true);
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function openRecordDetail(id) {
  try {
    const record = await api(`/api/records/${id}`);
    $("#modal-title").textContent = record.receipt_id;
    const fields = [
      ["NAME", record.name], ["ID NO", record.dl_number],
      ["VEHICLE", record.vehicle_no], ["MOBILE", record.mobile_no],
      ["DATE", record.test_date], ["TIME", record.test_time],
      ["AREA", record.area], ["SET NO", record.set_no],
      ["COUNTER", record.counter], ["VERSION", record.version],
      ["CALIBR DATE", record.calibr_date], ["MODE", record.testing_mode],
      ["GPS 1", record.gps1], ["GPS 2", record.gps2],
      ["LOCATION", record.test_location], ["OFFICER", record.testing_officer],
      ["BAC", `${fmtBac(record)} %`],
      ["CONFIDENCE", fmtConfidence(record)],
    ];
    $("#modal-body").innerHTML = `
      ${record.photo_url ? `<img class="modal-photo" src="${record.photo_url}" alt="">` : ""}
      <div class="detail-grid">
        ${fields.map(([label, value, cls]) =>
          `<div class="auto-item"><span>${label}</span><b class="${cls || ""}">${escapeHtml(value) || "--"}</b></div>`).join("")}
        <div class="auto-item detail-wide"><span>ADDRESS</span><b>${escapeHtml(record.address) || "--"}</b></div>
      </div>
      <div class="row-buttons modal-actions">
        <button id="modal-print" class="btn-primary">PRINT</button>
      </div>`;
    $("#modal-print").addEventListener("click", () =>
      printReceipt(record.id, $("#modal-print")));
    $("#modal").classList.remove("hidden");
  } catch (err) {
    toast(err.message, true);
  }
}

/* ============================== gps ============================== */

async function refreshGps() {
  try {
    const gps = await api("/api/gps");
    const fixEl = $("#gps-fix");
    fixEl.textContent = gps.fix ? "FIX OK" : "NO FIX";
    fixEl.classList.toggle("ok", !!gps.fix);
    $("#gps-lat").textContent = gps.lat != null ? gps.lat.toFixed(6) : "--";
    $("#gps-lon").textContent = gps.lon != null ? gps.lon.toFixed(6) : "--";
    $("#gps-sats").textContent = `${gps.sats || 0} SAT`;
    $("#gps-time").textContent = gps.updated_at || "--:--:--";
  } catch (err) { /* keep last values */ }
}

/* Screen dimming is applied from the saved brightness at boot; the
   settings screen that used to change it has been removed. */
function applyBrightness(percent) {
  $("#dim").style.opacity = String(((100 - percent) / 100) * 0.75);
}

/* Wipe every stored test. Double-confirmed: this cannot be undone. */
async function clearDatabase() {
  if (!confirm("Delete ALL records? This cannot be undone.")) return;
  if (!confirm("Are you sure? Every test record will be erased.")) return;
  try {
    const result = await api("/api/records?confirm=ERASE", { method: "DELETE" });
    toast(`CLEARED ${result.deleted}`);
    loadRecords($("#db-search").value.trim());
  } catch (err) {
    toast(err.message, true);
  }
}

function bindEvents() {
  document.querySelectorAll("[data-nav]").forEach((el) =>
    el.addEventListener("click", () => showScreen(el.dataset.nav)));
  $("#btn-back").addEventListener("click", goHome);

  $("#btn-start-scan").addEventListener("click", beginScan);
  $("#btn-rescan").addEventListener("click", enterScanReady);
  $("#btn-retry").addEventListener("click", enterScanReady);
  $("#btn-to-form").addEventListener("click", openForm);

  $("#btn-form-cancel").addEventListener("click", goHome);
  $("#btn-form-save").addEventListener("click", saveRecord);
  $("#f-name").addEventListener("input", () => $("#f-name").classList.remove("invalid"));

  $("#btn-done").addEventListener("click", goHome);
  $("#btn-print").addEventListener("click", () => printReceipt(state.savedId, $("#btn-print")));

  let searchTimer = null;
  $("#db-search").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadRecords(event.target.value.trim()), 250);
  });
  $("#db-body").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-id]");
    if (row) openRecordDetail(Number(row.dataset.id));
  });
  $("#btn-db-clear").addEventListener("click", clearDatabase);

  $("#modal-close").addEventListener("click", () => $("#modal").classList.add("hidden"));
  $("#modal").addEventListener("click", (event) => {
    if (event.target === $("#modal")) $("#modal").classList.add("hidden");
  });
}

/* ============================== on-screen keyboard ==============================
   The handheld has no physical keys, so every text/number field is filled from
   this panel. Keys never steal focus (pointerdown is prevented), and each press
   dispatches a real `input` event so existing field listeners still run. */

const KB_TEXT_ROWS = [
  ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
  ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
  ["a", "s", "d", "f", "g", "h", "j", "k", "l"],
  ["{shift}", "z", "x", "c", "v", "b", "n", "m", "{bksp}"],
  ["-", "/", "{space}", ".", ",", "{done}"],
];
const KB_NUM_ROWS = [
  ["1", "2", "3"],
  ["4", "5", "6"],
  ["7", "8", "9"],
  [".", "0", "{bksp}"],
  ["{clear}", "{done}"],
];
const KB_LABELS = {
  "{shift}": "SHIFT", "{bksp}": "⌫", "{space}": "SPACE",
  "{done}": "DONE", "{clear}": "CLEAR",
};

const kb = { el: null, input: null, mode: "text", shift: false };

/* Which inputs get the keyboard. Date/time/range keep their native pickers. */
function kbModeFor(el) {
  if (!el || el.tagName !== "INPUT" || el.readOnly || el.disabled) return null;
  const type = (el.getAttribute("type") || "text").toLowerCase();
  // Digit-only fields may declare themselves via type OR inputmode (the
  // mobile-number field uses inputmode="tel" with no type).
  const inputmode = (el.getAttribute("inputmode") || "").toLowerCase();
  if (type === "number" || type === "tel" ||
      ["numeric", "tel", "decimal"].includes(inputmode)) return "num";
  if (type === "text" || type === "search" || type === "") return "text";
  return null;
}

function kbRender() {
  const rows = kb.mode === "num" ? KB_NUM_ROWS : KB_TEXT_ROWS;
  kb.el.classList.toggle("num", kb.mode === "num");
  kb.el.innerHTML = rows.map((row) => `<div class="kb-row">` + row.map((key) => {
    const special = key.startsWith("{");
    const label = special ? KB_LABELS[key] : (kb.shift ? key.toUpperCase() : key);
    const cls = special ? ` kb-${key.slice(1, -1)}` : "";
    const on = key === "{shift}" && kb.shift ? " on" : "";
    return `<button type="button" class="kb-key${cls}${on}" data-key="${key}">${label}</button>`;
  }).join("") + `</div>`).join("");
}

function kbInsert(text) {
  const input = kb.input;
  if (!input) return;
  const value = input.value;
  const start = input.selectionStart ?? value.length;
  const end = input.selectionEnd ?? value.length;
  input.value = value.slice(0, start) + text + value.slice(end);
  const caret = start + text.length;
  try { input.setSelectionRange(caret, caret); } catch (err) { /* number inputs */ }
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function kbBackspace() {
  const input = kb.input;
  if (!input) return;
  const value = input.value;
  const start = input.selectionStart ?? value.length;
  const end = input.selectionEnd ?? value.length;
  let caret = start;
  if (start !== end) {
    input.value = value.slice(0, start) + value.slice(end);
  } else if (start > 0) {
    input.value = value.slice(0, start - 1) + value.slice(start);
    caret = start - 1;
  }
  try { input.setSelectionRange(caret, caret); } catch (err) { /* number inputs */ }
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function kbKey(key) {
  if (!kb.input) return;
  switch (key) {
    case "{shift}": kb.shift = !kb.shift; kbRender(); return;
    case "{bksp}": kbBackspace(); return;
    case "{space}": kbInsert(" "); return;
    case "{clear}":
      kb.input.value = "";
      kb.input.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    case "{done}": {
      // Commit and close directly — do not rely on the blur event arriving.
      const input = kb.input;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      kbClose();
      input.blur();
      return;
    }
    default:
      kbInsert(kb.shift ? key.toUpperCase() : key);
      if (kb.shift) { kb.shift = false; kbRender(); }
  }
}

function kbOpen(input) {
  const mode = kbModeFor(input);
  if (!mode) return;
  kb.input = input;
  kb.mode = mode;
  kb.shift = false;
  kbRender();
  kb.el.classList.remove("hidden");
  document.body.classList.add("kb-open");
  // The panel covers the lower screen — bring the field back into view.
  setTimeout(() => input.scrollIntoView({ block: "center", behavior: "smooth" }), 60);
}

function kbClose() {
  kb.input = null;
  kb.el.classList.add("hidden");
  document.body.classList.remove("kb-open");
}

function initKeyboard() {
  kb.el = $("#keyboard");
  // Keep focus on the field while typing.
  kb.el.addEventListener("pointerdown", (e) => e.preventDefault());
  kb.el.addEventListener("mousedown", (e) => e.preventDefault());
  kb.el.addEventListener("click", (e) => {
    const key = e.target.closest(".kb-key");
    if (key) { kbKey(key.dataset.key); beep(1200, 25, "sine", 0.03); }
  });

  document.addEventListener("focusin", (e) => {
    if (kbModeFor(e.target)) kbOpen(e.target);
    else if (kb.input) kbClose();   // moved to a date picker, button, etc.
  });
  document.addEventListener("focusout", (e) => {
    if (!kbModeFor(e.target)) return;
    // Fields edited via the keyboard need an explicit change event, since the
    // browser only fires one for real user input.
    e.target.dispatchEvent(new Event("change", { bubbles: true }));
    const left = e.target;
    setTimeout(() => {
      if (kb.input === left && document.activeElement !== left) kbClose();
    }, 0);
  });
}

/* Kiosk touchscreen: block every zoom gesture. touch-action in CSS handles
   most of it; these cover pinch (multi-touch) and mouse/keyboard zoom too. */
function blockZoom() {
  document.addEventListener("touchmove", (e) => {
    if (e.touches.length > 1) e.preventDefault();   // pinch — single-finger scroll untouched
  }, { passive: false });
  ["gesturestart", "gesturechange", "gestureend"].forEach((type) =>
    document.addEventListener(type, (e) => e.preventDefault()));
  document.addEventListener("wheel", (e) => { if (e.ctrlKey) e.preventDefault(); }, { passive: false });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && ["+", "-", "=", "0"].includes(e.key)) e.preventDefault();
  });
}

async function boot() {
  // Before anything is drawn, so an upside-down screen never flashes the
  // right way up first.
  try {
    const status = await api("/api/status");
    document.body.classList.toggle("inverted", status.screen_invert !== false);
  } catch (err) { /* leave unrotated if the backend is not up yet */ }

  blockZoom();
  initKeyboard();
  bindEvents();
  tickClock();
  setInterval(tickClock, 1000);
  try {
    state.settings = await api("/api/settings");
    applyBrightness(state.settings.brightness);
  } catch (err) { /* retried on next screen change */ }
  refreshStatus();
  setInterval(() => { if (state.screen === "home") refreshStatus(); }, 15000);
}

boot();
