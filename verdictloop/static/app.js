"use strict";

// The page renders backend evidence only. It never derives authorization truth
// or fabricates a benchmark result in the browser.
const byId = (id) => document.getElementById(id);
const buttons = ["refresh-button", "safe-button", "unsafe-button", "compare-button", "resume-button"];
const state = { phase: "not_started", busy: false, compared: false, resumed: false };

function value(value, fallback = "—") {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "boolean") return value ? "YES" : "NO";
  return String(value);
}

function put(id, content, fallback = "—") {
  byId(id).textContent = value(content, fallback);
}

function decision(id, allowed) {
  const node = byId(id);
  node.classList.remove("decision-allow", "decision-deny");
  if (typeof allowed !== "boolean") {
    node.textContent = "—";
    return;
  }
  node.textContent = allowed ? "ALLOW" : "DENY";
  node.classList.add(allowed ? "decision-allow" : "decision-deny");
}

function announce(message, isError = false) {
  const node = byId("message");
  node.textContent = message;
  node.classList.toggle("is-error", isError);
}

function connection(label, mode) {
  const node = byId("connection");
  node.classList.toggle("is-connected", mode === "connected");
  node.classList.toggle("is-error", mode === "error");
  put("connection-text", label);
}

function logEvent(message) {
  const list = byId("activity");
  if (list.children.length === 1 && list.firstElementChild.textContent === "Waiting for the active Atlas state.") {
    list.replaceChildren();
  }
  const item = document.createElement("li");
  item.textContent = message;
  list.prepend(item);
  while (list.children.length > 5) list.lastElementChild.remove();
}

function setBusy(busy) {
  state.busy = busy;
  for (const id of buttons) byId(id).disabled = busy;
  if (!busy) updateButtons();
}

function updateButtons() {
  if (state.busy) return;
  byId("safe-button").disabled = state.phase === "safe_promoted" || state.phase === "unsafe_rejected";
  byId("unsafe-button").disabled = state.phase !== "safe_promoted";
  byId("compare-button").disabled = false;
  byId("resume-button").disabled = false;
  byId("refresh-button").disabled = false;
}

function setPhase(phase) {
  state.phase = phase || "not_started";
  const labels = {
    not_started: "Not started",
    initialized: "Policy v1 active",
    safe_promoted: "Safe policy promoted",
    unsafe_rejected: "Unsafe candidate rejected",
  };
  put("phase", labels[state.phase] || state.phase);
  const safeDone = state.phase === "safe_promoted" || state.phase === "unsafe_rejected";
  for (const node of document.querySelectorAll(".workflow li")) {
    const step = node.dataset.step;
    const done = (safeDone && ["propose", "probe", "judge", "persist"].includes(step)) ||
      (state.compared && step === "learn") || (state.resumed && step === "reuse");
    node.classList.toggle("is-complete", done);
  }
  updateButtons();
}

function renderActive(active) {
  put("active-policy", active?.policy_id);
  put("active-version", active?.policy_version);
  put("active-strategy", active?.strategy_id);
  put("active-revision", active?.revision);
  put("fixture-hash", active?.fixture_hash);
}

function renderCall(call) {
  if (!call) return;
  put("task-tool", call.tool);
  put("task-actor", call.actor);
  put("task-scope", call.tenant && call.service ? `${call.tenant} / ${call.service}` : null);
  put("task-env", call.env_label);
  put("task-incident", call.incident_id);
  put("task-case", call.case_id);
}

function renderBlockedTask(blocked) {
  if (!blocked) {
    for (const id of ["task-tool", "task-actor", "task-scope", "task-env", "task-incident", "task-case"]) put(id, null);
    decision("task-oracle");
    decision("task-policy");
    put("task-policy-label", "Current active policy");
    return;
  }
  renderCall(blocked.call);
  decision("task-oracle", blocked.oracle_decision);
  const hasInitial = typeof blocked.initial_policy_decision === "boolean";
  decision("task-policy", hasInitial ? blocked.initial_policy_decision : blocked.active_policy_decision);
  put("task-policy-label", hasInitial ? "Policy v1 at proposal" : "Current active policy");
}

