"""Bounded, allow-only, default-deny policy evaluator. No expressions, no eval()."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from caselaw.fleet import fixture_is_intact, resolve
from caselaw.schema import (
    WILDCARD,
    Approval,
    Call,
    Fixture,
    Policy,
    PolicyRule,
    ResolvedResource,
)


def _matches(values: tuple[str, ...], value: str) -> bool:
    return WILDCARD in values or value in values


def _selector_value(field: str, call: Call, resolved: ResolvedResource) -> str:
    if field == "resource_id":
        return resolved.resource_id
    if field == "env_canonical":
        return resolved.env_canonical
    return call.env_label  # raw, untrusted caller input


def _has_valid_approval(call: Call, resolved: ResolvedResource, approvals: list[Approval]) -> bool:
    return any(
        a.context_id == call.approval_context_id
        and a.actor == call.actor
        and a.tool == call.tool
        and a.resource_id == resolved.resource_id
        and a.incident_id == call.incident_id
        and a.not_before <= call.eval_time < a.expires_at
        for a in approvals
    )


def _rule_allows(
    rule: PolicyRule, call: Call, resolved: ResolvedResource, approvals: list[Approval]
) -> bool:
    if not (
        _matches(rule.tools, call.tool)
        and _matches(rule.tenants, resolved.tenant)
        and _matches(rule.services, resolved.service)
        and _matches(rule.actors, call.actor)
    ):
        return False
    if rule.selector is not None and not _matches(
        rule.selector.values, _selector_value(rule.selector.field, call, resolved)
    ):
        return False
    return not rule.require_valid_approval or _has_valid_approval(call, resolved, approvals)


def decide(
    policy: Policy | dict[str, Any],
    call: Call,
    fixture: Fixture,
    approvals: list[Approval] | None = None,
) -> bool:
    """True = ALLOW. Anything malformed, mismatched or unresolvable fails closed."""
    if not isinstance(policy, Policy):
        try:
            policy = Policy.model_validate(policy)
        except ValidationError:
            return False
    if not fixture_is_intact(fixture) or policy.fixture_hash != fixture.fixture_hash:
        return False
    resolved = resolve(call, fixture)
    if resolved is None:
        return False
    if approvals is None:
        approvals = fixture.approvals_for(call.approval_context_id)
    return any(_rule_allows(rule, call, resolved, approvals) for rule in policy.rules)
