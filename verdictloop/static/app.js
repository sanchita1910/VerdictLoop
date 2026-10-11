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
  const safeDone = state.phase === "safe_promoted" || state.phase === "unsafe_rejected";
  byId("safe-button").disabled = safeDone;
  byId("unsafe-button").disabled = state.phase !== "safe_promoted";
  byId("compare-button").disabled = !safeDone || state.compared;
  byId("resume-button").disabled = !state.compared;
  byId("refresh-button").disabled = false;
  put("unsafe-help", state.phase === "unsafe_rejected" ?
    "This dangerous change was tested and rejected. Use a new run name to try again." :
    safeDone ? "The safe policy is active. Now test what happens if approval checks are removed." :
      "First approve the safe change. Then test a rule that removes approval checks.");
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

function renderOutcomeCard(kind, record) {
  const trial = record?.trial;
  const card = byId(`${kind}-card`);
  const technical = byId(`${kind}-technical`);
  const witness = byId(`${kind}-witness`);
  card.classList.toggle("has-result", Boolean(trial));
  card.classList.toggle("is-promote", trial?.verdict === "PROMOTE");
  card.classList.toggle("is-reject", trial?.verdict === "REJECT");
  technical.replaceChildren();
  witness.hidden = true;
  witness.textContent = "";
  if (!trial) {
    put(`${kind}-verdict`, "Not tested yet");
    put(`${kind}-explanation`, kind === "safe" ?
      "Test the safe change to see the actual Judge decision." :
      "The dangerous test unlocks after the safe policy is active.");
    put(`${kind}-proof`, "No saved trial for this run.");
    return;
  }

  const safe = kind === "safe";
  const approvedRestart = safe && trial.verdict === "PROMOTE" &&
    record.call?.tool === "restart_service" && record.oracle_decision === true &&
    record.before_decision === false && record.candidate_decision === true;
  const unauthorizedRestart = !safe && trial.verdict === "REJECT" &&
    record.call?.tool === "restart_service" && record.oracle_decision === false &&
    record.candidate_decision === true && trial.failure_classes?.includes("UNAUTHORIZED_GRANT");
  put(`${kind}-verdict`, safe && trial.verdict === "PROMOTE" ? "APPROVED (PROMOTED)" :
    !safe && trial.verdict === "REJECT" ? "REJECTED" : value(trial.verdict));
  put(`${kind}-explanation`, approvedRestart ? "An approved server restart is now allowed." :
    unauthorizedRestart ? "This rule would allow unauthorized server restarts." :
      "Review the saved decision and evidence below.");
  const checks = Number.isInteger(trial.full_sweep_count) ? `${trial.full_sweep_count} checks` : "a full safety check";
  const examples = Array.isArray(trial.counterexamples) ? trial.counterexamples.length : 0;
  put(`${kind}-proof`, safe ?
    `Saved evidence: ${checks}; ${value(trial.regression_count, "—")} regressions.` :
    `Saved evidence: ${checks}; ${examples} recorded counterexamples.`);
  if (approvedRestart) {
    witness.textContent = `Saved call ${value(record.call.case_id)}: the policy changed DENY → ALLOW; the independent check says ALLOW.`;
    witness.hidden = false;
  } else if (!safe) {
    const example = trial.counterexamples?.find((item) =>
      item.failure_class === "UNAUTHORIZED_GRANT" && item.policy_decisions?.[0] === true &&
      item.oracle_decisions?.[0] === false && item.case_ids?.[0]);
    if (example) {
      witness.textContent = `Saved example ${example.case_ids[0]}: proposed rule ALLOW; independent check DENY.`;
      witness.hidden = false;
    }
  }

  const note = document.createElement("p");
  note.textContent = "Trial IDs, policy rules, hashes, and full counterexamples are shown below. Raw quick-probe decisions are stored in Atlas; traced later trials can also be inspected in Workshop.";
  const raw = document.createElement("pre");
  raw.textContent = JSON.stringify(record, null, 2);
  technical.append(note, raw);
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
  renderOutcomeCard(kind, data);
  const kindLabel = kind === "safe" ? "Safe candidate" : "Approval-removal candidate";
  logEvent(`${kindLabel}: ${value(trial.verdict)}. Trial ${value(trial.trial_id)} persisted.`);
  announce(`${kindLabel} received a real ${value(trial.verdict)} verdict. Active Atlas state reloaded.`);
}

