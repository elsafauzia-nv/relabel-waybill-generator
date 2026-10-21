"use strict";

// Talks only to the local agent (agent.py) on this computer. Nothing is sent anywhere else.
const DEFAULT_AGENT = "http://127.0.0.1:8765";
const TID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{4,63}$/; // same rule as the agent
const STAGE_LABELS = {
  agent: "Local agent",
  browser: "Authenticated browser",
  submit: "Submitting tracking ID",
  scan: "Scan result",
  event: "Matching print event",
  generate: "Generating ZPL",
  validate: "Validating ZPL",
  render: "Rendering waybill",
  save: "Saving files",
};
const ICONS = { pending: "○", running: "●", ok: "✓", warn: "!", failed: "✕" };

let countries = { PH: ["JNT", "FLASH"], MY: ["JNT"] };
let agentOk = false;
let polling = null;
let current = null; // {job, zpl, previewLoaded}

const $ = (id) => document.getElementById(id);

function storage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (_) { /* storage blocked: fall back to defaults */ }
  return null;
}

function agentUrl() {
  const fromQuery = new URLSearchParams(location.search).get("agent");
  return (fromQuery || storage("waybillAgent") || DEFAULT_AGENT).replace(/\/+$/, "");
}

async function api(path, options = {}) {
  const response = await fetch(agentUrl() + path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    cache: "no-store",
  });
  let data = null;
  if ((response.headers.get("Content-Type") || "").includes("application/json")) data = await response.json();
  return { response, data };
}

// ---------------------------------------------------------------- agent status

async function checkAgent() {
  setPill("wait", "Checking local agent…");
  $("agentLabel").textContent = agentUrl();
  try {
    const { response, data } = await api("/api/health");
    if (!response.ok || !data || !data.ok) throw new Error(data && data.error ? data.error : `HTTP ${response.status}`);
    countries = data.countries || countries;
    agentOk = true;
    setPill("ok", data.busy ? "Local agent connected (busy)" : "Local agent connected");
    $("agentHelp").hidden = true;
  } catch (error) {
    agentOk = false;
    setPill("fail", "Local agent unavailable");
    $("agentHelp").hidden = false;
  }
  updateVendors();
  updateStart();
}

function setPill(kind, text) {
  $("agentPill").className = `pill pill-${kind}`;
  $("agentText").textContent = text;
}

// ---------------------------------------------------------------- form

function updateVendors() {
  const country = $("country").value;
  const vendors = countries[country] || [];
  const previous = $("vendor").value;
  $("vendor").replaceChildren(...vendors.map((v) => new Option(v, v)));
  if (vendors.includes(previous)) $("vendor").value = previous;
  $("vendor").disabled = vendors.length <= 1;
}

function formInputs() {
  return {
    country: $("country").value,
    vendor: $("vendor").value,
    tracking_id: $("tid").value.trim(),
    no_print: $("noPrint").checked,
  };
}

function validate(inputs) {
  if (!(countries[inputs.country] || []).includes(inputs.vendor)) {
    return `${inputs.vendor} is not available for ${inputs.country}.`;
  }
  if (!TID_PATTERN.test(inputs.tracking_id)) {
    return "Enter a tracking ID: 5-64 letters, digits, '-' or '_', no spaces.";
  }
  return null;
}

function updateStart() {
  $("start").disabled = !agentOk || polling !== null;
}

function showFormError(message) {
  $("formError").textContent = message || "";
  $("formError").hidden = !message;
}

async function start(extra = {}) {
  const inputs = formInputs();
  const problem = validate(inputs);
  showFormError(problem);
  if (problem) return;
  $("dupPanel").hidden = true;
  resetResults();
  setBanner("run", "Starting…");
  renderStages(null);
  const { response, data } = await api("/api/jobs", { method: "POST", body: JSON.stringify({ ...inputs, ...extra }) })
    .catch(() => ({ response: { ok: false, status: 0 }, data: null }));
  if (response.status === 202 && data && data.job_id) {
    current = { inputs: { ...inputs, tracking_id: extra.replace_with || inputs.tracking_id }, zpl: "", previewLoaded: false };
    poll(data.job_id);
    return;
  }
  if (response.status === 0) {
    setBanner("fail", "Local agent unavailable", "Start ./agent.py on this computer and click the status pill.");
    checkAgent();
  } else if (response.status === 409 && data && data.error === "duplicate") {
    showDuplicate(data);
    setBanner("idle", "This tracking ID already has a waybill - choose what to do.");
  } else if (response.status === 409 && data && data.error === "busy") {
    setBanner("fail", "The agent is already generating another waybill", "Wait for it to finish, then start again.");
  } else {
    setBanner("fail", "Not started", data && data.error ? data.error : `HTTP ${response.status}`);
  }
}

