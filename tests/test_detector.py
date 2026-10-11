import pytest

from caselaw import fleet
from caselaw.detector import detect_probe_violations, first_violation_index
from caselaw.policy import decide
from caselaw.schema import FailureClass, ProbeObservation

from tests.conftest import make_call


def observe(policy, fx, calls):
    out = []
    for i, call in enumerate(calls, start=1):
        r = fleet.resolve(call, fx)
        out.append(ProbeObservation(
            index=i, case_id=call.case_id, call=call, decision=decide(policy, call, fx),
            resource_id=r.resource_id if r else None,
            approval_context_id=call.approval_context_id,
            fixture_hash=fx.fixture_hash, universe_hash="u",
        ))
    return out


def filler(n):
    """Distinct calls that every test policy denies and the oracle also denies."""
    return [
        make_call(actor="mallory", service=s, env_label=e)
        for s in fleet.SERVICES for e in ("production", "prod-us")
    ][:n]


def aliases(cxs):
    return [c for c in cxs if c.failure_class is FailureClass.ALIAS_INCONSISTENCY]


def test_valid_pair_detected_at_second_observation(fx, raw_label):
    calls = filler(2) + [make_call(env_label="stg")] + filler(6)[2:5] + [make_call(env_label="staging")]
    obs = observe(raw_label, fx, calls)
    found = aliases(detect_probe_violations(obs, fx))
    assert len(found) == 1
    assert found[0].first_evidence_index == 7
    assert found[0].policy_decisions == (False, True)


def test_no_sufficient_pair_is_not_found(fx, raw_label):
    obs = observe(raw_label, fx, filler(4) + [make_call(env_label="staging")])
    cxs = detect_probe_violations(obs, fx)
    assert cxs == []
    assert first_violation_index(cxs) is None


@pytest.mark.parametrize("field,value", [
    ("tenant", "globex"), ("service", "billing"), ("actor", "bob"),
    ("incident_id", fleet.OTHER_INCIDENT), ("approval_context_id", fleet.UNAPPROVED_CTX),
    ("eval_time", fleet.EXPIRED_TIME),
])
def test_no_grouping_across_context_dimensions(fx, tenant_wide, raw_label, field, value):
    # Differ in env_label AND one other dimension: never an alias pair.
    calls = [make_call(env_label="staging"), make_call(env_label="stg", **{field: value})]
    for policy in (raw_label, tenant_wide):
        assert aliases(detect_probe_violations(observe(policy, fx, calls), fx)) == []


def test_no_grouping_across_tool(fx, raw_label):
    calls = [make_call(env_label="staging"), make_call(env_label="stg", tool="delete_service")]
    assert aliases(detect_probe_violations(observe(raw_label, fx, calls), fx)) == []


def test_same_raw_label_in_other_resource_not_equivalent(fx, raw_label):
    calls = [make_call(env_label="staging"), make_call(env_label="staging", service="billing")]
    assert aliases(detect_probe_violations(observe(raw_label, fx, calls), fx)) == []


def test_alias_inconsistency_is_not_automatically_a_grant(fx, raw_label):
    obs = observe(raw_label, fx, [make_call(env_label="staging"), make_call(env_label="stg")])
    cxs = detect_probe_violations(obs, fx)
    assert [c.failure_class for c in cxs] == [FailureClass.ALIAS_INCONSISTENCY]
    assert cxs[0].severity is FailureClass.INCORRECT_DENIAL


def test_unauthorized_grant_detected_on_single_call(fx, no_approval):
    obs = observe(no_approval, fx, [make_call(approval_context_id=fleet.UNAPPROVED_CTX)])
    cxs = detect_probe_violations(obs, fx)
    assert [c.failure_class for c in cxs] == [FailureClass.UNAUTHORIZED_GRANT]
    assert cxs[0].first_evidence_index == 1


def test_first_evidence_always_in_range(fx, no_approval, raw_label, v1):
    from caselaw.probes import UNIFORM, candidate_universe, probe
    for cand in (no_approval, raw_label):
        u = candidate_universe(v1, cand, fx)
        for seed in (17, 29, 43, 71, 97):
            cxs = detect_probe_violations(list(probe(cand, u, UNIFORM, 8, seed, fixture=fx).observations), fx)
            idx = first_violation_index(cxs)
            assert idx is None or 1 <= idx <= 8