function renderComparison(data, quiet = false) {
  const card = data.scorecard || data.comparison || data;
  const host = byId("comparison");
  host.classList.remove("is-unavailable");
  host.replaceChildren();
  const baseline = card.summary?.baseline;
  const learned = card.summary?.learned;
  if (!baseline || !learned) {
    const unavailable = document.createElement("p");
    unavailable.textContent = "This saved evaluation has no comparison summary to display.";
    host.append(unavailable);
    const details = document.createElement("details");
    const title = document.createElement("summary");
    title.textContent = "Technical details";
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(card, null, 2);
    details.append(title, pre);
    host.append(details);
  } else {
    const label = document.createElement("p");
    label.className = "comparison-label";
    label.textContent = `Small synthetic held-out evaluation · same ${value(card.budget)}-decision limit`;
    host.append(label);
    const grid = document.createElement("div");
    grid.className = "learning-grid";
    for (const [name, summary, className] of [
      ["Before learning", baseline, "before"], ["After learning", learned, "after"],
    ]) {
      const box = document.createElement("div");
      box.className = `learning-card ${className}`;
      const heading = document.createElement("h3");
      heading.textContent = name;
      const rate = document.createElement("strong");
      rate.textContent = `${value(summary.distinct_violating_detected)}/${value(summary.distinct_violating_total)}`;
      const description = document.createElement("p");
      description.textContent = "dangerous candidates detected";
      const bar = document.createElement("div");
      bar.className = "learning-bar";
      bar.setAttribute("aria-hidden", "true");
      const fill = document.createElement("span");
      const total = Number(summary.distinct_violating_total);
      const detected = Number(summary.distinct_violating_detected);
      fill.style.width = Number.isFinite(total) && total > 0 && Number.isFinite(detected) ?
        `${Math.max(0, Math.min(100, detected / total * 100))}%` : "0%";
      bar.append(fill);
      const secondary = document.createElement("p");
      secondary.className = "learning-secondary";
      secondary.textContent = `${value(summary.violating_trials_detected)}/${value(summary.violating_trials_total)} violating trials found`;
      box.append(heading, rate, description, bar, secondary);
      grid.append(box);
    }
    host.append(grid);
    const details = document.createElement("details");
    const title = document.createElement("summary");
    title.textContent = "Technical details";
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(data, null, 2);
    details.append(title, pre);
    host.append(details);
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
  const trial = data.later_trial;
  const blindSpot = trial?.first_violation_index === "NOT FOUND" &&
    trial.full_gate_verdict === "REJECT" &&
    trial.full_gate_failure_classes?.includes("ALIAS_INCONSISTENCY");
  const intro = document.createElement("p");
  intro.className = blindSpot ? "blind-spot-callout" : "comparison-note";
  intro.textContent = blindSpot ?
    "The quick test missed this danger. The full safety check rejected it." :
    trial ? "A later candidate was tested using the Atlas-loaded strategy." :
      "The active strategy was read from Atlas. No later trial was returned.";
  host.append(intro);
  const summary = document.createElement("dl");
  summary.className = "readback-grid";
  const fields = [
    ["Fast probe", trial?.first_violation_index],
    ["Full gate", trial?.full_gate_verdict],
    ["Failure", trial?.full_gate_failure_classes?.join(", ")],
    ["Active Atlas strategy", data.active?.strategy_id],
  ];
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
  const details = document.createElement("details");
  const title = document.createElement("summary");
  title.textContent = "Technical details";
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
  renderOutcomeCard("safe", data.latest_safe_trial);
  renderOutcomeCard("unsafe", data.latest_unsafe_trial);
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
  try { sessionStorage.setItem("verdictloop.run_id", id); } catch (_) { /* Optional browser convenience. */ }
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
      const kind = action === "run-safe" ? "safe" : "unsafe";
      renderTrialAction(data, kind);
      const narrow = window.matchMedia("(max-width: 680px)").matches;
      (narrow ? byId(`${kind}-card`) : byId("outcomes")).scrollIntoView({
        block: narrow ? "center" : "end",
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      });
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

try {
  const savedRunId = sessionStorage.getItem("verdictloop.run_id");
  if (savedRunId && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(savedRunId)) byId("run-id").value = savedRunId;
} catch (_) { /* Atlas remains the source of truth when browser storage is unavailable. */ }

byId("refresh-button").addEventListener("click", () => loadStatus());
byId("safe-button").addEventListener("click", () => runAction("run-safe"));
byId("unsafe-button").addEventListener("click", () => runAction("run-unsafe"));
byId("compare-button").addEventListener("click", () => runAction("compare"));
byId("resume-button").addEventListener("click", () => runAction("resume"));
byId("run-id").addEventListener("change", () => {
  state.compared = false;
  state.resumed = false;
  renderOutcomeCard("safe", null);
  renderOutcomeCard("unsafe", null);
  byId("comparison").textContent = "Loading saved evaluation…";
  const readback = byId("resume-result");
  readback.classList.remove("is-unavailable");
  readback.textContent = "No reuse result has been loaded.";
  loadStatus();
});
loadStatus();