function showDuplicate(data) {
  $("dupTitle").textContent = `${data.tracking_id} already has a waybill (${data.folder}/).`;
  $("dupFiles").textContent = data.files && data.files.length ? `Files: ${data.files.join(", ")}` : "";
  $("dupNewTid").value = "";
  $("dupPanel").hidden = false;
}

// ---------------------------------------------------------------- job progress

function poll(jobId) {
  polling = true;
  updateStart();
  setFormDisabled(true);
  const tick = async () => {
    let job;
    try {
      const { response, data } = await api(`/api/jobs/${jobId}`);
      if (!response.ok) throw new Error(data && data.error ? data.error : `HTTP ${response.status}`);
      job = data;
    } catch (error) {
      finish();
      setBanner("fail", "Lost contact with the local agent", String(error.message || error));
      checkAgent();
      return;
    }
    render(job);
    if (job.state === "running") {
      polling = setTimeout(tick, 800);
    } else {
      finish();
    }
  };
  tick();
}

function finish() {
  if (typeof polling === "number") clearTimeout(polling);
  polling = null;
  setFormDisabled(false);
  updateStart();
}

function setFormDisabled(disabled) {
  for (const id of ["country", "tid", "noPrint"]) $(id).disabled = disabled;
  $("vendor").disabled = disabled || (countries[$("country").value] || []).length <= 1;
}

function render(job) {
  renderStages(job.stages);
  $("log").textContent = job.log.map((l) => (l.kind === "step" ? `\n${l.text}` : `${l.kind.toUpperCase().padEnd(5)} ${l.text}`)).join("\n").trim();
  const result = job.result || {};

  if (result.zpl && result.zpl !== current.zpl) {
    current.zpl = result.zpl;
    $("zpl").value = result.zpl;
    $("copyZpl").disabled = false;
    $("downloadZpl").disabled = false;
  }
  renderChecks(result.field_checks || []);
  const files = result.files || {};
  if (files.png && !current.previewLoaded) {
    current.previewLoaded = true;
    loadPreview(files.png);
  }
  $("downloadPng").disabled = !files.png;
  $("downloadPdf").disabled = !files.pdf;
  current.files = files;
  current.name = `${job.country}_${job.vendor}_${job.tracking_id}`;

  if (job.state === "running") {
    const runningStage = job.stages.find((s) => s.state === "running");
    setBanner("run", runningStage ? `${STAGE_LABELS[runningStage.name]}…` : "Working…", runningStage ? runningStage.detail : "");
  } else if (job.state === "done") {
    const warned = job.stages.some((s) => s.state === "warn");
    setBanner("ok", `Generation completed${warned ? " (with warnings)" : ""}`,
      `${job.tracking_id} → ${result.folder || ""}/${result.duplicate ? " · " + result.duplicate : ""}`);
  } else {
    const failedStage = job.stages.find((s) => s.state === "failed");
    setBanner("fail", `Generation failed${failedStage ? " at: " + STAGE_LABELS[failedStage.name] : ""}`,
      [result.error, result.hint ? "→ " + result.hint : ""].filter(Boolean).join(" ") || (failedStage && failedStage.detail) || "");
    if (!files.png && current.zpl) {
      setPreviewMessage("No waybill: the ZPL was generated but rendering or verification did not complete.", true);
    } else if (!current.zpl) {
      setPreviewMessage("No waybill was generated.", true);
    }
  }
}

function renderStages(stages) {
  const list = stages || Object.keys(STAGE_LABELS).map((name) => ({ name, state: "pending", detail: "" }));
  $("stages").replaceChildren(...list.map((s) => {
    const li = document.createElement("li");
    li.className = `st-${s.state}`;
    const icon = document.createElement("span");
    icon.className = "icon";
    icon.textContent = ICONS[s.state] || "○";
    icon.setAttribute("aria-label", s.state);
    const body = document.createElement("div");
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = STAGE_LABELS[s.name] || s.name;
    const detail = document.createElement("div");
    detail.className = "detail";
    detail.textContent = s.detail || "";
    body.append(name, detail);
    li.append(icon, body);
    return li;
  }));
}

