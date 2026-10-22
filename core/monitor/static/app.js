const state = {
  runs: [], overview: {}, selected: null, detail: null,
  offset: 0, limit: 100, requestVersion: 0, eventVersion: 0,
  searchTimer: null, activeEvidence: null,
  tab: "offline", online: null, pollTimer: null,
  onlineSignature: "", openSteps: new Set(), closedSteps: new Set(), openObjects: new Set(),
};

const ids = [
  "sync-state", "theme-button", "refresh-runs", "visible-run-count", "dataset-summary",
  "run-search", "run-status-filter", "task-filter", "framework-filter", "model-filter", "run-list",
  "empty-state", "empty-title", "empty-message", "run-view", "run-kicker", "run-title", "run-meta",
  "run-status", "health", "trace-search", "agent-filter", "type-filter", "tool-filter", "result-count",
  "active-evidence", "trace", "previous-page", "next-page", "page-label", "insight-panel",
  "detection-count", "detections-content", "tab-offline", "tab-online", "online-count", "online-content",
];
const nodes = Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));
const unique = (values) => [...new Set(values.filter(Boolean))].sort((a, b) => String(a).localeCompare(String(b)));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const runKey = (run) => run.key || run.id;

function element(tag, className = "", text = null) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== null) value.textContent = String(text);
  return value;
}

async function request(path) {
  const response = await fetch(path, {cache: "no-store"});
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
  return value;
}

function setSync(mode, text) {
  nodes["sync-state"].className = `sync-state ${mode}`.trim();
  nodes["sync-state"].querySelector("span").textContent = text;
}

