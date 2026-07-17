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
  database: "DATABASE", gps: "GPS", settings: "SETTINGS",
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
  if (name === "settings") loadSettingsScreen();
}

function goHome() {
  cancelCountdown();
  showScreen("home");
}

/* ============================== clock ============================== */

function tickClock() {
  const now = new Date();
  const hhmm = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  $("#top-clock").textContent = hhmm;
  $("#home-time").textContent = hhmm;
  $("#home-date").textContent = now.toLocaleDateString([], {
    weekday: "long", day: "2-digit", month: "short", year: "numeric",
  });
  if (state.screen === "settings") {
    $("#set-now").textContent = now.toLocaleString([], {
      day: "2-digit", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    });
  }
}

/* ============================== home status ============================== */

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    $("#chip-set").textContent = `SET ${status.set_no || "--"}`;
    $("#chip-records").textContent = `${status.records} RECORD${status.records === 1 ? "" : "S"}`;
    $("#chip-sensor").textContent =
      status.sensor_state === "stabilizing" ? "SENSOR WARM-UP"
        : status.analyzer === "spi" ? "SENSOR LIVE" : "SENSOR DEMO";
  } catch (err) { /* backend not ready yet */ }
}

/* ============================== camera ============================== */

async function startCamera() {
  if (state.stream) return true;
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
      audio: false,
    });
    $("#cam").srcObject = state.stream;
    $("#cam2").srcObject = state.stream;
    $("#cam-off").classList.add("hidden");
    return true;
  } catch (err) {
    $("#cam-off").classList.remove("hidden");
    return false;
  }
}

function stopCamera() {
  if (state.stream) {
    state.stream.getTracks().forEach((track) => track.stop());
    state.stream = null;
    $("#cam").srcObject = null;
    $("#cam2").srcObject = null;
  }
}

function capturePhoto() {
  const video = $("#cam2");
  if (!state.stream || !video.videoWidth) return "";
  const canvas = $("#snap");
  const width = Math.min(640, video.videoWidth);
  const height = Math.round(width * video.videoHeight / video.videoWidth);
  canvas.width = width;
  canvas.height = height;
  canvas.getContext("2d").drawImage(video, 0, 0, width, height);
  return canvas.toDataURL("image/jpeg", 0.82);
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
}

function cancelCountdown() {
  clearInterval(state.timers.countdown);
  clearInterval(state.timers.poll);
  state.timers.countdown = null;
  state.timers.poll = null;
}

async function beginScan() {
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
  }
}

/* Measurement cycle (driven by the backend / sensor board):
   purge -> baseline (amber ring, WAIT) -> measure (cyan ring, BLOW). */
const PHASE_LABEL = { purge: "WAIT", baseline: "WAIT", measure: "BLOW" };