function renderChecks(checks) {
  $("checks").replaceChildren(...checks.map((c) => {
    const li = document.createElement("li");
    li.className = c.status;
    li.textContent = `${ICONS[c.status === "failed" ? "failed" : c.status] || "•"} ${c.field.replace(/_/g, " ")}: ${c.detail}`;
    return li;
  }));
}

function setBanner(kind, title, detail) {
  const banner = $("banner");
  banner.className = `banner banner-${kind}`;
  banner.textContent = title;
  if (detail) {
    const small = document.createElement("small");
    small.textContent = detail;
    banner.append(small);
  }
}

// ---------------------------------------------------------------- results

function resetResults() {
  current = null;
  $("zpl").value = "";
  $("checks").replaceChildren();
  for (const id of ["copyZpl", "downloadZpl", "downloadPng", "downloadPdf"]) $(id).disabled = true;
  setPreviewMessage("The waybill rendered from the ZPL appears here.");
}

function setPreviewMessage(text, isError = false) {
  const p = document.createElement("p");
  p.className = isError ? "error" : "muted";
  p.textContent = text;
  $("preview").replaceChildren(p);
}

async function fetchBlob(path) {
  const response = await fetch(agentUrl() + path, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.blob();
}

async function loadPreview(path) {
  try {
    const blob = await fetchBlob(path);
    const img = document.createElement("img");
    img.alt = `Waybill ${current ? current.name : ""}`;
    img.src = URL.createObjectURL(blob);
    $("preview").replaceChildren(img);
  } catch (error) {
    setPreviewMessage(`The waybill image could not be loaded from the agent (${error.message}).`, true);
  }
}

function saveBlob(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

async function downloadFile(kind) {
  if (!current || !current.files || !current.files[kind]) return;
  try {
    saveBlob(await fetchBlob(current.files[kind]), `${current.name}.${kind}`);
  } catch (error) {
    setBanner("fail", `Download failed`, error.message);
  }
}

// ---------------------------------------------------------------- wiring

$("agentPill").addEventListener("click", checkAgent);
$("agentSave").addEventListener("click", () => {
  const url = $("agentUrl").value.trim();
  if (/^http:\/\/(127\.0\.0\.1|localhost):\d{2,5}$/.test(url)) {
    storage("waybillAgent", url);
    checkAgent();
  } else {
    $("agentUrl").setCustomValidity("Use http://127.0.0.1:<port> or http://localhost:<port>");
    $("agentUrl").reportValidity();
  }
});
$("agentUrl").addEventListener("input", () => $("agentUrl").setCustomValidity(""));
$("country").addEventListener("change", () => { updateVendors(); showFormError(null); });
$("tid").addEventListener("input", () => showFormError(null));
$("form").addEventListener("submit", (event) => { event.preventDefault(); start(); });
$("dupRegenerate").addEventListener("click", () => start({ duplicate: "regenerate" }));
$("dupReplace").addEventListener("click", () => {
  const newTid = $("dupNewTid").value.trim();
  if (!TID_PATTERN.test(newTid)) { $("dupNewTid").focus(); setBanner("fail", "Enter a valid new tracking ID"); return; }
  start({ duplicate: "replace", replace_with: newTid });
});
$("dupCancel").addEventListener("click", () => { $("dupPanel").hidden = true; setBanner("idle", "Cancelled."); });
$("copyZpl").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("zpl").value);
    $("copyZpl").textContent = "Copied";
  } catch (_) {
    $("zpl").select();
    document.execCommand("copy");
    $("copyZpl").textContent = "Copied";
  }
  setTimeout(() => { $("copyZpl").textContent = "Copy ZPL"; }, 1500);
});
$("downloadZpl").addEventListener("click", () => {
  if (!current) return;
  saveBlob(new Blob([$("zpl").value], { type: "text/plain;charset=utf-8" }), `${current.name}.zpl`);
});
$("downloadPng").addEventListener("click", () => downloadFile("png"));
$("downloadPdf").addEventListener("click", () => downloadFile("pdf"));

$("agentUrl").value = agentUrl();
renderStages(null);
updateVendors();
checkAgent();
