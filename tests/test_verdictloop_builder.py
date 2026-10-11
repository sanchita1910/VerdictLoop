"""The Builder must propose an approved, narrow repair without mutating state."""

import pytest

from verdictloop.builder import BuilderInputError, propose_safe_policy
from verdictloop.fleet import APPROVED_CTX, APPROVED_INCIDENT, VALID_TIME, build_fixture
from verdictloop.judge import admit
from verdictloop.oracle import oracle
from verdictloop.policy import decide
from verdictloop.schema import Call, Policy


def _blocked_call() -> Call:
    return Call(
        actor="alice",
        tool="restart_service",
        tenant="acme",
        service="checkout",
        env_label="stg",
        incident_id=APPROVED_INCIDENT,
        approval_context_id=APPROVED_CTX,
        eval_time=VALID_TIME,
    )


def test_repair_is_narrow_deterministic_and_passes_full_gate() -> None:
    fixture = build_fixture()
    active = Policy(policy_id="v1", version=1, fixture_hash=fixture.fixture_hash)
    target = _blocked_call()
    assert oracle(target, fixture)
    assert not decide(active, target, fixture)

    candidate = propose_safe_policy(target, active, fixture)
    assert candidate == propose_safe_policy(target, active, fixture)
    assert candidate.producer == "deterministic_builder"
    assert candidate.parent_policy_id == active.policy_id
    assert candidate.content_hash == candidate.policy.content_hash
    assert candidate.policy.version == 2
    assert candidate.policy.rules[0].selector.field == "env_canonical"
    assert candidate.policy.rules[0].require_valid_approval
    assert decide(candidate.policy, target, fixture)
    assert decide(candidate.policy, target.model_copy(update={"env_label": "staging"}), fixture)

    for change in (
        {"tenant": "globex"},
        {"service": "billing"},
        {"env_label": "production"},
        {"actor": "bob"},
        {"approval_context_id": "ctx-none"},
        {"incident_id": "INC-2"},
        {"eval_time": 300},
    ):
        assert not decide(candidate.policy, target.model_copy(update=change), fixture)
    gate = admit(active, candidate.policy, target, fixture)
    assert gate.passed and gate.verdict == "PROMOTE"
    assert gate.full_sweep_count > 0


def test_builder_rejects_ineligible_input() -> None:
    fixture = build_fixture()
    active = Policy(policy_id="v1", version=1, fixture_hash=fixture.fixture_hash)
    target = _blocked_call()
    for call, policy, used_fixture in (
        (target.model_copy(update={"approval_context_id": "ctx-none"}), active, fixture),
        (target.model_copy(update={"tool": "other_tool"}), active, fixture),
        (target.model_copy(update={"env_label": "unknown"}), active, fixture),
        (target, active.model_copy(update={"fixture_hash": "mismatch"}), fixture),
        (target, active, fixture.model_copy(update={"fixture_hash": "mismatch"})),
    ):
        with pytest.raises(BuilderInputError):
            propose_safe_policy(call, policy, used_fixture)

    safe = propose_safe_policy(target, active, fixture)
    with pytest.raises(BuilderInputError, match="already allows"):
        propose_safe_policy(target, safe.policy, fixture)
