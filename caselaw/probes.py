"""Candidate-universe construction and probe call selection.

Selection only: interpreting what was observed is detector.py's job.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from caselaw import policy as policy_mod
from caselaw.fleet import resolve
from caselaw.schema import (
    PROBE_BUDGET,
    WILDCARD,
    Call,
    Fixture,
    Policy,
    PolicyRule,
    ProbeObservation,
    ProbeResult,
    Strategy,
    stable_hash,
)

UNIFORM = Strategy(strategy_id="uniform-v1", alias_decisions=0)


@dataclass(frozen=True)
class ProbeContexts:
    """Fixed non-scope dimensions of every universe call."""

    actors: tuple[str, ...]
    tools: tuple[str, ...]
    incidents: tuple[str, ...]
    approval_contexts: tuple[str, ...]
    eval_times: tuple[int, ...]

    @classmethod
    def from_fixture(cls, fixture: Fixture) -> ProbeContexts:
        return cls(
            actors=tuple(a.actor_id for a in fixture.actors),
            tools=fixture.tools,
            incidents=fixture.incidents,
            approval_contexts=fixture.approval_contexts,
            eval_times=fixture.eval_times,
        )


# --------------------------------------------------------------------------
# Candidate universe (purely syntactic: never consults decide() or the oracle)
# --------------------------------------------------------------------------


def _all_scopes(fixture: Fixture) -> set[tuple[str, str]]:
    return {(r.tenant, r.service) for r in fixture.resources}


def _rule_scopes(rule: PolicyRule, fixture: Fixture) -> set[tuple[str, str]] | None:
    """Tenant/service pairs a rule can touch; None if not syntactically boundable."""
    tenants = {r.tenant for r in fixture.resources}
    services = {r.service for r in fixture.resources}
    if any(t != WILDCARD and t not in tenants for t in rule.tenants):
        return None
    if any(s != WILDCARD and s not in services for s in rule.services):
        return None
    ts = tenants if WILDCARD in rule.tenants else set(rule.tenants)
    ss = services if WILDCARD in rule.services else set(rule.services)
    return {(t, s) for t in ts for s in ss} & _all_scopes(fixture)


def affected_scopes(base: Policy, candidate: Policy, fixture: Fixture) -> list[tuple[str, str]]:
    """Scopes of every rule added, removed or changed (old and new both count)."""
    base_rules = {stable_hash(r): r for r in base.rules}
    cand_rules = {stable_hash(r): r for r in candidate.rules}
    changed = [base_rules[h] for h in base_rules.keys() - cand_rules.keys()] + [
        cand_rules[h] for h in cand_rules.keys() - base_rules.keys()
    ]
    scopes: set[tuple[str, str]] = set()
    for rule in changed:
        rs = _rule_scopes(rule, fixture)
        if rs is None:
            return sorted(_all_scopes(fixture))
        scopes |= rs
    return sorted(scopes)


def candidate_universe(
    base_policy: Policy,
    candidate_policy: Policy,
    fixture: Fixture,
    contexts: ProbeContexts | None = None,
) -> list[Call]:
    """Stable, deduplicated list of every call in the changed tenant/service scopes."""
    ctx = contexts or ProbeContexts.from_fixture(fixture)
    calls: dict[str, Call] = {}
    for tenant, service in affected_scopes(base_policy, candidate_policy, fixture):
        labels = [
            label
            for r in sorted(fixture.resources, key=lambda r: r.resource_id)
            if r.tenant == tenant and r.service == service
            for label in r.labels
        ]
        for label in labels:
            for actor in ctx.actors:
                for tool in ctx.tools:
                    for incident in ctx.incidents:
                        for approval_ctx in ctx.approval_contexts:
                            for t in ctx.eval_times:
                                call = Call(
                                    actor=actor,
                                    tool=tool,
                                    tenant=tenant,
                                    service=service,
                                    env_label=label,
                                    incident_id=incident,
                                    approval_context_id=approval_ctx,
                                    eval_time=t,
                                )
                                calls.setdefault(call.case_id, call)
    return list(calls.values())


def universe_hash(universe: list[Call]) -> str:
    return stable_hash([c.case_id for c in universe])


# --------------------------------------------------------------------------
# Probe selection
# --------------------------------------------------------------------------


def select_uniform(universe: list[Call], budget: int, rng: random.Random) -> list[Call]:
    if len(universe) <= budget:
        return list(universe)
    return rng.sample(universe, budget)


def select_calls(
    universe: list[Call], strategy: Strategy, budget: int, seed: int, fixture: Fixture
) -> list[Call]:
    rng = random.Random(seed)
    if strategy.alias_decisions == 0:
        return select_uniform(universe, budget, rng)
    raise NotImplementedError("alias-pair selection lands after the MVP")


def probe(
    candidate: Policy,
    universe: list[Call],
    strategy: Strategy,
    budget: int,
    seed: int,
    *,
    fixture: Fixture,
) -> ProbeResult:
    """Spend at most `budget` (<= 8) individual policy decisions on the universe."""
    budget = min(budget, PROBE_BUDGET)
    u_hash = universe_hash(universe)
    start = time.perf_counter()
    selected = select_calls(universe, strategy, budget, seed, fixture)[:budget]
    observations = []
    for i, call in enumerate(selected, start=1):
        resolved = resolve(call, fixture)
        observations.append(
            ProbeObservation(
                index=i,
                case_id=call.case_id,
                call=call,
                decision=policy_mod.decide(candidate, call, fixture),
                resource_id=resolved.resource_id if resolved else None,
                approval_context_id=call.approval_context_id,
                fixture_hash=fixture.fixture_hash,
                universe_hash=u_hash,
            )
        )
    return ProbeResult(
        strategy_id=strategy.strategy_id,
        alias_decisions=strategy.alias_decisions,
        seed=seed,
        budget=budget,
        decisions_used=len(observations),
        candidate_hash=candidate.content_hash,
        universe_hash=u_hash,
        fixture_hash=fixture.fixture_hash,
        observations=tuple(observations),
        duration_ms=(time.perf_counter() - start) * 1000,
    )