function trackCycle(session) {
  showScanStage("scan-run");
  $("#photo-tag").classList.add("hidden");
  state.photoData = "";

  const ring = $("#ring-fg");
  const circumference = 2 * Math.PI * 96;
  let lastPhase = "";
  let lastCount = -1;
  let photoTaken = false;
  let polling = false;
  sndTick();

  state.timers.countdown = setInterval(async () => {
    if (polling) return;
    polling = true;
    try {
      const status = await api(`/api/scan/${session.session_id}`);
      if (status.status === "done") {
        cancelCountdown();
        beep(660, 400);
        state.result = status.result;
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

      if (phase !== lastPhase) {
        lastPhase = phase;
        lastCount = -1;
        $("#ring-label").textContent = PHASE_LABEL[phase] || "WAIT";
        ring.classList.toggle("prep", !isMeasure);
        $(".ring-wrap").classList.toggle("prep", !isMeasure);
        beep(isMeasure ? 990 : 660, 120);
      }
      ring.style.strokeDashoffset = String(circumference * Math.min(1, elapsed / total));
      const count = Math.max(0, Math.ceil(remaining));
      if (count !== lastCount) {
        lastCount = count;
        $("#ring-count").textContent = String(count);
        if (isMeasure && count > 0) sndTick();
      }

      if (isMeasure && !photoTaken && elapsed >= session.photo_second) {
        photoTaken = true;
        state.photoData = capturePhoto();
        if (state.photoData) {
          $("#flash").classList.remove("go");
          void $("#flash").offsetWidth;
          $("#flash").classList.add("go");
          $("#photo-tag").classList.remove("hidden");
          sndShutter();
        }
      }
    } catch (err) {
      cancelCountdown();
      $("#scan-error-msg").textContent = err.message;
      showScanStage("scan-error");
    } finally {
      polling = false;
    }
  }, 250);
}

function showResults(result) {
  showScanStage("scan-result");
  const verdict = $("#verdict");
  verdict.textContent = result.test_result;
  verdict.className = `verdict ${result.test_result === "PASS" ? "good" : "bad"}`;

  $("#val-alcohol").textContent = fmtVal(result.alcohol_bac);
  $("#val-cannabis").textContent = fmtVal(result.cannabis_ppb);
  $("#sub-alcohol").textContent = `PEAK ${fmtVal(result.alcohol_peak || 0)} µA`;
  $("#sub-cannabis").textContent = `PEAK ${fmtVal(result.cannabis_peak || 0)} mV`;
  setResultCard("#card-alcohol", "#flag-alcohol", result.alcohol_flag);
  setResultCard("#card-cannabis", "#flag-cannabis", result.cannabis_flag);
  result.test_result === "PASS" ? sndPass() : sndFail();
  if (result.baseline_stable === false) toast("BASELINE UNSTABLE — RESULT SUSPECT", true);
}

function setResultCard(cardSel, flagSel, flag) {
  const failed = flag === "YES";
  $(cardSel).classList.toggle("fail", failed);
  $(cardSel).classList.toggle("pass", !failed);
  $(flagSel).textContent = failed ? "FAIL" : "PASS";
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
    ["RESULT", result.test_result, result.test_result === "PASS" ? "good" : "bad"],
    ["ALCOHOL", `${fmtVal(result.alcohol_bac)} mV·s`, result.alcohol_flag === "YES" ? "bad" : "good"],
    ["CANNABIS", `${fmtVal(result.cannabis_ppb)} mV·s`, result.cannabis_flag === "YES" ? "bad" : "good"],
  ];
  $("#auto-grid").innerHTML = autoItems.map(([label, value, cls]) =>
    `<div class="auto-item"><span>${label}</span><b class="${cls || ""}">${value}</b></div>`).join("");

  const photo = $("#form-photo");
  if (state.photoData) {
    photo.src = state.photoData;
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
      alcohol_flag: result.alcohol_flag, cannabis_flag: result.cannabis_flag,
      mobile_no: $("#f-mobile").value.trim(),
      address: $("#f-address").value.trim(),
      photo_b64: state.photoData,
    });
    buildPrintReceipt(saved.receipt_id, name);
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

