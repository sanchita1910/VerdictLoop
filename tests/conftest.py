import pytest

from caselaw import fleet
from caselaw.schema import Call, Fixture, Policy, PolicyRule, Selector


@pytest.fixture(scope="session")
def fx() -> Fixture:
    return fleet.build_fixture()


def make_call(**overrides) -> Call:
    base = dict(
        actor="alice",
        tool=fleet.RESTART,
        tenant="acme",
        service="checkout",
        env_label="staging",
        incident_id=fleet.APPROVED_INCIDENT,
        approval_context_id=fleet.APPROVED_CTX,
        eval_time=fleet.VALID_TIME,
    )
    base.update(overrides)
    return Call(**base)


def make_rule(**overrides) -> PolicyRule:
    base = dict(
        rule_id="r-acme-checkout-staging",
        tools=(fleet.RESTART,),
        tenants=("acme",),
        services=("checkout",),
        selector=Selector(field="env_canonical", values=("staging",)),
        require_valid_approval=True,
    )
    base.update(overrides)
    return PolicyRule(**base)


def make_policy(fx: Fixture, *rules: PolicyRule, policy_id="policy-v2", version=2) -> Policy:
    return Policy(policy_id=policy_id, version=version, fixture_hash=fx.fixture_hash, rules=rules)


@pytest.fixture
def v1(fx) -> Policy:
    return make_policy(fx, policy_id="policy-v1", version=1)


@pytest.fixture
def safe(fx) -> Policy:
    return make_policy(fx, make_rule())


@pytest.fixture
def no_approval(fx) -> Policy:
    return make_policy(fx, make_rule(require_valid_approval=False), policy_id="cand-noapproval")


@pytest.fixture
def raw_label(fx) -> Policy:
    return make_policy(
        fx,
        make_rule(selector=Selector(field="env_label", values=("staging",))),
        policy_id="cand-rawlabel",
    )


@pytest.fixture
def tenant_wide(fx) -> Policy:
    return make_policy(fx, make_rule(tenants=("*",)), policy_id="cand-tenantwide")


@pytest.fixture
def target() -> Call:
    return make_call()