async function loadRuns(manual = false) {
  nodes["refresh-runs"].disabled = true;
  setSync("", manual ? "Refreshing runs" : "Loading runs");
  try {
    const stamp = Date.now();
    const [runData, overview] = await Promise.all([
      request(`/api/runs?t=${stamp}`), request(`/api/summary?t=${stamp}`),
    ]);
    state.runs = Array.isArray(runData.runs) ? runData.runs : [];
    state.overview = overview || {};
    initializeRunFilters(); renderDatasetSummary(); renderRunList();
    const requested = decodeURIComponent(location.hash.replace(/^#/, ""));
    const selectedKey = state.runs.some((run) => runKey(run) === requested) ? requested
      : state.runs.some((run) => runKey(run) === state.selected) ? state.selected : runKey(state.runs[0] || {});
    if (selectedKey) await selectRun(selectedKey, {preserveFilters: manual && selectedKey === state.selected});
    else showEmpty("No collected runs yet", "The Monitor will show runs as soon as they are collected.");
    setSync("ready", `Synced · ${plural(state.runs.length, "run")}`);
  } catch (error) { showFatal(error.message); }
  finally { nodes["refresh-runs"].disabled = false; }
}

function initializeRunFilters() {
  fillSelect(nodes["task-filter"], "All tasks", unique(state.runs.map((run) => run.task_id)), nodes["task-filter"].value);
  fillSelect(nodes["framework-filter"], "All frameworks", unique(state.runs.map((run) => run.framework || run.adapter)), nodes["framework-filter"].value);
  fillSelect(nodes["model-filter"], "All models", unique(state.runs.map((run) => run.model)), nodes["model-filter"].value);
}

function fillSelect(select, emptyLabel, values, selected = "") {
  const options = [new Option(emptyLabel, ""), ...values.map((value) => new Option(value, value))];
  select.replaceChildren(...options); select.value = values.includes(selected) ? selected : "";
}

function renderDatasetSummary() {
  const values = [
    ["Runs", state.overview.runs || 0],
    ["Completed", state.overview.statuses?.completed || 0],
    ["Partial", state.overview.statuses?.partial || 0],
    ["Records", compactNumber(state.overview.records || 0)],
    ["Detected", state.overview.detected || 0],
  ];
  nodes["dataset-summary"].replaceChildren(...values.map(([label, value]) => {
    const item = element("div", "dataset-stat");
    item.append(element("strong", "", value), element("span", "", label));
    return item;
  }));
}

function filteredRuns() {
  const query = nodes["run-search"].value.trim().toLocaleLowerCase();
  return state.runs.filter((run) => {
    if (nodes["run-status-filter"].value && run.status !== nodes["run-status-filter"].value) return false;
    if (nodes["task-filter"].value && run.task_id !== nodes["task-filter"].value) return false;
    if (nodes["framework-filter"].value && (run.framework || run.adapter) !== nodes["framework-filter"].value) return false;
    if (nodes["model-filter"].value && run.model !== nodes["model-filter"].value) return false;
    return !query || JSON.stringify(run).toLocaleLowerCase().includes(query);
  });
}

function renderRunList() {
  const runs = filteredRuns();
  nodes["visible-run-count"].textContent = plural(runs.length, "run");
  const fragment = document.createDocumentFragment();
  for (const run of runs) {
    const key = runKey(run);
    const button = element("button", `run-item${key === state.selected ? " active" : ""}`); button.type = "button";
    button.title = key; button.addEventListener("click", () => selectRun(key));
    const top = element("span", "run-item-top");
    top.append(element("i", `run-dot ${run.status || ""}`), element("span", "run-item-title", run.id));
    const meta = element("span", "run-item-meta");
    if (run.location) meta.append(element("span", "run-location", run.location));
    meta.append(element("span", "", run.task_id || "unknown"), element("span", "", run.model || run.framework || run.adapter || "unknown"));
    const badges = [];
    if (run.detected !== null && run.detected !== undefined) badges.push([`offline ${run.detected ? plural(run.detected, "finding") : "clean"}`, run.detected ? "flagged" : "clean"]);
    if (run.detected_online !== null && run.detected_online !== undefined) badges.push([`online ${run.online_running ? "running · " : ""}${run.detected_online ? plural(run.detected_online, "finding") : "clean"}`, run.online_running ? "running" : run.detected_online ? "flagged online" : "clean"]);
    if (!badges.length) badges.push([compactNumber(run.records?.total || 0), ""]);
    badges.forEach(([text, cls]) => meta.append(element("span", cls, text)));
    button.append(top, meta); fragment.append(button);
  }
  if (!runs.length) fragment.append(element("div", "run-list-empty", "No runs match the current filters."));
  nodes["run-list"].replaceChildren(fragment);
}

async function selectRun(runId, {preserveFilters = false} = {}) {
  const version = ++state.requestVersion;
  const changed = state.selected !== runId;
  state.selected = runId; state.offset = 0; state.activeEvidence = null; state.online = null; state.onlineSignature = ""; state.openSteps.clear(); state.closedSteps.clear(); state.openObjects.clear(); stopPolling();
  if (changed && !preserveFilters) resetTraceFilters();
  history.replaceState(null, "", `#${encodeURIComponent(runId)}`);
  renderRunList(); showTraceLoading();
  try {
    const detail = await request(`/api/runs/${encodeURIComponent(runId)}`);
    if (version !== state.requestVersion) return false;
    state.detail = detail; renderRunHeader(); renderFilters(preserveFilters && !changed); renderInsights();
    nodes["empty-state"].classList.add("hidden"); nodes["run-view"].classList.remove("hidden"); nodes["insight-panel"].classList.remove("hidden");
    await loadEvents({version}); return true;
  } catch (error) { if (version === state.requestVersion) showFatal(error.message); return false; }
}

function renderRunHeader() {
  const metadata = state.detail.metadata || {};
  nodes["run-kicker"].textContent = [metadata.framework || metadata.adapter, metadata.runtime].filter(Boolean).join(" · ") || "COLLECTED TRACE";
  nodes["run-title"].textContent = metadata.id || state.selected;
  nodes["run-status"].textContent = metadata.status || "unknown"; nodes["run-status"].className = `status ${metadata.status || ""}`;
  nodes["run-meta"].textContent = `${metadata.task_id || "unknown task"} · ${formatTime(metadata.started_at)} · ${formatDuration(durationSeconds(metadata.started_at, metadata.ended_at))}`;
  const warnings = Array.isArray(metadata.warnings) ? metadata.warnings : [];
  const errors = Array.isArray(metadata.errors) ? metadata.errors : [];
  nodes.health.classList.toggle("hidden", !warnings.length && !errors.length);
  if (warnings.length || errors.length) {
    const list = element("ul", errors.length ? "errors" : "");
    [...errors, ...warnings].forEach((message) => list.append(element("li", "", message)));
    nodes.health.replaceChildren(element("div", "health-title", `${plural(warnings.length, "warning")} · ${plural(errors.length, "error")}`), list);
  }
}

function renderFilters(preserve = false) {
  const agent = preserve ? nodes["agent-filter"].value : "";
  const tool = preserve ? nodes["tool-filter"].value : "";
  fillSelect(nodes["agent-filter"], "All agents", unique((state.detail.sections || []).map((s) => s.agent_id)), agent);
  fillSelect(nodes["tool-filter"], "All tools", state.detail.tools || [], tool);
}

function renderInsights() {
  const reports = state.detail.reports || {};
  const onlineReport = reports.online?.live || null;
  nodes["online-count"].textContent = onlineReport ? Object.keys(onlineReport.behaviors || {}).length || "" : "";
  nodes["tab-online"].classList.toggle("running", Boolean(onlineReport && onlineReport.status === "running"));
  setTab(state.tab);
}

function setTab(tab) {
  state.tab = tab;
  nodes["tab-offline"].classList.toggle("active", tab === "offline");
  nodes["tab-online"].classList.toggle("active", tab === "online");
  nodes["detections-content"].classList.toggle("hidden", tab !== "offline");
  nodes["online-content"].classList.toggle("hidden", tab !== "online");
  if (tab === "offline") { stopPolling(); renderDetections(); }
  else loadOnline().catch((error) => { nodes["online-content"].replaceChildren(element("div", "no-detection-note", error.message)); });
}

function evidenceChain() {
  const chain = [];
  const push = (source, behaviors) => {
    for (const [behavior, value] of Object.entries(behaviors || {}))
      for (const span of value.evidence || []) chain.push({...span, behavior, source, key: `${source}:${behavior}:${span.start}:${span.end}:${chain.length}`});
  };
  const reports = state.detail?.reports || {};
  push("offline", reports.offline?.behaviors || state.detail?.detections?.behaviors);
  push("online", reports.online?.live?.behaviors);
  return chain.sort((a, b) => a.start - b.start || a.end - b.end);
}

function stopPolling() { if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; } }

async function loadOnline() {
  if (!state.selected) return;
  const reports = state.detail.reports || {};
  const report = reports.online?.live;
  if (!report) { nodes["online-content"].replaceChildren(element("div", "no-detection-note", "No live online results for this run.")); return; }
  const version = state.requestVersion;
  const view = await request(`/api/runs/${encodeURIComponent(state.selected)}/online`);
  if (version !== state.requestVersion || state.tab !== "online") return;
  const signature = JSON.stringify(view);
  state.online = view;
  if (signature !== state.onlineSignature) { state.onlineSignature = signature; renderOnline(); markUnrevealed(); }
  if (view.status === "running") {
    stopPolling();
    state.pollTimer = setTimeout(async () => {
      try { const detail = await request(`/api/runs/${encodeURIComponent(state.selected)}`); if (version !== state.requestVersion) return; state.detail = detail; renderInsights(); }
      catch (error) { showFatal(error.message); }
    }, 3000);
  }
}

function renderOnline() {
  const view = state.online; const fragment = document.createDocumentFragment();
  const progress = view.progress || {}; const timing = view.timing || {};
  const status = element("div", `online-status ${view.status}`);
  status.append(
    element("span", "online-pill", view.status === "running" ? "running" : "completed"),
    element("span", "", `steps ${progress.steps_resolved ?? 0}/${progress.steps_total ?? "?"} resolved`),
    element("span", "", `records ${progress.records_revealed ?? "?"}/${progress.records_arrived ?? 0} delivered${progress.feed_ended ? ", feed ended" : ""}`),
    element("span", "", timing.median_seconds ? `median ${Math.round(timing.median_seconds)}s/step, max ${Math.round(timing.max_seconds)}s` : "no timing yet"),
  );
  fragment.append(status);
  const chain = evidenceChain().filter((span) => span.source === "online");
  if (chain.length) {
    const nav = element("div", "evidence-nav"); nav.append(element("span", "", `${plural(chain.length, "evidence span")} · trace order`));
    const controls = element("span", ""); const prev = element("button", "mini-button", "←"), next = element("button", "mini-button", "→");
    prev.addEventListener("click", () => moveEvidence(-1)); next.addEventListener("click", () => moveEvidence(1)); controls.append(prev, next); nav.append(controls); fragment.append(nav);
  }
  fragment.append(element("h3", "online-section", `Findings ${view.findings.length ? `(${view.findings.length})` : ""}`));
  if (!view.findings.length) fragment.append(element("div", view.status === "running" ? "no-detection-note" : "clean-detection", view.status === "running" ? "No finding reported yet." : "Online Detector: no finding reported in this run."));
  for (const finding of view.findings) {
    const card = element("article", `behavior-card online${finding.status === "withdrawn" ? " withdrawn" : ""}`);
    card.append(element("h3", "", finding.taxonomy), element("p", "behavior-summary", finding.description || ""));
    const meta = element("div", "finding-meta");
    meta.append(element("span", "", finding.finding_id), element("span", "", `first evidence L${finding.first_evidence}`), element("span", "", `reported at record ${finding.proposed_at}`), element("span", "delay", finding.delay_records === null ? "" : `delay ${finding.delay_records} records`));
    if (finding.status === "withdrawn") meta.append(element("span", "withdrawn-tag", "withdrawn"));
    if (finding.delivered_round) meta.append(element("span", "delivered", `delivered · round ${finding.delivered_round}`));
    if (finding.repeats) meta.append(element("span", "repeats", `repeats ${finding.repeats}`));
    card.append(meta);
    const list = element("div", "evidence-list");
    for (const span of chain.filter((item) => item.behavior.startsWith(finding.taxonomy))) {
      const button = element("button", `evidence-item online${state.activeEvidence === span.key ? " active" : ""}`); button.type = "button";
      button.append(element("span", "evidence-line", span.start === span.end ? `L${span.start}` : `L${span.start}–${span.end}`), element("span", "evidence-note", span.note || "No note"));
      button.addEventListener("click", () => jumpEvidence(span)); list.append(button);
    }
    card.append(list); fragment.append(card);
  }
  if (view.interventions && view.interventions.length) {
    fragment.append(element("h3", "online-section", `Interventions (${view.interventions.filter((e) => e.event === "intervened").length})`));
    const list = element("div", "evidence-list");
    for (const entry of view.interventions) {
      const kind = entry.event === "intervened" ? "" : entry.event === "released" ? " released" : " timeout";
      const row = element(entry.trace_line ? "button" : "div", `evidence-item intervention-item${kind}`); if (entry.trace_line) row.type = "button";
      const label = entry.event === "intervened" ? `${entry.round ? `barrier round ${entry.round}` : (entry.hook || "process channel")} · L${entry.trace_line}` : entry.event.replace(/_/g, " ");
      const waited = entry.waited_seconds === null || entry.waited_seconds === undefined ? "" : `waited ${Math.round(entry.waited_seconds)}s`;
      const ids = entry.finding_ids.length ? `${entry.event === "intervened" ? "delivered" : "undelivered"} ${entry.finding_ids.join(", ")}` : "no new finding";
      row.append(element("span", "evidence-line", label), element("span", "evidence-note", [entry.hook, waited, ids].filter(Boolean).join(" · ")));
      if (entry.trace_line) row.addEventListener("click", () => jumpLine(entry.trace_line));
      list.append(row);
    }
    fragment.append(list);
  }
  fragment.append(element("h3", "online-section", `Steps (${view.steps.length})`));
  const stepsBox = element("div", "online-steps");
  for (const step of [...view.steps].reverse()) stepsBox.append(renderStep(step));
  fragment.append(stepsBox);
  nodes["online-content"].replaceChildren(fragment);
}

function stepOpen(step) {
  if (state.openSteps.has(step.id)) return true;
  if (state.closedSteps.has(step.id)) return false;
  return step.violations > 0;
}

function renderStep(step) {
  const details = element("details", `online-step${step.violations ? " has-violation" : ""}${step.status !== "resolved" ? " pending" : ""}`);
  details.open = stepOpen(step);
  details.addEventListener("toggle", () => { if (details.open) { state.openSteps.add(step.id); state.closedSteps.delete(step.id); } else { state.closedSteps.add(step.id); state.openSteps.delete(step.id); } });
  const summary = element("summary", "step-row");
  const lines = step.start === step.end ? `L${step.start}` : `L${step.start}–${step.end}`;
  summary.append(
    element("span", "step-id", step.id),
    element("span", "step-lines", lines),
    element("span", "step-cell", plural(step.objects.length, "object")),
    element("span", `step-cell${step.violations ? " bad" : ""}`, step.violations ? plural(step.violations, "violation") : "clean"),
    element("span", "step-cell muted", step.duration_seconds ? `${Math.round(step.duration_seconds)}s` : ""),
  );
  details.append(summary);
  if (!details.open) { details.addEventListener("toggle", () => { if (details.open && !details.dataset.filled) fillStep(details, step); }, {once: false}); }
  if (details.open) fillStep(details, step);
  return details;
}

function fillStep(details, step) {
  if (details.dataset.filled) return;
  details.dataset.filled = "1";
  const body = element("div", "step-body");
  if (step.summary) { body.append(element("div", "step-label", "Summary"), element("p", "step-summary", step.summary)); }
  if (step.references.length) {
    body.append(element("div", "step-label", `Ledger · ${plural(step.references.length, "row")}`));
    const ledger = element("div", "ledger");
    for (const ref of step.references) {
      const row = element("div", "ledger-row");
      row.append(element("span", `ref-kind ${ref.kind}`, ref.kind), lineLink(ref.line), element("span", "ref-text", ref.text));
      ledger.append(row);
    }
    body.append(ledger);
  }
  if (step.objects.length) {
    body.append(element("div", "step-label", `Audit objects · ${plural(step.objects.length, "object")}`));
    for (const object of step.objects) body.append(renderAuditObject(object));
  }
  if (!step.summary && !step.references.length && !step.objects.length) body.append(element("p", "step-summary", "Nothing recorded for this step."));
  details.append(body);
}

function lineLink(line, label = null) {
  const button = element("button", "line-link", label || `L${line}`); button.type = "button";
  button.addEventListener("click", (event) => { event.preventDefault(); jumpLine(line); });
  return button;
}

function renderAuditObject(object) {
  const box = element("div", `audit-object${object.violations ? " violation" : ""}`);
  const head = element("div", "audit-head");
  head.append(element("span", `ref-kind ${object.kind}`, object.kind), element("span", "object-id", object.object_id), lineLink(object.line));
  box.append(head, element("p", "object-text", object.text));
  for (const row of object.checks.filter((r) => r.verdict !== "clean")) {
    const note = element("div", `verdict ${row.verdict}`);
    note.append(element("b", "", row.item), element("span", "verdict-note", row.note || row.verdict)); box.append(note);
  }
  const extras = [];
  if (object.source_text) extras.push(["source", object.source_line, object.source_text]);
  for (const src of object.sources || []) extras.push(["source", src.line, src.text]);
  const cleanRows = object.checks.filter((r) => r.verdict === "clean");
  if (extras.length || cleanRows.length || object.unsourced_numbers?.length) {
    const key = object.object_id;
    const more = element("div", `object-more${state.openObjects.has(key) ? "" : " hidden"}`);
    for (const [label, line, text] of extras) { const row = element("div", "object-source"); row.append(lineLink(line, `${label} L${line}`), element("span", "", text)); more.append(row); }
    if (object.unsourced_numbers?.length) more.append(element("div", "object-unsourced", `no trace origin for ${object.unsourced_numbers.join(", ")}`));
    for (const row of cleanRows) { const note = element("div", "verdict clean"); note.append(element("b", "", row.item), element("span", "verdict-note", row.note || "")); more.append(note); }
    const parts = []; if (extras.length) parts.push(plural(extras.length, "source")); if (cleanRows.length) parts.push(plural(cleanRows.length, "clean note"));
    const toggle = element("button", "content-toggle", `${state.openObjects.has(key) ? "Hide" : "Show"} ${parts.join(" · ")}`); toggle.type = "button";
    toggle.addEventListener("click", () => { const hidden = more.classList.toggle("hidden"); if (hidden) state.openObjects.delete(key); else state.openObjects.add(key); toggle.textContent = `${hidden ? "Show" : "Hide"} ${parts.join(" · ")}`; });
    box.append(toggle, more);
  }
  return box;
}

function markUnrevealed() {
  const arrived = state.online?.status === "running" ? state.online?.progress?.records_arrived : null;
  for (const card of nodes.trace.querySelectorAll(".event")) {
    const lines = JSON.parse(card.dataset.lines || "[]");
    card.classList.toggle("unrevealed", arrived !== null && arrived !== undefined && lines.length > 0 && Math.min(...lines) > arrived);
  }
}

function renderDetections() {
  const detections = state.detail.detections;
  const chain = evidenceChain().filter((span) => span.source === "offline");
  const total = Object.keys(detections?.behaviors || {}).length;
  nodes["detection-count"].textContent = total || "";
  const fragment = document.createDocumentFragment();
  if (chain.length) {
    const nav = element("div", "evidence-nav"); nav.append(element("span", "", `${plural(chain.length, "evidence span")} · trace order`));
    const controls = element("span", ""); const prev = element("button", "mini-button", "←"), next = element("button", "mini-button", "→");
    prev.addEventListener("click", () => moveEvidence(-1)); next.addEventListener("click", () => moveEvidence(1)); controls.append(prev, next); nav.append(controls); fragment.append(nav);
  }
  if (!detections) fragment.append(element("div", "no-detection-note", "No offline results yet. Run `ridar detect` on this run."));
  else if (!Object.keys(detections.behaviors).length) fragment.append(element("div", "clean-detection", "Offline Detector: no confirmed integrity violation in this run."));
  else appendBehaviorCards(fragment, detections.behaviors, "offline", chain);
  nodes["detections-content"].replaceChildren(fragment);
}

function appendBehaviorCards(fragment, behaviors, source, chain) {
  for (const [behavior, value] of Object.entries(behaviors)) {
    const card = element("article", `behavior-card ${source}`);
    const title = element("h3", "", behavior);
    card.append(title, element("p", "behavior-summary", value.summary || "No summary."));
    const list = element("div", "evidence-list");
    for (const span of chain.filter((item) => item.source === source && item.behavior === behavior)) {
      const button = element("button", `evidence-item ${source}${state.activeEvidence === span.key ? " active" : ""}`); button.type = "button";
      button.append(element("span", "evidence-line", span.start === span.end ? `L${span.start}` : `L${span.start}–${span.end}`), element("span", "evidence-note", span.note || "No segment note"));
      button.addEventListener("click", () => jumpEvidence(span)); list.append(button);
    }
    card.append(list); fragment.append(card);
  }
}

function spansForItem(item) { return evidenceChain().filter((span) => item.lines.some((line) => line >= span.start && line <= span.end)); }
function moveEvidence(direction) { const chain = evidenceChain(); if (!chain.length) return; let index = chain.findIndex((span) => span.key === state.activeEvidence); if (index < 0) index = direction > 0 ? -1 : 0; jumpEvidence(chain[(index + direction + chain.length) % chain.length]); }
async function jumpEvidence(span) { state.activeEvidence = span.key; if (state.tab === "offline") renderDetections(); else if (state.online) renderOnline(); await jumpLine(span.start); }
async function jumpLine(line) {
  resetTraceFilters(); state.offset = 0;
  await loadEvents({anchor: line});
  const target = [...nodes.trace.querySelectorAll(".event")].find((card) => JSON.parse(card.dataset.lines || "[]").some((value) => value >= line));
  target?.scrollIntoView({behavior: "smooth", block: "center"});
}

function setTraceFilter(kind, value) {
  if (kind === "agent") nodes["agent-filter"].value = value;
  if (kind === "type") nodes["type-filter"].value = value;
  if (kind === "tool") nodes["tool-filter"].value = value;
  state.offset = 0; loadEvents().catch((error) => showFatal(error.message));
}
function resetTraceFilters() { nodes["trace-search"].value = ""; nodes["agent-filter"].value = ""; nodes["type-filter"].value = ""; nodes["tool-filter"].value = ""; }

async function loadEvents({version = state.requestVersion, anchor = null} = {}) {
  if (!state.selected) return;
  const eventVersion = ++state.eventVersion;
  const query = new URLSearchParams({offset: state.offset, limit: state.limit, agent: nodes["agent-filter"].value, type: nodes["type-filter"].value, tool: nodes["tool-filter"].value, q: nodes["trace-search"].value});
  if (anchor !== null) query.set("anchor", anchor);
  const page = await request(`/api/runs/${encodeURIComponent(state.selected)}/events?${query}`);
  if (version !== state.requestVersion || eventVersion !== state.eventVersion) return;
  state.offset = page.offset; renderEvents(page);
}

function renderEvents(page) {
  const fragment = document.createDocumentFragment(); page.items.forEach((item) => fragment.append(renderEvent(item)));
  if (!page.items.length) fragment.append(element("div", "trace-empty", "No trace records match the current filters."));
  nodes.trace.replaceChildren(fragment); nodes["result-count"].textContent = plural(page.total, "record");
  const first = page.total ? page.offset + 1 : 0, last = Math.min(page.offset + page.limit, page.total);
  nodes["page-label"].textContent = `${first}–${last} of ${page.total}`; nodes["previous-page"].disabled = page.offset === 0; nodes["next-page"].disabled = page.offset + page.limit >= page.total;
  const active = evidenceChain().find((span) => span.key === state.activeEvidence);
  nodes["active-evidence"].textContent = active ? `${active.source.toUpperCase()} · ${active.behavior} · L${active.start}${active.end === active.start ? "" : `–${active.end}`}` : "";
  markUnrevealed();
}

function renderEvent(item) {
  const matches = spansForItem(item); const active = matches.some((span) => span.key === state.activeEvidence);
  const card = element("article", `event ${item.display_type}${matches.length ? " evidence" : ""}${active ? " active-evidence-card" : ""}`); card.dataset.lines = JSON.stringify(item.lines || []);
  const isTool = item.kind === "tool" || item.kind === "tool_result"; const label = isTool ? item.name : labelFor(item.display_type);
  const head = element("header", "event-head"); head.append(element("span", "event-label", label), element("span", "event-index", lineLabel(item.lines)));
  if (item.pair_status && item.pair_status !== "complete") head.append(element("span", `badge ${item.pair_status === "error" ? "error" : "warning"}`, item.pair_status));
  if (item.display_type === "supervisor") head.append(element("span", "badge warning", `${item.round ? `barrier round ${item.round}` : (item.hook || "process channel")} · ${(item.finding_ids || []).join(", ") || "no finding"}`));
  head.append(element("span", "event-agent", item.agent_id === "main" ? "" : item.agent_id)); card.append(head);
  const body = element("div", "event-body");
  if (matches.length) { const tags = element("div", "evidence-tags"); matches.forEach((span) => tags.append(element("span", `${span.source}${span.key === state.activeEvidence ? " active" : ""}`, span.behavior))); body.append(tags); }
  if (item.kind === "tool") { body.append(ioBlock("Request", item.input), ioBlock("Response", item.result?.content ?? "")); }
  else if (item.kind === "tool_result") body.append(ioBlock("Response", item.result?.content ?? ""));
  else body.append(contentBlock(item.content)); card.append(body); return card;
}

function ioBlock(label, value) { const block = element("section", "io"); block.append(element("div", "io-label", label), contentBlock(value)); return block; }
function contentBlock(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? "", null, 2); const wrapper = element("div"); const pre = element("pre", "content", text); wrapper.append(pre);
  if (text.length > 1600 || text.split("\n").length > 14) { pre.classList.add("truncated"); const button = element("button", "content-toggle", "Show more"); button.type = "button"; button.addEventListener("click", () => { const collapsed = pre.classList.toggle("truncated"); button.textContent = collapsed ? "Show more" : "Show less"; }); wrapper.append(button); }
  return wrapper;
}

