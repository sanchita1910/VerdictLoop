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
    assert len(store.trials.documents) == 2
    active = store.load_active_state("integrated-demo")
    assert active.policy.version == 2
    assert active.state.revision == 1
    assert active.state.objective_metrics["approved_restart_allowed"] is True
