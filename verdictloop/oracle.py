"""Independent authorization oracle (ground truth). Shares no code with policy.py."""

from __future__ import annotations

from verdictloop.fleet import resolve
from verdictloop.schema import Approval, Call, Fixture


def oracle(call: Call, fixture: Fixture, approvals: list[Approval] | None = None) -> bool:
    """True iff the call is genuinely authorized.

    Authorized means: the target resolves, the actor is on call for that
    tenant/service, and an unexpired approval exists for exactly this actor,
    tool, resource and incident in the call's approval context.
    """
    resolved = resolve(call, fixture)
    if resolved is None or call.tool not in fixture.tools:
        return False
    actor = next((a for a in fixture.actors if a.actor_id == call.actor), None)
    if actor is None or actor.tenant != resolved.tenant or resolved.service not in actor.services:
        return False
    if approvals is None:
        approvals = fixture.approvals_for(call.approval_context_id)
    return any(
        a.context_id == call.approval_context_id
        and a.actor == call.actor
        and a.tool == call.tool
        and a.resource_id == resolved.resource_id
        and a.incident_id == call.incident_id
        and a.not_before <= call.eval_time < a.expires_at
        for a in approvals
    )