function lineLabel(lines = []) { if (!lines.length) return ""; return lines.length === 1 ? `L${lines[0]}` : `L${Math.min(...lines)} + L${Math.max(...lines)}`; }
function labelFor(type) { return {user: "User", assistant: "Assistant", reasoning: "Thinking", error: "Error", supervisor: "Supervisor"}[type] || type; }
function compactNumber(value) { return new Intl.NumberFormat(undefined, {notation: value >= 10000 ? "compact" : "standard", maximumFractionDigits: 1}).format(value); }
function formatTime(value) { if (!value) return "unknown time"; const date = new Date(value); return Number.isNaN(date.valueOf()) ? value : date.toLocaleString(); }
function durationSeconds(start, end) { const a = new Date(start).valueOf(), b = new Date(end).valueOf(); return Number.isFinite(a) && Number.isFinite(b) ? Math.max(0, (b - a) / 1000) : null; }
function formatDuration(seconds) { if (seconds === null || seconds === undefined) return "—"; if (seconds < 60) return `${Math.round(seconds)}s`; if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`; return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`; }
function showTraceLoading() { nodes.trace.replaceChildren(element("div", "run-skeleton"), element("div", "run-skeleton")); }
function showEmpty(title, message) { nodes["run-view"].classList.add("hidden"); nodes["insight-panel"].classList.add("hidden"); nodes["empty-state"].classList.remove("hidden"); nodes["empty-title"].textContent = title; nodes["empty-message"].textContent = message; }
function showFatal(message) { setSync("error", "Unable to load"); showEmpty("Something went wrong", message); }
function resetAndLoad() { state.offset = 0; loadEvents().catch((error) => showFatal(error.message)); }

