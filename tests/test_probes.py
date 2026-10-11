import pytest

from caselaw import fleet, probes
from caselaw.probes import UNIFORM, candidate_universe, probe, universe_hash
from caselaw.schema import PROBE_BUDGET

from tests.conftest import make_policy, make_rule


def _boom(*a, **k):
    raise AssertionError("universe construction must be syntactic")


def test_universe_does_not_call_oracle_or_decide(fx, v1, no_approval, monkeypatch):
    monkeypatch.setattr("caselaw.oracle.oracle", _boom)
    monkeypatch.setattr("caselaw.policy.decide", _boom)
    monkeypatch.setattr(probes.policy_mod, "decide", _boom)
    assert candidate_universe(v1, no_approval, fx)


def test_universe_is_stable_and_hashed(fx, v1, safe):
    a, b = candidate_universe(v1, safe, fx), candidate_universe(v1, safe, fx)
    assert [c.case_id for c in a] == [c.case_id for c in b]
    assert universe_hash(a) == universe_hash(b)
    assert len({c.case_id for c in a}) == len(a)


def test_raw_label_rule_includes_all_labels_in_scope(fx, v1, raw_label):
    labels = {c.env_label for c in candidate_universe(v1, raw_label, fx)}
    assert labels == {"staging", "stg", "production", "prod-us"}


def test_old_and_new_scopes_both_included(fx):
    old = make_policy(fx, make_rule(tenants=("acme",)))
    new = make_policy(fx, make_rule(tenants=("globex",)))
    scopes = {(c.tenant, c.service) for c in candidate_universe(old, new, fx)}
    assert scopes == {("acme", "checkout"), ("globex", "checkout")}


def test_wildcard_expansion(fx, v1, tenant_wide):
    scopes = {(c.tenant, c.service) for c in candidate_universe(v1, tenant_wide, fx)}
    assert scopes == {(t, "checkout") for t in fleet.TENANTS}


def test_cross_tenant_contexts_remain_distinct(fx, v1, tenant_wide):
    u = candidate_universe(v1, tenant_wide, fx)
    same_label = [c for c in u if c.env_label == "staging" and c.actor == "alice"
                  and c.approval_context_id == fleet.APPROVED_CTX
                  and c.incident_id == fleet.APPROVED_INCIDENT and c.eval_time == fleet.VALID_TIME]
    assert len(same_label) == len(fleet.TENANTS)
    assert len({c.case_id for c in same_label}) == len(fleet.TENANTS)


def test_unbounded_diff_expands_to_full_fixture(fx, v1):
    unknown = make_policy(fx, make_rule(tenants=("acme", "unknown-tenant")))
    scopes = {(c.tenant, c.service) for c in candidate_universe(v1, unknown, fx)}
    assert scopes == {(r.tenant, r.service) for r in fx.resources}


def test_identical_policies_give_empty_universe(fx, safe):
    assert candidate_universe(safe, safe, fx) == []


def test_uniform_probe_budget_and_records(fx, v1, no_approval):
    u = candidate_universe(v1, no_approval, fx)
    r = probe(no_approval, u, UNIFORM, PROBE_BUDGET, seed=17, fixture=fx)
    assert r.decisions_used == PROBE_BUDGET
    assert [o.index for o in r.observations] == list(range(1, 9))
    assert len({o.case_id for o in r.observations}) == PROBE_BUDGET
    assert all(o.universe_hash == universe_hash(u) == r.universe_hash for o in r.observations)
    assert all(o.fixture_hash == fx.fixture_hash for o in r.observations)
    assert all(o.resource_id for o in r.observations)


def test_uniform_probe_is_seeded(fx, v1, no_approval):
    u = candidate_universe(v1, no_approval, fx)
    a = probe(no_approval, u, UNIFORM, 8, seed=29, fixture=fx)
    b = probe(no_approval, u, UNIFORM, 8, seed=29, fixture=fx)
    c = probe(no_approval, u, UNIFORM, 8, seed=43, fixture=fx)
    assert [o.case_id for o in a.observations] == [o.case_id for o in b.observations]
    assert [o.case_id for o in a.observations] != [o.case_id for o in c.observations]


def test_budget_is_capped_at_eight(fx, v1, no_approval):
    u = candidate_universe(v1, no_approval, fx)
    assert probe(no_approval, u, UNIFORM, 50, seed=1, fixture=fx).decisions_used == 8


def test_small_universe_uses_every_call(fx, no_approval):
    u = candidate_universe(make_policy(fx), no_approval, fx)[:3]
    assert probe(no_approval, u, UNIFORM, 8, seed=1, fixture=fx).decisions_used == 3