function renderTrialHistory(data) {
  const records = [];
  if (Array.isArray(data?.trials)) records.push(...data.trials);
  for (const name of ["latest_safe_trial", "latest_unsafe_trial"]) {
    if (data?.[name]) records.push(data[name]);
  }
  const seen = new Set();
  const unique = records.filter((record) => {
    const trial = record?.trial;
    if (!trial?.trial_id || seen.has(trial.trial_id)) return false;
    seen.add(trial.trial_id);
    return true;
  });
  const host = byId("trial-history");
  if (unique.length === 0) {
    host.className = "history-empty";
    host.textContent = "No persisted trial is attached to this run yet.";
    return;
  }
  const list = document.createElement("ol");
  list.className = "history-list";
  for (const record of unique) {
    const trial = record.trial;
    const item = document.createElement("li");
    item.classList.toggle("is-reject", trial.verdict === "REJECT");
    const verdict = document.createElement("strong");
    verdict.textContent = `${value(trial.verdict, "Verdict pending")} · ${value(trial.candidate_id, "candidate")}`;
    const id = document.createElement("code");
    id.textContent = trial.trial_id;
    item.append(verdict, id);
    list.append(item);
  }
  host.className = "";
  host.replaceChildren(list);
}

function renderCandidate(candidate, fallbackDiff) {
  if (!candidate) {
    put("candidate-id", "Awaiting proposal");
    put("candidate-producer", null);
    put("candidate-mutation", null);
    put("candidate-hash", null);
    byId("candidate-diff").textContent = "Run a proposal to see its actual policy diff.";
    return;
  }
  put("candidate-id", candidate.candidate_id);
  put("candidate-producer", candidate.producer);
  put("candidate-mutation", candidate.mutation_class);
  put("candidate-hash", candidate.content_hash);
  const diff = candidate.diff || candidate.rule_diff || fallbackDiff || {
    added_rules: candidate.added_rules,
    removed_rules: candidate.removed_rules,
    changed_rules: candidate.changed_rules,
  };
  const noDiff = typeof diff === "object" && diff !== null &&
    Object.values(diff).every((part) => part === undefined || (Array.isArray(part) && part.length === 0));
  byId("candidate-diff").textContent = noDiff ? "No policy rule diff was returned by the backend." :
    typeof diff === "string" ? diff : JSON.stringify(diff, null, 2);
}

function renderEvidence(trial) {
  const host = byId("evidence");
  const counterexamples = Array.isArray(trial?.counterexamples) ? trial.counterexamples : [];
  const caseIds = Array.isArray(trial?.evidence_case_ids) ? trial.evidence_case_ids : [];
  if (counterexamples.length === 0 && caseIds.length === 0) {
    host.className = "evidence-empty";
    host.textContent = Number.isInteger(trial?.full_sweep_count) && trial.full_sweep_count > 0 ?
      `Full finite-fixture sweep completed: ${trial.full_sweep_count} calls, with no counterexample returned.` :
      "No executed evidence was returned.";
    return;
  }
  const list = document.createElement("ol");
  list.className = "evidence-list";
  for (const counterexample of counterexamples) {
    const item = document.createElement("li");
    const kind = document.createElement("strong");
    kind.textContent = value(counterexample.failure_class, "Counterexample");
    const description = document.createElement("span");
    const calls = Array.isArray(counterexample.case_ids) ? counterexample.case_ids.join(", ") : "";
    const policy = Array.isArray(counterexample.policy_decisions) ? counterexample.policy_decisions.map((x) => x ? "ALLOW" : "DENY").join(", ") : "";
    const oracle = Array.isArray(counterexample.oracle_decisions) ? counterexample.oracle_decisions.map((x) => x ? "ALLOW" : "DENY").join(", ") : "";
    description.textContent = [counterexample.detail, calls && `Cases: ${calls}`, policy && `Policy: ${policy}`, oracle && `Oracle: ${oracle}`].filter(Boolean).join(" · ");
    item.append(kind, description);
    list.append(item);
  }
  if (counterexamples.length === 0) {
    for (const caseId of caseIds) {
      const item = document.createElement("li");
      item.textContent = `Evidence case: ${caseId}`;
      list.append(item);
    }
  }
  host.className = "";
  host.replaceChildren(list);
}

