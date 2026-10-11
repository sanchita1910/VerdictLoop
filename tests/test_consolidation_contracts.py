"""Canonical integration contracts retained from the former caselaw tests."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import verdictloop.cli as cli
import verdictloop.mutations as mutations
from verdictloop.builder import propose_safe_policy
from verdictloop.fleet import build_fixture
from verdictloop.schema import Candidate, Policy, PolicyRule, Selector, Strategy
from verdictloop.store import CaseLawStore, StoreError
from tests.test_verdictloop_store import FakeDatabase


def test_two_runs_reuse_one_immutable_fixture_and_approvals() -> None:
    store = CaseLawStore(FakeDatabase())
    first = cli.init_run(store, "rehearsal-one", seed=17)
    second = cli.init_run(store, "rehearsal-two", seed=29)

    assert first.run_id != second.run_id
    assert first.fixture_hash == second.fixture_hash == build_fixture().fixture_hash
    assert len(store.fleet.documents) == 1
    assert len(store.approvals.documents) == len(build_fixture().approvals)
    assert len(store.runs.documents) == 2
    assert store.load_active_state(first.run_id).state == first
    assert store.load_active_state(second.run_id).state == second


@pytest.mark.parametrize(
    "failure", ["missing_candidate", "unsafe_gate_error", "unexpected_unsafe_pass"]
)
def test_unsafe_preflight_failure_keeps_policy_v1_and_no_trials(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    store = CaseLawStore(FakeDatabase())
    cli.init_run(store, "preflight", seed=17)

    if failure == "missing_candidate":
        monkeypatch.setattr(mutations, "mutate", lambda *args, **kwargs: [])
    else:
        original_admit = cli.admit
        calls = 0

        def admit_then_fail(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2 and failure == "unsafe_gate_error":
                raise RuntimeError("unsafe gate unavailable")
            if calls == 2:
                return original_admit(*args, **kwargs).model_copy(
                    update={"passed": True, "verdict": "PROMOTE"}
                )
            return original_admit(*args, **kwargs)

        monkeypatch.setattr(cli, "admit", admit_then_fail)

    with pytest.raises((RuntimeError, ValueError)):
        cli.run_mvp(store, "preflight")

    active = store.load_active_state("preflight")
    assert active.policy.version == 1
    assert active.state.revision == 0
    assert not store.trials.documents


def test_duplicate_policy_id_cannot_replace_immutable_content() -> None:
    store = CaseLawStore(FakeDatabase())
    state = cli.init_run(store, "immutable", seed=17)
    original = store.load_policy(state.active_policy_id)
    changed = original.model_copy(update={
        "rules": (PolicyRule(
            rule_id="new", tools=("restart_service",), tenants=("acme",),
            services=("checkout",), selector=Selector(field="env_canonical", values=("staging",)),
        ),),
    })
    with pytest.raises(StoreError):
        store.save_policy(changed)
    assert store.load_policy(original.policy_id) == original


@pytest.mark.parametrize(
    "invalid_update", [{"budget": 9}, {"alias_decisions": 3}]
)
def test_strategy_storage_revalidates_model_copy(
    invalid_update: dict[str, int]
) -> None:
    store = CaseLawStore(FakeDatabase())
    cli.init_run(store, "strategy", seed=17)
    baseline = store.load_active_state("strategy").strategy
    invalid = baseline.model_copy(update={"strategy_id": "invalid", **invalid_update})
    with pytest.raises((StoreError, ValueError)):
        store.save_strategy(invalid, run_id="strategy", version=2,
                            parent_strategy_id=baseline.strategy_id)
    assert len(store.strategies.documents) == 1


def test_atlas_ping_error_does_not_expose_uri_or_credentials() -> None:
    secret = "mongodb+srv://user:password@example.invalid"

    def broken_ping(_command: str) -> None:
        raise RuntimeError(secret)

    client = SimpleNamespace(admin=SimpleNamespace(command=broken_ping))
    store = CaseLawStore(FakeDatabase(), client=client)
    with pytest.raises(StoreError) as caught:
        store.ping()
    assert secret not in str(caught.value)
    assert "password" not in str(caught.value)


def test_approval_removal_selector_rejects_broad_added_rule() -> None:
    """An added approval-free rule must match the bounded safe rule shape."""
    fixture = build_fixture()
    base = Policy(policy_id="v1", version=1, fixture_hash=fixture.fixture_hash)
    target = cli._approved_checkout_call(fixture)
    safe = propose_safe_policy(target, base, fixture)
    variants = mutations.mutate(safe.policy, fixture, seed=17, split="train")
    actual = next(c for c in variants if c.mutation_class == mutations.APPROVAL_REMOVED)

    def classified(candidate: Candidate) -> bool:
        selector = cli._is_exact_approval_removal
        if len(inspect.signature(selector).parameters) == 3:
            return selector(safe.policy, candidate, fixture)
        return selector(safe.policy, candidate)

    assert classified(actual)

    broad_rule = PolicyRule(
        rule_id="wildcard-grant", tools=("restart_service",), tenants=("*",),
        services=("*",), selector=None, require_valid_approval=False,
    )
    broad_policy = Policy(
        policy_id="broad-v3", version=safe.policy.version + 1,
        fixture_hash=fixture.fixture_hash, rules=(*safe.policy.rules, broad_rule),
    )
    broad = Candidate(
        candidate_id="broad", parent_policy_id=safe.policy.policy_id,
        policy=broad_policy, mutation_class="approval_removed", seed=17,
        split="train", content_hash=broad_policy.content_hash,
        triggering_target=target,
    )
    assert not classified(broad)
