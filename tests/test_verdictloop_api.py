"""HTTP contract exercised against B's real Judge and an in-memory Atlas boundary."""

from __future__ import annotations

from fastapi.testclient import TestClient

from verdictloop.api import app, get_store
from verdictloop.cli import init_run, run_mvp
from verdictloop.store import CaseLawStore
from tests.test_verdictloop_store import FakeDatabase


def test_safe_then_unsafe_routes_use_real_gate_and_persisted_state() -> None:
    store = CaseLawStore(FakeDatabase())
    app.dependency_overrides[get_store] = lambda: store
    try:
        with TestClient(app) as client:
            initial = client.get("/api/status", params={"run_id": "api-demo"})
            assert initial.status_code == 200
            assert initial.json()["phase"] == "not_started"

            premature = client.post("/api/run-unsafe", json={"run_id": "api-demo"})
            assert premature.status_code == 409
            assert store.runs.find_one({"_id": "api-demo"}) is None

            safe = client.post("/api/run-safe", json={"run_id": "api-demo"})
            assert safe.status_code == 200, safe.text
            safe_body = safe.json()
            assert safe_body["phase"] == "safe_promoted"
            assert safe_body["candidate"]["producer"] == "deterministic_builder"
            assert safe_body["candidate"]["added_rules"]
            assert safe_body["candidate"]["changed_rules"] == []
            assert safe_body["trial"]["verdict"] == "PROMOTE"
            assert safe_body["trial"]["passed"] is True
            assert safe_body["trial"]["full_sweep_count"] > 0
            assert safe_body["trial"]["universe_hash"]
            assert safe_body["oracle_decision"] is True
            assert safe_body["before_decision"] is False
            assert safe_body["candidate_decision"] is True
            assert safe_body["active"]["policy_version"] == 2
            assert safe_body["active"]["revision"] == 1

            persisted_safe = client.get("/api/status", params={"run_id": "api-demo"}).json()
            assert persisted_safe["latest_safe_trial"]["trial"]["trial_id"] == safe_body["trial"]["trial_id"]
            assert persisted_safe["blocked_task"]["initial_policy_decision"] is False
            assert persisted_safe["blocked_task"]["active_policy_decision"] is True

            unsafe = client.post("/api/run-unsafe", json={"run_id": "api-demo"})
            assert unsafe.status_code == 200, unsafe.text
            unsafe_body = unsafe.json()
            assert unsafe_body["phase"] == "unsafe_rejected"
            assert unsafe_body["candidate"]["mutation_class"] == "approval_removed"
            assert unsafe_body["candidate"]["added_rules"]
            assert unsafe_body["trial"]["verdict"] == "REJECT"
            assert unsafe_body["trial"]["passed"] is False
            assert "UNAUTHORIZED_GRANT" in unsafe_body["trial"]["failure_classes"]
            assert unsafe_body["trial"]["counterexamples"]
            assert unsafe_body["trial"]["evidence_case_ids"]
            assert unsafe_body["oracle_decision"] is False
            assert unsafe_body["before_decision"] is False
            assert unsafe_body["candidate_decision"] is True
            assert unsafe_body["active"] == safe_body["active"]

            persisted_unsafe = client.get("/api/status", params={"run_id": "api-demo"}).json()
            assert len(persisted_unsafe["trials"]) == 2
            assert persisted_unsafe["latest_unsafe_trial"]["trial"]["trial_id"] == unsafe_body["trial"]["trial_id"]
            assert persisted_unsafe["latest_unsafe_trial"]["call"] is not None
            assert persisted_unsafe["latest_unsafe_trial"]["oracle_decision"] is False
            assert persisted_unsafe["latest_unsafe_trial"]["candidate_decision"] is True
            assert len(store.trials.documents) == 2
            assert store.load_active_state("api-demo").state.active_policy_id == safe_body["active"]["policy_id"]
    finally:
        app.dependency_overrides.clear()