function renderTrialDetails(trial, oracleDecision, candidateDecision, call) {
  put("judge-verdict", trial?.verdict, "Awaiting trial");
  byId("judge-verdict").classList.toggle("is-promote", trial?.verdict === "PROMOTE");
  byId("judge-verdict").classList.toggle("is-reject", trial?.verdict === "REJECT");
  put("trial-id", trial?.trial_id);
  put("trial-context", call ? `Evaluated call ${value(call.case_id)} · ${value(call.actor)} · ${value(call.approval_context_id)}` : "No evaluated call selected.");
  decision("trial-oracle", oracleDecision);
  decision("trial-candidate", candidateDecision);
  put("sweep-count", trial?.full_sweep_count);
  put("target-outcome", trial?.target_outcome);
  put("regression-count", trial?.regression_count);
  put("universe-hash", trial?.universe_hash);
  put("failure-classes", !trial ? null : Array.isArray(trial.failure_classes) && trial.failure_classes.length ? trial.failure_classes.join(", ") : "None");
  renderEvidence(trial);
}

function renderTrialAction(data, kind) {
  if (data.active) renderActive(data.active);
  if (data.phase) setPhase(data.phase);
  if (kind === "safe" && data.call) {
    renderCall(data.call);
    decision("task-oracle", data.oracle_decision);
    decision("task-policy", data.before_decision);
    put("task-policy-label", "Policy v1 at proposal");
  }
  renderCandidate(data.candidate, data.policy_diff);
  const trial = data.trial || {};
  renderTrialDetails(trial, data.oracle_decision, data.candidate_decision, data.call);
  const kindLabel = kind === "safe" ? "Safe candidate" : "Approval-removal candidate";
  logEvent(`${kindLabel}: ${value(trial.verdict)}. Trial ${value(trial.trial_id)} persisted.`);
  announce(`${kindLabel} received a real ${value(trial.verdict)} verdict. Active Atlas state reloaded.`);
}

function renderComparison(data, quiet = false) {
  const card = data.scorecard || data.comparison || data;
  const host = byId("comparison");
  host.classList.remove("is-unavailable");
  host.replaceChildren();
  if (!card.summary?.baseline || !card.summary?.learned || !Array.isArray(card.candidates)) {
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(card, null, 2);
    host.append(pre);
  } else {
    const meta = document.createElement("p");
    meta.className = "comparison-meta";
    const seeds = Array.isArray(card.heldout_seeds) ? card.heldout_seeds.join(", ") : "—";
    const duration = typeof data.evaluation_duration_ms === "number" ? ` · Evaluation ${Math.round(data.evaluation_duration_ms)} ms` : "";
    meta.textContent = `Manifest ${value(card.manifest?.manifest_id)} · Fixture ${value(card.fixture_hash)} · ${card.candidates.length} held-out candidates · Seeds ${seeds} · Budget ${value(card.budget)} decisions per trial${duration}`;
    host.append(meta);

    const grid = document.createElement("div");
    grid.className = "metric-grid";
    for (const [label, summary] of [["Uniform", card.summary.baseline], ["Learned", card.summary.learned]]) {
      const box = document.createElement("div");
      box.className = "metric-card";
      const heading = document.createElement("h3");
      heading.textContent = `${label} · ${value(summary.strategy_id)}`;
      const rate = document.createElement("strong");
      rate.textContent = `${summary.violating_trials_detected}/${summary.violating_trials_total}`;
      const description = document.createElement("p");
      const percent = typeof summary.detection_rate === "number" ? `${Math.round(summary.detection_rate * 100)}%` : "—";
      description.textContent = `violating trials found within budget (${percent}); ${summary.distinct_violating_detected}/${summary.distinct_violating_total} distinct violating candidates`;
      const details = document.createElement("small");
      details.textContent = `Alias decisions: ${summary.alias_decisions} · Mean first index: ${summary.mean_probes_to_first} · Probe decisions: ${summary.decisions_used} · Duration: ${summary.probe_duration_ms} ms · Safe false alarms: ${summary.safe_control_false_alarms}/${summary.safe_control_trials}`;
      box.append(heading, rate, description, details);
      grid.append(box);
    }
    host.append(grid);

    const selection = document.createElement("p");
    selection.className = "comparison-note";
    selection.textContent = `Validation selected ${value(card.selection?.selected?.strategy_id)}. ${value(card.selection?.reason, "")}`;
    host.append(selection);

    const tableScroll = document.createElement("div");
    tableScroll.className = "table-scroll";
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const header = document.createElement("tr");
    for (const title of ["Candidate / mutation", "Full gate", "Uniform first hit, by seed", "Learned first hit, by seed"]) {
      const cell = document.createElement("th");
      cell.scope = "col";
      cell.textContent = title;
      header.append(cell);
    }
    thead.append(header);
    const tbody = document.createElement("tbody");
    const baselineId = card.summary.baseline.strategy_id;
    const learnedId = card.summary.learned.strategy_id;
    for (const row of card.candidates) {
      const tr = document.createElement("tr");
      const cells = [
        `${value(row.candidate_id)} · ${value(row.mutation_class)}`,
        value(row.gate_verdict),
        Array.isArray(row.strategies?.[baselineId]?.first_hits) ? row.strategies[baselineId].first_hits.join(" / ") : "—",
        Array.isArray(row.strategies?.[learnedId]?.first_hits) ? row.strategies[learnedId].first_hits.join(" / ") : "—",
      ];
      for (const content of cells) {
        const cell = document.createElement("td");
        cell.textContent = content;
        tr.append(cell);
      }
      tbody.append(tr);
    }
    table.append(thead, tbody);
    tableScroll.append(table);
    host.append(tableScroll);

    const accounting = data.decision_accounting;
    if (accounting) {
      const counts = document.createElement("p");
      counts.className = "comparison-note";
      counts.textContent = `Authorization evaluations: uniform probe ${value(accounting.uniform_fast_policy_decisions)}, learned probe ${value(accounting.learned_fast_policy_decisions)}, full gates ${value(accounting.full_gate_authorization_evaluations)}; total ${value(accounting.all_heldout_authorization_evaluations)}.`;
      host.append(counts);
    }
    const foot = document.createElement("p");
    foot.className = "comparison-note";
    foot.textContent = value(card.notes, "Descriptive bounded-fixture result.");
    host.append(foot);
  }
  state.compared = true;
  setPhase(state.phase);
  if (!quiet) {
    logEvent("Held-out comparison returned from the evaluator.");
    announce("Held-out comparison loaded from the backend.");
  }
}

