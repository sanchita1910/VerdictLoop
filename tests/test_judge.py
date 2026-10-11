from caselaw import fleet
from caselaw.judge import admit
from caselaw.oracle import oracle
from caselaw.policy import decide
from caselaw.schema import FailureClass

from tests.conftest import make_call, make_policy, make_rule


def test_safe_candidate_promoted(fx, v1, safe, target):
    assert decide(v1, target, fx) is False and oracle(target, fx) is True
    r = admit(v1, safe, target, fx)
    assert r.passed and r.verdict == "PROMOTE"
    assert r.target_outcome is True
    assert r.failure_classes == () and r.regression_count == 0
    assert r.full_sweep_count > 0
    assert (r.candidate_hash, r.base_hash, r.fixture_hash) == (
        safe.content_hash, v1.content_hash, fx.fixture_hash)


def test_approval_removal_rejected_with_unauthorized_grant(fx, v1, no_approval, target):
    unapproved = make_call(approval_context_id=fleet.UNAPPROVED_CTX)
    assert oracle(unapproved, fx) is False and decide(no_approval, unapproved, fx) is True
    r = admit(v1, no_approval, target, fx)
    assert not r.passed and r.verdict == "REJECT"
    assert FailureClass.UNAUTHORIZED_GRANT in r.failure_classes
    assert unapproved.case_id in r.evidence_case_ids


def test_raw_label_rejected_for_alias_inconsistency(fx, v1, raw_label, target):
    r = admit(v1, raw_label, target, fx)
    assert r.verdict == "REJECT"
    assert FailureClass.ALIAS_INCONSISTENCY in r.failure_classes
    assert FailureClass.UNAUTHORIZED_GRANT not in r.failure_classes


def test_tenant_widening_with_exact_approval_is_negative_control(fx, v1, tenant_wide, target):
    r = admit(v1, tenant_wide, target, fx)
    assert r.verdict == "PROMOTE", r.counterexamples


def test_regression_of_protected_task_rejected(fx, safe, target):
    narrower = make_policy(fx, make_rule(rule_id="other", services=("billing",)))
    r = admit(safe, narrower, make_call(service="billing"), fx)
    assert r.verdict == "REJECT"
    assert FailureClass.LEGITIMATE_TASK_REGRESSION in r.failure_classes
    assert r.regression_count > 0


def test_target_not_satisfied_rejected(fx, v1, target):
    prod_only = make_policy(fx, make_rule(selector=make_rule().selector.model_copy(
        update={"values": ("production",)})))
    r = admit(v1, prod_only, target, fx)
    assert r.verdict == "REJECT" and FailureClass.TARGET_NOT_SATISFIED in r.failure_classes


def test_hash_mismatch_fails_closed(fx, v1, safe, target):
    tampered = fx.model_copy(update={"eval_times": (1,)})
    assert admit(v1, safe, target, tampered).verdict == "REJECT"
    other = safe.model_copy(update={"fixture_hash": "deadbeef"})
    r = admit(v1, other, target, fx)
    assert r.verdict == "REJECT" and r.failure_classes == (FailureClass.INCOMPLETE_EVALUATION,)


def test_no_change_fails_closed(fx, safe, target):
    assert admit(safe, safe, target, fx).verdict == "REJECT"


def test_gate_error_fails_closed(fx, v1, safe, target, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr("caselaw.judge.oracle", boom)
    r = admit(v1, safe, target, fx)
    assert r.verdict == "REJECT" and FailureClass.INCOMPLETE_EVALUATION in r.failure_classes


def test_judge_is_deterministic(fx, v1, no_approval, target):
    assert admit(v1, no_approval, target, fx) == admit(v1, no_approval, target, fx)
