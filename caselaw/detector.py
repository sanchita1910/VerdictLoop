"""The single, shared post-probe violation detector.

Every probe strategy calls detect_probe_violations(); it receives no strategy
hints, so strategies differ only in which calls they selected.
"""

from __future__ import annotations

from collections.abc import Iterable

from caselaw.fleet import resolve
from caselaw.oracle import oracle
from caselaw.schema import (
    Call,
    Counterexample,
    FailureClass,
    Fixture,
    ProbeObservation,
)

AliasKey = tuple[str, str, str, str, str, str, str, int]


def equivalence_key(call: Call, fixture: Fixture) -> AliasKey | None:
    """Everything except the raw env_label, using trusted resolution."""
    resolved = resolve(call, fixture)
    if resolved is None:
        return None
    return (
        resolved.resource_id,
        resolved.tenant,
        resolved.service,
        call.actor,
        call.incident_id,
        call.tool,
        call.approval_context_id,
        call.eval_time,
    )


def find_alias_inconsistencies(
    decided: Iterable[tuple[int, Call, bool]], fixture: Fixture
) -> list[tuple[int, Call, bool, Call, bool]]:
    """(later_index, first_call, first_decision, later_call, later_decision) per
    equivalence class, reported once, at the earliest point it becomes evident."""
    first_seen: dict[AliasKey, list[tuple[Call, bool]]] = {}
    reported: set[AliasKey] = set()
    found = []
    for index, call, decision in decided:
        key = equivalence_key(call, fixture)
        if key is None:
            continue
        prior = first_seen.setdefault(key, [])
        if key not in reported:
            for other, other_decision in prior:
                if other.env_label != call.env_label and other_decision != decision:
                    found.append((index, other, other_decision, call, decision))
                    reported.add(key)
                    break
        prior.append((call, decision))
    return found


def alias_counterexample(
    first: Call, first_dec: bool, later: Call, later_dec: bool, fixture: Fixture,
    first_evidence_index: int | None,
) -> Counterexample:
    truth = oracle(first, fixture)  # equivalent calls share one ground truth
    return Counterexample(
        failure_class=FailureClass.ALIAS_INCONSISTENCY,
        case_ids=(first.case_id, later.case_id),
        policy_decisions=(first_dec, later_dec),
        oracle_decisions=(truth, truth),
        severity=FailureClass.INCORRECT_DENIAL if truth else FailureClass.UNAUTHORIZED_GRANT,
        first_evidence_index=first_evidence_index,
        detail=f"{first.env_label!r} vs {later.env_label!r} on the same resource",
    )


def grant_counterexample(call: Call, first_evidence_index: int | None) -> Counterexample:
    return Counterexample(
        failure_class=FailureClass.UNAUTHORIZED_GRANT,
        case_ids=(call.case_id,),
        policy_decisions=(True,),
        oracle_decisions=(False,),
        first_evidence_index=first_evidence_index,
        detail=f"{call.actor} {call.tool} {call.tenant}/{call.service}/{call.env_label} "
        f"ctx={call.approval_context_id} incident={call.incident_id} t={call.eval_time}",
    )


def detect_probe_violations(
    observations: list[ProbeObservation], fixture: Fixture
) -> list[Counterexample]:
    found: list[Counterexample] = []
    for obs in observations:
        if obs.decision and not oracle(obs.call, fixture):
            found.append(grant_counterexample(obs.call, obs.index))
    for index, a, a_dec, b, b_dec in find_alias_inconsistencies(
        ((o.index, o.call, o.decision) for o in observations), fixture
    ):
        found.append(alias_counterexample(a, a_dec, b, b_dec, fixture, index))
    return sorted(found, key=lambda c: (c.first_evidence_index, c.failure_class.value))


def first_violation_index(counterexamples: list[Counterexample]) -> int | None:
    """1..8, or None meaning NOT FOUND."""
    indexes = [c.first_evidence_index for c in counterexamples if c.first_evidence_index]
    return min(indexes) if indexes else None