function renderResume(data) {
  state.resumed = Boolean(data.later_trial && data.later_trial.strategy_id === data.active?.strategy_id);
  setPhase(state.phase);
  if (data.active) renderActive(data.active);
  const host = byId("resume-result");
  host.classList.remove("is-unavailable");
  host.replaceChildren();
  const summary = document.createElement("dl");
  summary.className = "readback-grid";
  const fields = [
    ["Selected strategy", data.selected_strategy?.strategy_id],
    ["Atlas active strategy", data.active?.strategy_id],
    ["Atlas revision", data.active?.revision],
    ["Promotion eligible", data.promotion_eligible],
    ["Pointer promoted now", data.promoted],
    ["Already active", data.already_active],
  ];
  if (data.later_trial) {
    fields.push(["Later trial", data.later_trial.trial_id || data.later_trial.candidate_id]);
    fields.push(["Later trial strategy", data.later_trial.strategy_id]);
    fields.push(["Later trial probe", `${value(data.later_trial.decisions_used)} decisions · first hit ${value(data.later_trial.first_violation_index)}`]);
    fields.push(["Later trial full gate", data.later_trial.full_gate_verdict]);
  }
  for (const [label, content] of fields) {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = label;
    const definition = document.createElement("dd");
    definition.textContent = value(content);
    row.append(term, definition);
    summary.append(row);
  }
  host.append(summary);
  const explanation = document.createElement("p");
  explanation.className = "comparison-note";
  explanation.textContent = data.later_trial ?
    "This server used the Atlas-loaded strategy on a later trial. Run CLI resume in a fresh process to demonstrate cross-process reuse." :
    "This is an Atlas pointer readback in the current server process. Run CLI resume in a fresh process for cross-process reuse.";
  host.append(explanation);
  const details = document.createElement("details");
  const title = document.createElement("summary");
  title.textContent = "Full Atlas readback response";
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(data, null, 2);
  details.append(title, pre);
  host.append(details);
  logEvent(`Atlas readback returned active strategy ${value(data.active?.strategy_id, "from this run")}.`);
  announce("Atlas strategy readback and later trial completed. Use a fresh CLI process for cross-process proof.");
}

