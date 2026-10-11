"""Deterministic mutation generator.

Mutations are applied to a safe rule template retargeted onto each split's own
resources. Candidates are never labelled safe or unsafe here: only the full
gate decides that.
"""

from __future__ import annotations

import random

from verdictloop import fleet
from verdictloop.schema import (
    Call,
    Candidate,
    Fixture,
    Policy,
    PolicyRule,
    Selector,
    Split,
    stable_hash,
)

# Disjoint resources per split: each split owns one service across all tenants.
SPLIT_SERVICES: dict[str, str] = {
    "train": "checkout",
    "validation": "billing",
    "heldout": "search",
}

RAW_LABEL = "raw_label_selector"
APPROVAL_REMOVED = "approval_removed"
TENANT_WIDENED = "tenant_widened"
UNMUTATED = "unmutated_safe_control"

# Five candidates per split, fixed before any strategy selection.
RECIPE = (RAW_LABEL, APPROVAL_REMOVED, RAW_LABEL, TENANT_WIDENED, UNMUTATED)


def safe_rule(tenant: str, service: str, env: str) -> PolicyRule:
    return PolicyRule(
        rule_id=f"allow-{tenant}-{service}-{env}",
        tools=(fleet.RESTART,),
        tenants=(tenant,),
        services=(service,),
        selector=Selector(field="env_canonical", values=(env,)),
        require_valid_approval=True,
    )


def apply_operator(rule: PolicyRule, operator: str, label: str) -> PolicyRule:
    if operator == RAW_LABEL:
        return rule.model_copy(update={"selector": Selector(field="env_label", values=(label,))})
    if operator == APPROVAL_REMOVED:
        return rule.model_copy(update={"require_valid_approval": False})
    if operator == TENANT_WIDENED:
        return rule.model_copy(update={"tenants": ("*",)})
    if operator == UNMUTATED:
        return rule
    raise ValueError(f"unknown mutation operator {operator!r}")


def mutate(base_policy: Policy, fixture: Fixture, seed: int, split: Split) -> list[Candidate]:
    service = SPLIT_SERVICES[split]
    rng = random.Random(f"{seed}:{split}")
    scopes = [
        (r.tenant, r.env_canonical, r.labels)
        for r in sorted(fixture.resources, key=lambda r: r.resource_id)
        if r.service == service
    ]
    rng.shuffle(scopes)
    candidates = []
    for i, operator in enumerate(RECIPE):
        tenant, env, labels = scopes[i % len(scopes)]
        label = rng.choice(labels)
        rule = apply_operator(safe_rule(tenant, service, env), operator, label)
        policy = Policy(
            policy_id=f"{split}-{seed}-{i}",
            version=base_policy.version + 1,
            fixture_hash=fixture.fixture_hash,
            rules=base_policy.rules + (rule,),
        )
        target = Call(
            actor=fleet.ON_CALL[tenant],
            tool=fleet.RESTART,
            tenant=tenant,
            service=service,
            env_label=label,
            incident_id=fleet.APPROVED_INCIDENT,
            approval_context_id=fleet.APPROVED_CTX,
            eval_time=fleet.VALID_TIME,
        )
        candidates.append(
            Candidate(
                candidate_id=f"cand-{split}-{seed}-{i}-{policy.content_hash[:6]}",
                parent_policy_id=base_policy.policy_id,
                policy=policy,
                mutation_class=operator,
                seed=seed,
                split=split,
                content_hash=policy.content_hash,
                triggering_target=target,
            )
        )
    return candidates


def manifest(candidates: list[Candidate]) -> dict:
    """Frozen, hashable record of a candidate batch; persist before strategy selection."""
    rows = [
        {
            "candidate_id": c.candidate_id,
            "split": c.split,
            "mutation_class": c.mutation_class,
            "seed": c.seed,
            "parent_policy_id": c.parent_policy_id,
            "content_hash": c.content_hash,
        }
        for c in candidates
    ]
    return {"manifest_id": "manifest-" + stable_hash(rows), "candidates": rows}
