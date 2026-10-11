"""Known direct-entry authorization gaps for Developer B's safety modules.

These cases are marked xfail until the agreed B-owned resolver/oracle fixes land.
The production fixture remains unchanged; the tests exercise explicit inputs.
"""

import pytest

from verdictloop.fleet import (
    APPROVED_CTX, APPROVED_INCIDENT, RESTART, UNAPPROVED_CTX,
    VALID_TIME, build_fixture, resolve,
)
from verdictloop.oracle import oracle
from verdictloop.schema import Call


def _call(context=APPROVED_CTX):
    return Call(
        actor="alice", tool=RESTART, tenant="acme", service="checkout",
        env_label="staging", incident_id=APPROVED_INCIDENT,
        approval_context_id=context, eval_time=VALID_TIME,
    )


def test_supplied_fixture_approvals_match_default_oracle():
    fixture = build_fixture()
    for call in (_call(APPROVED_CTX), _call(UNAPPROVED_CTX)):
        assert oracle(call, fixture, approvals=fixture.approvals_for(call.approval_context_id)) == oracle(call, fixture)


@pytest.mark.xfail(strict=True, reason="B-owned resolver must reject ambiguous raw labels")
def test_resolver_rejects_ambiguous_label_within_scope():
    fixture = build_fixture()
    resources = tuple(
        resource.model_copy(update={"labels": (*resource.labels, "staging")})
        if resource.resource_id == "acme/checkout/production" else resource
        for resource in fixture.resources
    )
    ambiguous = fixture.model_copy(update={"resources": resources})
    ambiguous = ambiguous.model_copy(update={"fixture_hash": ambiguous.content_hash()})
    assert resolve(_call(), ambiguous) is None


@pytest.mark.xfail(strict=True, reason="B-owned oracle must constrain supplied approvals to fixture")
def test_oracle_rejects_supplied_approval_not_in_fixture():
    fixture = build_fixture()
    call = _call(UNAPPROVED_CTX)
    assert oracle(call, fixture) is False
    source = next(approval for approval in fixture.approvals
                  if approval.resource_id == "acme/checkout/staging")
    forged = source.model_copy(update={
        "approval_id": "outside-fixture",
        "context_id": UNAPPROVED_CTX,
    })
    assert oracle(call, fixture, approvals=[forged]) is False
