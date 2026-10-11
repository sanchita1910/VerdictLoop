"""Deterministic, bounded proposal for a legitimate restart blocked by policy.

The Builder only returns candidate data. The independent full gate and the
promotion controller decide whether that data can become active policy.
"""

from __future__ import annotations

from verdictloop.fleet import RESTART, fixture_is_intact, resolve
from verdictloop.oracle import oracle
from verdictloop.policy import decide
from verdictloop.schema import (
    Call,
    Candidate,
    Fixture,
    Policy,
    PolicyRule,
    Selector,
    stable_hash,
)


class BuilderInputError(ValueError):
    """The supplied call is not a legitimate blocked restart in this fixture."""


def propose_safe_policy(blocked_call: Call, active_policy: Policy, fixture: Fixture) -> Candidate:
    """Add one canonical-environment allow rule that still requires approval.

    Scope is the trusted resolved tenant, service, environment, and the
    triggering actor. The raw environment label does not influence the rule.
    This function neither modifies the fixture nor changes the active policy.
    """
    if not fixture_is_intact(fixture):
        raise BuilderInputError("Fixture content hash is invalid")
    if active_policy.fixture_hash != fixture.fixture_hash:
        raise BuilderInputError("Active policy belongs to another fixture")
    if blocked_call.tool != RESTART:
        raise BuilderInputError("Only restart_service is in scope")

    resource = resolve(blocked_call, fixture)
    if resource is None:
        raise BuilderInputError("The target does not resolve in the trusted fleet")
    if not oracle(blocked_call, fixture):
        raise BuilderInputError("The independent oracle does not approve this call")
    if decide(active_policy, blocked_call, fixture):
        raise BuilderInputError("The active policy already allows this call")

    rule_id = "safe-" + stable_hash(
        [active_policy.policy_id, resource.resource_id, blocked_call.actor, RESTART]
    )
    if any(rule.rule_id == rule_id for rule in active_policy.rules):
        raise BuilderInputError("The proposed rule ID already exists")
    rule = PolicyRule(
        rule_id=rule_id,
        tools=(RESTART,),
        tenants=(resource.tenant,),
        services=(resource.service,),
        actors=(blocked_call.actor,),
        selector=Selector(field="env_canonical", values=(resource.env_canonical,)),
        require_valid_approval=True,
    )
    proposed = Policy(
        policy_id="policy-" + stable_hash([active_policy.policy_id, rule.model_dump(mode="json")]),
        version=active_policy.version + 1,
        fixture_hash=fixture.fixture_hash,
        rules=(*active_policy.rules, rule),
    )
    return Candidate(
        candidate_id="candidate-" + stable_hash(
            [active_policy.policy_id, blocked_call.case_id, proposed.content_hash]
        ),
        parent_policy_id=active_policy.policy_id,
        policy=proposed,
        mutation_class="safe_repair",
        seed=0,
        split="demo",
        content_hash=proposed.content_hash,
        triggering_target=blocked_call,
        producer="deterministic_builder",
    )
