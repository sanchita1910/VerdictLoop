"""Small same-origin API over the real VerdictLoop Judge and Atlas evidence.

The UI is a view of persisted trials. These routes never manufacture a gate
verdict, comparison result, or active-pointer transition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from verdictloop.cli import (
    CandidateOutcome,
    _approved_checkout_call,
    _open_store,
    compare_run,
    init_run,
    load_run_evaluation,
    resume_run,
    run_safe,
    run_unsafe,
)
from verdictloop.oracle import oracle
from verdictloop.policy import decide
from verdictloop.probes import candidate_universe
from verdictloop.schema import Candidate, FailureClass, Fixture, Policy
from verdictloop.store import CaseLawStore, StoreError, Trial


app = FastAPI(title="VerdictLoop", version="0.1.0")
RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"
STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class RunRequest(BaseModel):
    run_id: str = Field(default="demo", pattern=RUN_ID_PATTERN)


def get_store() -> CaseLawStore:
    """A per-request Atlas connection; error text never includes URI details."""
    try:
        return _open_store()
    except StoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except Exception:
        raise HTTPException(status_code=503, detail="Atlas is unavailable") from None


StoreDependency = Annotated[CaseLawStore, Depends(get_store)]


def _active_payload(store: CaseLawStore, run_id: str) -> dict[str, Any]:
    active = store.load_active_state(run_id)
    return {
        "policy_id": active.policy.policy_id,
        "policy_version": active.policy.version,
        "strategy_id": active.strategy.strategy_id,
        "revision": active.state.revision,
        "fixture_hash": active.fixture.fixture_hash,
    }


def _candidate_payload(candidate: Candidate, base: Policy) -> dict[str, Any]:
    before = {rule.rule_id: rule for rule in base.rules}
    after = {rule.rule_id: rule for rule in candidate.policy.rules}
    return {
        "candidate_id": candidate.candidate_id,
        "producer": candidate.producer,
        "mutation_class": candidate.mutation_class,
        "split": candidate.split,
        "seed": candidate.seed,
        "parent_policy_id": candidate.parent_policy_id,
        "proposed_policy_id": candidate.policy.policy_id,
        "content_hash": candidate.content_hash,
        "added_rules": [rule.model_dump(mode="json") for rule_id, rule in after.items() if rule_id not in before],
        "removed_rules": [rule.model_dump(mode="json") for rule_id, rule in before.items() if rule_id not in after],
        "changed_rules": [
            {
                "rule_id": rule_id,
                "before": before[rule_id].model_dump(mode="json"),
                "after": after[rule_id].model_dump(mode="json"),
            }
            for rule_id in sorted(before.keys() & after.keys())
            if before[rule_id] != after[rule_id]
        ],
    }


def _trial_payload(trial: Trial) -> dict[str, Any]:
    gate = trial.full_gate_result
    return {
        "trial_id": trial.trial_id,
        "candidate_id": trial.candidate_id,
        "verdict": trial.verdict,
        "full_sweep_count": gate.full_sweep_count,
        "failure_classes": [failure.value for failure in gate.failure_classes],
        "counterexamples": [item.model_dump(mode="json") for item in gate.counterexamples],
        "evidence_case_ids": list(gate.evidence_case_ids),
        "universe_hash": gate.universe_hash,
        "target_outcome": gate.target_outcome,
        "regression_count": gate.regression_count,
        "passed": gate.passed,
        "created_at": trial.created_at.isoformat(),
    }


def _stored_trials(store: CaseLawStore, run_id: str) -> list[tuple[Trial, Candidate]]:
    trials = [Trial.model_validate({k: v for k, v in row.items() if k != "_id"})
              for row in store.trials.find({"run_id": run_id})]
    trials.sort(key=lambda trial: (trial.created_at, trial.trial_id))
    return [(trial, store.load_candidate(trial.candidate_id)) for trial in trials]


def _persisted_evidence_call(
    trial: Trial, candidate: Candidate, base: Policy, fixture: Fixture
) -> dict[str, Any] | None:
    """Reconstruct an actual gate case from immutable Judge evidence."""
    if trial.verdict == "PROMOTE":
        call = trial.triggering_target
    else:
        grant_ids = {
            case_id
            for counterexample in trial.full_gate_result.counterexamples
            if counterexample.failure_class == FailureClass.UNAUTHORIZED_GRANT
            for case_id in counterexample.case_ids
        }
        call = next(
            (item for item in candidate_universe(base, candidate.policy, fixture)
             if item.case_id in grant_ids and item.case_id in trial.full_gate_result.evidence_case_ids),
            None,
        )
    if call is None:
        return None
    truth = oracle(call, fixture)
    before = decide(base, call, fixture)
    after = decide(candidate.policy, call, fixture)
    if trial.verdict == "PROMOTE" and not (truth and after):
        raise StoreError("Persisted promotion call no longer matches the fixture")
    if trial.verdict == "REJECT" and not (not truth and after):
        raise StoreError("Persisted grant evidence no longer matches the fixture")
    return {
        "call": {"case_id": call.case_id, **call.model_dump(mode="json")},
        "oracle_decision": truth,
        "before_decision": before,
        "candidate_decision": after,
    }


def _status_payload(store: CaseLawStore, run_id: str) -> dict[str, Any]:
    if store.runs.find_one({"_id": run_id}) is None:
        return {
            "run_id": run_id,
            "initialized": False,
            "phase": "not_started",
            "active": None,
            "trials": [],
            "latest_safe_trial": None,
            "latest_unsafe_trial": None,
            "blocked_task": None,
            "comparison": None,
        }
    active = store.load_active_state(run_id)
    rows = _stored_trials(store, run_id)
    summaries = []
    for trial, candidate in rows:
        base = store.load_policy(trial.base_policy_id)
        evidence = _persisted_evidence_call(trial, candidate, base, active.fixture)
        summaries.append({
            "trial": _trial_payload(trial),
            "candidate": _candidate_payload(candidate, base),
            "call": evidence["call"] if evidence else None,
            "oracle_decision": evidence["oracle_decision"] if evidence else None,
            "before_decision": evidence["before_decision"] if evidence else None,
            "candidate_decision": evidence["candidate_decision"] if evidence else None,
        })
    safe = next((item for item in reversed(summaries)
                 if item["candidate"]["mutation_class"] == "safe_repair"
                 and item["trial"]["verdict"] == "PROMOTE"), None)
    unsafe = next((item for item in reversed(summaries)
                   if item["candidate"]["mutation_class"] == "approval_removed"
                   and item["trial"]["verdict"] == "REJECT"), None)
    promoted = safe is not None and active.policy.policy_id == safe["candidate"]["proposed_policy_id"]
    phase = "unsafe_rejected" if promoted and unsafe else "safe_promoted" if promoted else "initialized"
    target = _approved_checkout_call(active.fixture)
    blocked_task = {
        "call": {"case_id": target.case_id, **target.model_dump(mode="json")},
        "oracle_decision": oracle(target, active.fixture),
        "active_policy_decision": decide(active.policy, target, active.fixture),
    }
    if safe is not None:
        safe_base = store.load_policy(next(trial.base_policy_id for trial, candidate in rows
                                           if trial.trial_id == safe["trial"]["trial_id"]))
        blocked_task["initial_policy_decision"] = decide(safe_base, target, active.fixture)
    evaluation = load_run_evaluation(store, run_id, active.fixture.fixture_hash)
    comparison = _comparison_payload(store, run_id, evaluation) if evaluation else None
    return {
        "run_id": run_id,
        "initialized": True,
        "phase": phase,
        "active": _active_payload(store, run_id),
        "trials": summaries,
        "latest_safe_trial": safe,
        "latest_unsafe_trial": unsafe,
        "blocked_task": blocked_task,
        "comparison": comparison,
    }


def _outcome_payload(store: CaseLawStore, run_id: str, outcome: CandidateOutcome) -> dict[str, Any]:
    base = store.load_policy(outcome.trial.base_policy_id)
    status = _status_payload(store, run_id)
    return {
        "run_id": run_id,
        "phase": status["phase"],
        "active": status["active"],
        "candidate": _candidate_payload(outcome.candidate, base),
        "trial": _trial_payload(outcome.trial),
        "call": {"case_id": outcome.call.case_id, **outcome.call.model_dump(mode="json")},
        "oracle_decision": outcome.oracle_decision,
        "before_decision": outcome.before_decision,
        "candidate_decision": outcome.candidate_decision,
    }


def _run_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, StoreError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=500, detail="Workflow failed; internal details withheld")


def _comparison_payload(
    store: CaseLawStore, run_id: str, evaluation: dict[str, Any]
) -> dict[str, Any]:
    card = evaluation["scorecard"]
    baseline_fast = card["summary"]["baseline"]["decisions_used"]
    learned_fast = card["summary"]["learned"]["decisions_used"]
    full_gate_checks = card["full_gate_decisions"]
    return {
        "run_id": run_id,
        "evaluation_id": evaluation["evaluation_id"],
        "evaluation_duration_ms": evaluation["evaluation_duration_ms"],
        "promotion_eligible": evaluation["promotion_eligible"],
        "selected_strategy": card["selection"]["selected"],
        "selection_reason": card["selection"]["reason"],
        "decision_accounting": {
            "uniform_fast_policy_decisions": baseline_fast,
            "learned_fast_policy_decisions": learned_fast,
            "full_gate_authorization_evaluations": full_gate_checks,
            "all_heldout_authorization_evaluations": baseline_fast + learned_fast + full_gate_checks,
        },
        "scorecard": card,
        "active": _active_payload(store, run_id),
    }


@app.get("/api/status")
def status(
    store: StoreDependency,
    run_id: str = Query(default="demo", pattern=RUN_ID_PATTERN),
) -> dict[str, Any]:
    try:
        return _status_payload(store, run_id)
    except Exception as exc:
        raise _run_error(exc) from None


@app.post("/api/run-safe")
def post_run_safe(request: RunRequest, store: StoreDependency) -> dict[str, Any]:
    try:
        if store.runs.find_one({"_id": request.run_id}) is None:
            init_run(store, request.run_id, seed=17)
        outcome = run_safe(store, request.run_id)
        return _outcome_payload(store, request.run_id, outcome)
    except Exception as exc:
        raise _run_error(exc) from None


@app.post("/api/run-unsafe")
def post_run_unsafe(request: RunRequest, store: StoreDependency) -> dict[str, Any]:
    try:
        if store.runs.find_one({"_id": request.run_id}) is None:
            raise ValueError("Run the safe proposal before the unsafe mutation")
        outcome = run_unsafe(store, request.run_id)
        return _outcome_payload(store, request.run_id, outcome)
    except Exception as exc:
        raise _run_error(exc) from None


@app.post("/api/compare")
def post_compare(request: RunRequest, store: StoreDependency) -> dict[str, Any]:
    """Freeze and persist B's actual five-seed held-out scorecard once per run."""
    try:
        evaluation = compare_run(store, request.run_id)
        return _comparison_payload(store, request.run_id, evaluation)
    except Exception as exc:
        raise _run_error(exc) from None


@app.post("/api/resume")
def post_resume(request: RunRequest, store: StoreDependency) -> dict[str, Any]:
    """CAS-promote an eligible strategy and read it back from persisted state."""
    try:
        return resume_run(store, request.run_id)
    except Exception as exc:
        raise _run_error(exc) from None


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    path = STATIC_DIR / "index.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Demo UI is not installed")
    return FileResponse(path)