nodes["run-search"].addEventListener("input", () => renderRunList());
["run-status-filter", "task-filter", "framework-filter", "model-filter"].forEach((id) => nodes[id].addEventListener("change", () => renderRunList()));
nodes["refresh-runs"].addEventListener("click", () => loadRuns(true));
["agent-filter", "type-filter", "tool-filter"].forEach((id) => nodes[id].addEventListener("change", resetAndLoad));
nodes["trace-search"].addEventListener("input", () => { clearTimeout(state.searchTimer); state.searchTimer = setTimeout(resetAndLoad, 250); });
nodes["previous-page"].addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); loadEvents().catch((e) => showFatal(e.message)); });
nodes["next-page"].addEventListener("click", () => { state.offset += state.limit; loadEvents().catch((e) => showFatal(e.message)); });
nodes["tab-offline"].addEventListener("click", () => setTab("offline"));
nodes["tab-online"].addEventListener("click", () => setTab("online"));
nodes["theme-button"].addEventListener("click", () => { const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = next; localStorage.setItem("monitor-theme", next); });
window.addEventListener("hashchange", () => { const key = decodeURIComponent(location.hash.replace(/^#/, "")); if (key && key !== state.selected && state.runs.some((run) => runKey(run) === key)) selectRun(key); });
document.addEventListener("keydown", (event) => { const editing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName); if (event.key === "/" && !editing) { event.preventDefault(); nodes["trace-search"].focus(); } if (event.key.toLowerCase() === "r" && !editing) loadRuns(true); if (event.key === "[" && !editing) moveEvidence(-1); if (event.key === "]" && !editing) moveEvidence(1); });
const savedTheme = localStorage.getItem("monitor-theme"); if (savedTheme) document.documentElement.dataset.theme = savedTheme;
loadRuns();
