from caselaw import fleet
from caselaw.oracle import oracle
from caselaw.policy import decide
from caselaw.schema import Selector

from tests.conftest import make_call, make_policy, make_rule


def test_empty_v1_denies(fx, v1, target):
    assert decide(v1, target, fx) is False
    assert oracle(target, fx) is True  # legitimate but blocked


def test_safe_canonical_staging_rule_with_approval_allows(fx, safe, target):
    assert decide(safe, target, fx) is True
    assert decide(safe, make_call(env_label="stg"), fx) is True


def test_same_rule_without_valid_approval_denies(fx, safe):
    assert decide(safe, make_call(approval_context_id=fleet.UNAPPROVED_CTX), fx) is False
    assert decide(safe, make_call(eval_time=fleet.EXPIRED_TIME), fx) is False
    assert decide(safe, make_call(incident_id=fleet.OTHER_INCIDENT), fx) is False


def test_raw_label_selector_differs_across_aliases(fx, raw_label):
    assert decide(raw_label, make_call(env_label="staging"), fx) is True
    assert decide(raw_label, make_call(env_label="stg"), fx) is False


def test_invalid_policy_schema_fails_closed(fx, target):
    bad = {"policy_id": "x", "version": 2, "fixture_hash": fx.fixture_hash,
           "rules": [{"rule_id": "r", "tools": ["*"], "tenants": ["*"], "services": ["*"],
                      "effect": "deny"}]}
    assert decide(bad, target, fx) is False
    assert decide({"policy_id": "x", "rules": "eval('1')"}, target, fx) is False


def test_wrong_fixture_hash_fails_closed(fx, target):
    p = make_policy(fx, make_rule()).model_copy(update={"fixture_hash": "deadbeef"})
    assert decide(p, target, fx) is False
    tampered = fx.model_copy(update={"eval_times": (1,)})
    assert decide(make_policy(fx, make_rule()), target, tampered) is False


def test_production_allowed_when_correctly_approved(fx):
    prod = make_policy(fx, make_rule(selector=Selector(field="env_canonical", values=("production",))))
    assert decide(prod, make_call(env_label="prod-us"), fx) is True
    assert decide(prod, make_call(env_label="prod-us", approval_context_id=fleet.UNAPPROVED_CTX), fx) is False


def test_wrong_tenant_and_service_denied(fx, safe):
    assert decide(safe, make_call(actor="bob", tenant="globex"), fx) is False
    assert decide(safe, make_call(service="billing"), fx) is False


def test_unresolvable_label_denied(fx, tenant_wide):
    assert decide(tenant_wide, make_call(env_label="nonexistent"), fx) is False