function buildPrintReceipt(receiptId, name) {
  const scan = state.scan;
  const result = state.result;
  const rows = [
    ["Receipt", receiptId], ["Area", scan.area], ["Version", scan.version],
    ["Set No", scan.set_no], ["Counter", scan.counter],
    ["Date", result.test_date], ["Time", result.test_time],
    ["Calibr Date", scan.calibr_date],
    ["GPS", `${state.gpsFix.gps1 || "--"} / ${state.gpsFix.gps2 || "--"}`],
    ["Name", name], ["DL No", $("#f-dl").value.trim() || "--"],
    ["Vehicle", $("#f-vehicle").value.trim() || "--"],
    ["Location", $("#f-location").value.trim() || "--"],
    ["Officer", $("#f-officer").value.trim() || "--"],
    ["Mode", scan.testing_mode],
    ["Alcohol", `${fmtVal(result.alcohol_bac)} mV.s [${result.alcohol_flag === "YES" ? "FAIL" : "PASS"}]`],
    ["Cannabis", `${fmtVal(result.cannabis_ppb)} mV.s [${result.cannabis_flag === "YES" ? "FAIL" : "PASS"}]`],
  ];
  $("#print-receipt").innerHTML = `
    <h3>BREATHCHECK</h3>
    <div class="pr-line"></div>
    ${state.photoData ? `<img src="${state.photoData}" alt="">` : ""}
    ${rows.map(([k, v]) => `<div class="pr-row"><span>${k}</span><span>${v}</span></div>`).join("")}
    <div class="pr-line"></div>
    <div class="pr-big">${result.test_result}</div>
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
        <td><span class="badge ${row.alcohol_flag === "YES" ? "yes" : "no"}">${row.alcohol_flag}</span></td>
        <td><span class="badge ${row.cannabis_flag === "YES" ? "yes" : "no"}">${row.cannabis_flag}</span></td>
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
      ["NAME", record.name], ["DL NO", record.dl_number],
      ["VEHICLE", record.vehicle_no], ["MOBILE", record.mobile_no],
      ["DATE", record.test_date], ["TIME", record.test_time],
      ["AREA", record.area], ["SET NO", record.set_no],
      ["COUNTER", record.counter], ["VERSION", record.version],
      ["CALIBR DATE", record.calibr_date], ["MODE", record.testing_mode],
      ["GPS 1", record.gps1], ["GPS 2", record.gps2],
      ["LOCATION", record.test_location], ["OFFICER", record.testing_officer],
      ["ALCOHOL", `${fmtVal(record.alcohol_bac)} mV·s`, record.alcohol_flag === "YES" ? "bad" : "good"],
      ["CANNABIS", `${fmtVal(record.cannabis_ppb)} mV·s`, record.cannabis_flag === "YES" ? "bad" : "good"],
      ["ALC PEAK", `${fmtVal(record.alcohol_peak || 0)} µA`],
      ["CAN PEAK", `${fmtVal(record.cannabis_peak || 0)} mV`],
      ["RESULT", record.test_result, record.test_result === "PASS" ? "good" : "bad"],
    ];
    $("#modal-body").innerHTML = `
      ${record.photo_url ? `<img class="modal-photo" src="${record.photo_url}" alt="">` : ""}
      <div class="detail-grid">
        ${fields.map(([label, value, cls]) =>
          `<div class="auto-item"><span>${label}</span><b class="${cls || ""}">${escapeHtml(value) || "--"}</b></div>`).join("")}
        <div class="auto-item detail-wide"><span>ADDRESS</span><b>${escapeHtml(record.address) || "--"}</b></div>
      </div>`;
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

/* ============================== settings ============================== */

function applyBrightness(percent) {
  $("#dim").style.opacity = String(((100 - percent) / 100) * 0.75);
}

async function loadSettingsScreen() {
  try {
    const settings = await api("/api/settings");
    state.settings = settings;
    $("#set-brightness").value = settings.brightness;
    $("#set-brightness-val").textContent = `${settings.brightness}%`;
    $("#set-sound").textContent = settings.sound ? "ON" : "OFF";
    $("#set-sound").classList.toggle("on", settings.sound);
    $("#set-alimit").value = settings.alcohol_limit;
    $("#set-climit").value = settings.cannabis_limit;
    $("#set-scansec").value = settings.scan_seconds;
    $("#set-photosec").value = settings.photo_second;
    $("#set-mode").textContent = settings.testing_mode;
    $("#set-mode").classList.toggle("on", settings.testing_mode === "ACTIVE");
    $("#set-officer").value = settings.officer;
    $("#set-area").value = settings.area;
    $("#set-setno").value = settings.set_no;
    $("#set-calibr").value = settings.calibr_date;
    $("#set-version").textContent = settings.version;
    $("#set-counter").textContent = settings.counter;
    $("#set-sensor").textContent = settings.analyzer.toUpperCase();
    $("#set-gps").textContent = settings.gps_mode.toUpperCase();
    applyBrightness(settings.brightness);
    const status = await api("/api/status");
    $("#set-records").textContent = status.records;
  } catch (err) {
    toast(err.message, true);
  }
}

let saveTimer = null;
let pendingPatch = {};
function pushSettings(patch, quiet = false) {
  Object.assign(pendingPatch, patch);
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const payload = pendingPatch;
    pendingPatch = {};
    try {
      state.settings = await postJson("/api/settings", payload, "PUT");
      if (!quiet) toast("SAVED");
    } catch (err) {
      toast(err.message, true);
    }
  }, 350);
}

function bindSettings() {
  $("#set-brightness").addEventListener("input", (event) => {
    const value = Number(event.target.value);
    $("#set-brightness-val").textContent = `${value}%`;
    applyBrightness(value);
    pushSettings({ brightness: value }, true);
  });
  $("#set-sound").addEventListener("click", () => {
    const on = !$("#set-sound").classList.contains("on");
    $("#set-sound").textContent = on ? "ON" : "OFF";
    $("#set-sound").classList.toggle("on", on);
    if (state.settings) state.settings.sound = on;
    pushSettings({ sound: on });
    if (on) beep(880, 80);
  });
  $("#set-mode").addEventListener("click", () => {
    const next = $("#set-mode").textContent === "ACTIVE" ? "PASSIVE" : "ACTIVE";
    $("#set-mode").textContent = next;
    $("#set-mode").classList.toggle("on", next === "ACTIVE");
    pushSettings({ testing_mode: next });
  });

  const numberFields = [
    ["#set-alimit", "alcohol_limit"], ["#set-climit", "cannabis_limit"],
    ["#set-scansec", "scan_seconds"], ["#set-photosec", "photo_second"],
  ];
  numberFields.forEach(([sel, key]) => {
    $(sel).addEventListener("change", (event) => {
      const value = Number(event.target.value);
      if (Number.isFinite(value)) pushSettings({ [key]: value });
    });
  });
  const textFields = [
    ["#set-officer", "officer"], ["#set-area", "area"],
    ["#set-setno", "set_no"], ["#set-calibr", "calibr_date"],
  ];
  textFields.forEach(([sel, key]) => {
    $(sel).addEventListener("change", (event) => pushSettings({ [key]: event.target.value }));
  });

  $("#btn-set-time").addEventListener("click", async () => {
    const value = $("#set-datetime").value;
    if (!value) { toast("PICK DATE & TIME", true); return; }
    try {
      const result = await postJson("/api/time", { datetime: value.replace("T", " ") + ":00" });
      toast(result.message, !result.ok);
    } catch (err) {
      toast(err.message, true);
    }
  });

  $("#btn-export").addEventListener("click", () => { window.location.href = "/api/export.csv"; });

  $("#btn-erase").addEventListener("click", async () => {
    if (!confirm("Erase ALL records? This cannot be undone.")) return;
    if (!confirm("Are you sure? Every test record will be deleted.")) return;
    try {
      const result = await api("/api/records?confirm=ERASE", { method: "DELETE" });
      toast(`ERASED ${result.deleted}`);
      loadSettingsScreen();
    } catch (err) {
      toast(err.message, true);
    }
  });
}

/* ============================== boot ============================== */

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
  $("#btn-print").addEventListener("click", () => window.print());

  let searchTimer = null;
  $("#db-search").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadRecords(event.target.value.trim()), 250);
  });
  $("#db-body").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-id]");
    if (row) openRecordDetail(Number(row.dataset.id));
  });

  $("#modal-close").addEventListener("click", () => $("#modal").classList.add("hidden"));
  $("#modal").addEventListener("click", (event) => {
    if (event.target === $("#modal")) $("#modal").classList.add("hidden");
  });

  bindSettings();
}

async function boot() {
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
