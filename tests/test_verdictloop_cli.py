"""CLI integration against the real Judge and an in-memory Atlas boundary."""

from __future__ import annotations

from verdictloop.cli import init_run, run_mvp
from verdictloop.store import CaseLawStore
from tests.test_verdictloop_store import FakeDatabase


def test_mvp_promotes_safe_and_persists_rejected_unsafe_trial() -> None:
    store = CaseLawStore(FakeDatabase())
    init_run(store, "integrated-demo", seed=17)
    result = run_mvp(store, "integrated-demo")

    assert result.approved_oracle_decision is True
    assert result.initial_policy_decision is False
    assert result.unapproved_oracle_decision is False
    assert result.unsafe_policy_decision is True
    assert result.safe_trial.full_gate_result.passed
    assert result.safe_trial.verdict == "PROMOTE"
    assert not result.unsafe_trial.full_gate_result.passed
    assert result.unsafe_trial.verdict == "REJECT"
    for trial in (result.safe_trial, result.unsafe_trial):
        fast = trial.fast_probe_result
        assert fast is not None
        assert fast.seed == 17
        assert fast.budget == 8
        assert fast.decisions_used == len(fast.observations) == 8
        assert fast.strategy_id == trial.strategy_id
        assert fast.candidate_hash == trial.full_gate_result.candidate_hash
        assert fast.fixture_hash == trial.fixture_hash
        assert fast.universe_hash == trial.universe_hash
        assert [observation.index for observation in fast.observations] == list(range(1, 9))
    assert len(store.trials.documents) == 2
    assert all(row.get("fast_probe_result") is not None for row in store.trials.documents.values())
    active = store.load_active_state("integrated-demo")
    assert active.policy.version == 2
    assert active.state.revision == 1
    assert active.state.objective_metrics["approved_restart_allowed"] is True
