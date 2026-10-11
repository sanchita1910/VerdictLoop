"""Atlas store tests against B's real finite-fixture Judge and in-memory CAS."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from verdictloop.fleet import APPROVED_CTX, APPROVED_INCIDENT, RESTART, VALID_TIME, build_fixture
from verdictloop.judge import admit
from verdictloop.schema import Call, Candidate, Policy, PolicyRule, Selector, Strategy
from verdictloop.store import (
    ActiveRunState,
    CaseLawStore,
    FixtureMismatchError,
    MissingReferenceError,
    PromotionEvidenceError,
    StalePromotionError,
    Trial,
)


class FakeCollection:
    def __init__(self):
        self.documents = {}

    def insert_one(self, document):
        if document["_id"] in self.documents:
            raise ValueError("duplicate _id")
        self.documents[document["_id"]] = deepcopy(document)
        return SimpleNamespace(inserted_id=document["_id"])

    def find_one(self, query):
        return next(
            (deepcopy(doc) for doc in self.documents.values()
             if all(doc.get(k) == v for k, v in query.items())),
            None,
        )

    def find(self, query):
        return [
            deepcopy(doc) for doc in self.documents.values()
            if all(doc.get(k) == v for k, v in query.items())
        ]

    def update_one(self, query, change):
        for doc in self.documents.values():
            if all(doc.get(k) == v for k, v in query.items()):
                doc.update(deepcopy(change.get("$set", {})))
                for key, value in change.get("$inc", {}).items():
                    doc[key] += value
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)


class FakeDatabase:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection())

    def command(self, name):
        assert name == "ping"
        return {"ok": 1}


def _fixture_setup():
    fixture = build_fixture()
    base = Policy(policy_id="policy-v1", version=1, fixture_hash=fixture.fixture_hash)
    strategy = Strategy(strategy_id="uniform-v1", alias_decisions=0, budget=8)
    store = CaseLawStore(FakeDatabase())
    store.save_fixture(fixture)
    store.save_approvals(fixture)
    store.save_policy(base)
    store.save_strategy(strategy, run_id="demo")
    store.initialize_run(ActiveRunState(
        run_id="demo", revision=0, active_policy_id=base.policy_id,
        active_strategy_id=strategy.strategy_id, fixture_hash=fixture.fixture_hash,
    ))
    return store, fixture, base, strategy


def _approved_call():
    return Call(
        actor="alice", tool=RESTART, tenant="acme", service="checkout",
        env_label="staging", incident_id=APPROVED_INCIDENT,
        approval_context_id=APPROVED_CTX, eval_time=VALID_TIME,
    )


def _safe_policy(fixture):
    return Policy(
        policy_id="policy-v2", version=2, fixture_hash=fixture.fixture_hash,
        rules=(PolicyRule(
            rule_id="r-safe", tools=(RESTART,), tenants=("acme",),
            services=("checkout",),
            selector=Selector(field="env_canonical", values=("staging",)),
            require_valid_approval=True,
        ),),
    )


def _candidate_trial(store, fixture, base, strategy):
    target = _approved_call()
    policy = _safe_policy(fixture)
    candidate = Candidate(
        candidate_id="candidate-safe", parent_policy_id=base.policy_id,
        policy=policy, mutation_class="safe", seed=17, split="demo",
        content_hash=policy.content_hash, triggering_target=target,
        producer="deterministic_builder",
    )
    gate = admit(base, policy, target, fixture)
    assert gate.passed and gate.verdict == "PROMOTE"
    trial = Trial(
        trial_id="trial-safe", run_id="demo", candidate_id=candidate.candidate_id,
        base_policy_id=base.policy_id, candidate_policy_id=policy.policy_id,
        strategy_id=strategy.strategy_id, fixture_hash=fixture.fixture_hash,
        universe_hash=gate.universe_hash, triggering_target=target,
        full_gate_result=gate, verdict=gate.verdict,
        counterexamples=gate.counterexamples,
    )
    store.save_policy(policy)
    store.save_candidate(candidate)
    store.save_trial(trial)
    return candidate, trial


def test_initialize_reload_and_policy_round_trip():
    store, fixture, base, strategy = _fixture_setup()
    active = store.load_active_state("demo")
    assert active.state.revision == 0
    assert active.policy == base
    assert active.strategy == strategy
    assert active.fixture == fixture
    assert store.ping() is True
    assert set(store.db.collections) == {
        "fleet", "approvals", "policies", "strategies", "trials", "evaluations", "runs"
    }


def test_real_full_gate_evidence_promotes_only_once():
    store, fixture, base, strategy = _fixture_setup()
    candidate, trial = _candidate_trial(store, fixture, base, strategy)
    promoted = store.promote_policy(candidate, trial, expected_revision=0)
    assert promoted.revision == 1
    assert promoted.active_policy_id == candidate.policy.policy_id
    assert promoted.objective_metrics["approved_restart_allowed"] is True
    with pytest.raises(StalePromotionError):
        store.promote_policy(candidate, trial, expected_revision=0)
    assert store.load_active_state("demo").policy == candidate.policy


def test_rejected_or_inconsistent_gate_cannot_promote():
    store, fixture, base, strategy = _fixture_setup()
    candidate, trial = _candidate_trial(store, fixture, base, strategy)
    bad_gate = trial.full_gate_result.model_copy(update={"target_outcome": False})
    bad_trial = trial.model_copy(update={"full_gate_result": bad_gate})
    store.save_trial(bad_trial.model_copy(update={"trial_id": "trial-bad"}))
    with pytest.raises(PromotionEvidenceError):
        store.promote_policy(candidate, bad_trial, expected_revision=0)
    assert store.load_active_state("demo").policy == base


def test_partial_sweep_cannot_promote_even_when_verdict_claims_success():
    store, fixture, base, strategy = _fixture_setup()
    candidate, trial = _candidate_trial(store, fixture, base, strategy)
    partial_gate = trial.full_gate_result.model_copy(
        update={"full_sweep_count": trial.full_gate_result.full_sweep_count - 1}
    )
    partial_trial = trial.model_copy(
        update={"trial_id": "trial-partial", "full_gate_result": partial_gate}
    )
    store.save_trial(partial_trial)
    with pytest.raises(PromotionEvidenceError, match="candidate universe"):
        store.promote_policy(candidate, partial_trial, expected_revision=0)
    assert store.load_active_state("demo").policy == base


def test_fixture_or_missing_reference_fails_closed():
    store, fixture, _, _ = _fixture_setup()
    store.approvals.documents.clear()
    with pytest.raises(FixtureMismatchError):
        store.load_active_state("demo")
    store.save_approvals(fixture)
    store.strategies.documents.clear()
    with pytest.raises(MissingReferenceError):
        store.load_active_state("demo")


def test_corrupted_approval_document_fails_fixture_hash():
    store, fixture, _, _ = _fixture_setup()
    key = next(iter(store.approvals.documents))
    store.approvals.documents[key]["actor"] = "mallory"
    with pytest.raises(FixtureMismatchError):
        store.load_fixture(fixture.fixture_hash)


def test_strategy_promotion_uses_persisted_eligibility_and_cas():
    store, fixture, _, baseline = _fixture_setup()
    learned = Strategy(strategy_id="alias-v2", alias_decisions=2, budget=8)
    store.save_strategy(learned, run_id="demo", version=2, parent_strategy_id=baseline.strategy_id)
    evaluation = {
        "evaluation_id": "eval-1", "run_id": "demo", "fixture_hash": fixture.fixture_hash,
        "strategy_id": learned.strategy_id, "promotion_eligible": True,
    }
    store.save_evaluation(evaluation)
    promoted = store.promote_strategy(learned, evaluation, expected_revision=0)
    assert promoted.active_strategy_id == learned.strategy_id and promoted.revision == 1
    with pytest.raises(StalePromotionError):
        store.promote_strategy(learned, evaluation, expected_revision=0)
