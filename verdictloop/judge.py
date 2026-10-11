"""Full finite-fixture gate and deterministic Judge. The only path to PROMOTE."""

from __future__ import annotations

from verdictloop.detector import alias_counterexample, find_alias_inconsistencies, grant_counterexample
from verdictloop.fleet import fixture_is_intact
from verdictloop.oracle import oracle
from verdictloop.policy import decide
from verdictloop.probes import ProbeContexts, candidate_universe, universe_hash
from verdictloop.schema import (
    Call,
    Counterexample,
    FailureClass,
    Fixture,
    GateResult,
    Policy,
)

MAX_COUNTEREXAMPLES_PER_CLASS = 10


def _reject(
    base: Policy,
    candidate: Policy,
    fixture: Fixture,
    detail: str,
    *,
    sweep: int = 0,
    u_hash: str = "",
) -> GateResult:
    return GateResult(
        passed=False,
        verdict="REJECT",
        failure_classes=(FailureClass.INCOMPLETE_EVALUATION,),
        evidence_case_ids=(),
        counterexamples=(
            Counterexample(
                failure_class=FailureClass.INCOMPLETE_EVALUATION,
                case_ids=(),
                policy_decisions=(),
                detail=detail,
            ),
        ),
        full_sweep_count=sweep,
        target_outcome=False,
        regression_count=0,
        candidate_hash=candidate.content_hash,
        base_hash=base.content_hash,
        fixture_hash=fixture.fixture_hash,
        universe_hash=u_hash,
    )


def full_gate(
    base: Policy,
    candidate: Policy,
    triggering_target: Call,
    fixture: Fixture,
    contexts: ProbeContexts | None = None,
) -> GateResult:
    if not fixture_is_intact(fixture):
        return _reject(base, candidate, fixture, "fixture hash mismatch")
    if base.fixture_hash != fixture.fixture_hash or candidate.fixture_hash != fixture.fixture_hash:
        return _reject(base, candidate, fixture, "policy bound to a different fixture")

    universe = candidate_universe(base, candidate, fixture, contexts)
    u_hash = universe_hash(universe)
    if not universe:
        return _reject(base, candidate, fixture, "empty candidate universe", u_hash=u_hash)

    target_outcome = oracle(triggering_target, fixture) and decide(
        candidate, triggering_target, fixture
    )

    grants: list[Counterexample] = []
    regressions: list[Counterexample] = []
    decided: list[tuple[int, Call, bool]] = []
    for i, call in enumerate(universe, start=1):
        truth = oracle(call, fixture)
        before = decide(base, call, fixture)
        after = decide(candidate, call, fixture)
        decided.append((i, call, after))
        if after and not truth:
            grants.append(grant_counterexample(call, None))
        if before and truth and not after:
            regressions.append(
                Counterexample(
                    failure_class=FailureClass.LEGITIMATE_TASK_REGRESSION,
                    case_ids=(call.case_id,),
                    policy_decisions=(after,),
                    oracle_decisions=(truth,),
                    detail="previously allowed legitimate task now denied",
                )
            )
    if len(decided) != len(universe):
        return _reject(base, candidate, fixture, "incomplete sweep", sweep=len(decided), u_hash=u_hash)

    aliases = [
        alias_counterexample(a, a_dec, b, b_dec, fixture, None)
        for _, a, a_dec, b, b_dec in find_alias_inconsistencies(decided, fixture)
    ]

    failures: list[FailureClass] = []
    counterexamples: list[Counterexample] = []
    if not target_outcome:
        failures.append(FailureClass.TARGET_NOT_SATISFIED)
        counterexamples.append(
            Counterexample(
                failure_class=FailureClass.TARGET_NOT_SATISFIED,
                case_ids=(triggering_target.case_id,),
                policy_decisions=(decide(candidate, triggering_target, fixture),),
                oracle_decisions=(oracle(triggering_target, fixture),),
                detail="triggering legitimate task is still not allowed",
            )
        )
    for cls, found in (
        (FailureClass.UNAUTHORIZED_GRANT, grants),
        (FailureClass.ALIAS_INCONSISTENCY, aliases),
        (FailureClass.LEGITIMATE_TASK_REGRESSION, regressions),
    ):
        if found:
            failures.append(cls)
            counterexamples.extend(found[:MAX_COUNTEREXAMPLES_PER_CLASS])

    evidence = sorted({cid for c in grants + aliases + regressions for cid in c.case_ids})
    passed = not failures
    return GateResult(
        passed=passed,
        verdict="PROMOTE" if passed else "REJECT",
        failure_classes=tuple(failures),
        evidence_case_ids=tuple(evidence),
        counterexamples=tuple(counterexamples),
        full_sweep_count=len(decided),
        target_outcome=target_outcome,
        regression_count=len(regressions),
        candidate_hash=candidate.content_hash,
        base_hash=base.content_hash,
        fixture_hash=fixture.fixture_hash,
        universe_hash=u_hash,
    )


def admit(
    base: Policy,
    candidate: Policy,
    triggering_target: Call,
    fixture: Fixture,
) -> GateResult:
    """Deterministic verdict. Never promotes on a fast-probe miss; any error rejects."""
    try:
        return full_gate(base, candidate, triggering_target, fixture)
    except Exception as exc:  # fail closed
        return _reject(base, candidate, fixture, f"gate error: {type(exc).__name__}")