def test_compare_resume_persist_real_scorecard_and_unseen_later_trial() -> None:
    db = FakeDatabase()
    app.dependency_overrides[get_store] = lambda: CaseLawStore(db)
    try:
        with TestClient(app) as client:
            run_id = "learning-demo"
            assert client.post("/api/run-safe", json={"run_id": run_id}).status_code == 200
            assert client.post("/api/run-unsafe", json={"run_id": run_id}).status_code == 200
            premature = client.post("/api/resume", json={"run_id": run_id})
            assert premature.status_code == 409

            compared = client.post("/api/compare", json={"run_id": run_id})
            assert compared.status_code == 200, compared.text
            comparison = compared.json()
            card = comparison["scorecard"]
            assert comparison["evaluation_duration_ms"] > 0
            assert len(card["candidates"]) == 5
            assert len(card["heldout_seeds"]) == 5
            assert card["budget"] == 8
            accounting = comparison["decision_accounting"]
            assert accounting["uniform_fast_policy_decisions"] == 200
            assert accounting["learned_fast_policy_decisions"] == 200
            assert accounting["full_gate_authorization_evaluations"] == card["full_gate_decisions"]
            assert len(db["evaluations"].documents) == 1
            repeated = client.post("/api/compare", json={"run_id": run_id})
            assert repeated.status_code == 200
            assert repeated.json()["evaluation_id"] == comparison["evaluation_id"]
            assert repeated.json()["evaluation_duration_ms"] == comparison["evaluation_duration_ms"]

        # A new store instance reads the persisted evidence and active pointer.
        with TestClient(app) as restarted_client:
            resumed = restarted_client.post("/api/resume", json={"run_id": run_id})
            assert resumed.status_code == 200, resumed.text
            payload = resumed.json()
            assert payload["promoted"] is True
            assert payload["active"]["strategy_id"] == payload["selected_strategy"]["strategy_id"]
            assert payload["active"]["revision"] == 2
            later = payload["later_trial"]
            assert later["strategy_id"] == payload["active"]["strategy_id"]
            assert later["mutation_seed"] == 101
            assert later["probe_seed"] == 113
            assert later["decisions_used"] == 8
            assert len(later["observations"]) == 8
            assert later["universe_hash"]
            assert later["full_gate_verdict"] in {"PROMOTE", "REJECT"}
            stored_later = db["policies"].find_one({"_id": f"candidate:{later['candidate_id']}"})
            frozen_hashes = {row["content_hash"] for row in card["manifest"]["candidates"]}
            assert stored_later["content_hash"] not in frozen_hashes
            assert len(db["trials"].documents) == 3
            status = restarted_client.get("/api/status", params={"run_id": run_id})
            assert status.status_code == 200
            assert status.json()["comparison"]["evaluation_id"] == comparison["evaluation_id"]
            assert status.json()["active"]["strategy_id"] == payload["active"]["strategy_id"]
    finally:
        app.dependency_overrides.clear()


def test_invalid_run_id_is_rejected() -> None:
    store = CaseLawStore(FakeDatabase())
    app.dependency_overrides[get_store] = lambda: store
    try:
        with TestClient(app) as client:
            response = client.get("/api/status", params={"run_id": "../bad"})
            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_two_full_mvp_runs_share_atlas_without_mutation_id_collision() -> None:
    store = CaseLawStore(FakeDatabase())
    outcomes = []
    for run_id in ("first-run", "second-run"):
        init_run(store, run_id, seed=17)
        outcomes.append(run_mvp(store, run_id))
        active = store.load_active_state(run_id)
        assert active.policy.version == 2
        assert active.state.revision == 1

    first, second = outcomes
    assert first.unsafe_trial.verdict == second.unsafe_trial.verdict == "REJECT"
    assert first.unsafe_trial.candidate_policy_id != second.unsafe_trial.candidate_policy_id
    assert first.unsafe_trial.candidate_id != second.unsafe_trial.candidate_id
    assert first.unsafe_trial.full_gate_result.candidate_hash != second.unsafe_trial.full_gate_result.candidate_hash
    assert len(store.trials.documents) == 4