function renderStatus(data, quiet = false, preserveCurrentTrial = false) {
  renderActive(data.active);
  setPhase(data.phase);
  renderTrialHistory(data);
  renderBlockedTask(data.blocked_task);
  if (!preserveCurrentTrial) {
    const latest = data.latest_unsafe_trial || data.latest_safe_trial;
    if (latest) {
      renderCandidate(latest.candidate);
      // Status returns persisted gate evidence and recomputed individual
      // decisions for the recorded demonstration call when one is available.
      renderTrialDetails(latest.trial, latest.oracle_decision, latest.candidate_decision, latest.call);
    } else {
      renderCandidate(null);
      renderTrialDetails(null);
    }
  }
  if (data.comparison) {
    renderComparison(data.comparison, true);
  } else if (!quiet) {
    state.compared = false;
    const host = byId("comparison");
    host.classList.remove("is-unavailable");
    host.textContent = "No evaluation has been loaded.";
    setPhase(state.phase);
  }
  connection("Atlas state loaded", "connected");
  if (!quiet) announce(data.initialized ? `Run ${value(data.run_id)} loaded from Atlas.` : `Run ${value(data.run_id)} has not started. The safe workflow will initialize it.`);
}

function runId() {
  const id = byId("run-id").value.trim();
  if (!id) throw new Error("Enter a run ID first.");
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(id)) throw new Error("Run ID must use letters, numbers, underscores, dots, or hyphens, and be at most 80 characters.");
  return id;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Accept": "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) },
    cache: "no-store",
    ...options,
  });
  let result;
  try { result = await response.json(); } catch (_) { result = null; }
  if (!response.ok) {
    const detail = typeof result?.detail === "string" ? result.detail : `HTTP ${response.status}`;
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return result;
}

async function loadStatus(quiet = false, preserveCurrentTrial = false) {
  const id = runId();
  if (!quiet) setBusy(true);
  try {
    const data = await api(`/api/status?run_id=${encodeURIComponent(id)}`);
    renderStatus(data, quiet, preserveCurrentTrial);
    if (!quiet) logEvent(`Run ${id} reloaded from Atlas.`);
  } catch (error) {
    connection("Atlas state unavailable", "error");
    announce(`Could not load run: ${error.message}`, true);
  } finally {
    if (!quiet) setBusy(false);
  }
}

async function runAction(action) {
  let id;
  try { id = runId(); } catch (error) { announce(error.message, true); return; }
  setBusy(true);
  announce(`Running ${action.replace("-", " ")} against the backend…`);
  try {
    const data = await api(`/api/${action}`, { method: "POST", body: JSON.stringify({ run_id: id }) });
    if (action === "run-safe" || action === "run-unsafe") {
      renderTrialAction(data, action === "run-safe" ? "safe" : "unsafe");
      await loadStatus(true, true);
    } else if (action === "compare") {
      renderComparison(data);
    } else if (action === "resume") {
      renderResume(data);
      await loadStatus(true);
    }
  } catch (error) {
    const message = `${action.replace("-", " ")} unavailable: ${error.message}`;
    announce(message, true);
    if (action === "compare" || action === "resume") {
      const host = byId(action === "compare" ? "comparison" : "resume-result");
      host.classList.add("is-unavailable");
      host.textContent = error.status === 503 ? "The backend has no genuine result for this step yet." : message;
    }
    logEvent(message);
  } finally {
    setBusy(false);
  }
}

byId("refresh-button").addEventListener("click", () => loadStatus());
byId("safe-button").addEventListener("click", () => runAction("run-safe"));
byId("unsafe-button").addEventListener("click", () => runAction("run-unsafe"));
byId("compare-button").addEventListener("click", () => runAction("compare"));
byId("resume-button").addEventListener("click", () => runAction("resume"));
byId("run-id").addEventListener("change", () => {
  state.compared = false;
  state.resumed = false;
  const readback = byId("resume-result");
  readback.classList.remove("is-unavailable");
  readback.textContent = "No reuse result has been loaded.";
  loadStatus();
});
loadStatus();
