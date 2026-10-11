"""Failure classification and bounded strategy selection.

Learns only how the fixed 8-decision probe budget is split between alias pairs
and uniform sampling (0/2/4 alias decisions). Train identifies which failure
class the current strategy misses; validation picks the allocation. Held-out
candidates are never touched here.
"""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, ConfigDict

from verdictloop.detector import detect_probe_violations, first_violation_index
from verdictloop.judge import admit
from verdictloop.probes import candidate_universe, probe
from verdictloop.schema import (
    PROBE_BUDGET,
    Candidate,
    FailureClass,
    Fixture,
    GateResult,
    Policy,
    Strategy,
)

ALLOCATIONS = (
    Strategy(strategy_id="uniform-v1", alias_decisions=0),
    Strategy(strategy_id="alias2-v2", alias_decisions=2),
    Strategy(strategy_id="alias4-v2", alias_decisions=4),
)
NOT_FOUND_COST = PROBE_BUDGET + 1  # probes-to-first when nothing was found


class TrialOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str
    mutation_class: str
    split: str
    strategy_id: str
    seed: int
    gate_verdict: str
    gate_failure_classes: tuple[FailureClass, ...]
    violating: bool  # ground truth from the full gate, not from the probe
    detected: bool
    detected_classes: tuple[FailureClass, ...]
    first_violation_index: int | None  # 1..8, None = NOT FOUND
    decisions_used: int
    universe_hash: str
    duration_ms: float


class AllocationScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy_id: str
    alias_decisions: int
    violating_trials: int
    detected_trials: int
    detection_rate: float
    mean_probes_to_first: float  # NOT FOUND counts as 9
    non_alias_trials: int
    non_alias_detected: int
    safe_trials: int
    safe_false_alarms: int


def gate_verdicts(base: Policy, candidates: list[Candidate], fixture: Fixture) -> dict[str, GateResult]:
    return {
        c.candidate_id: admit(base, c.policy, c.triggering_target, fixture) for c in candidates
    }


def run_trial(
    base: Policy,
    candidate: Candidate,
    strategy: Strategy,
    seed: int,
    fixture: Fixture,
    gate: GateResult,
) -> TrialOutcome:
    universe = candidate_universe(base, candidate.policy, fixture)
    result = probe(candidate.policy, universe, strategy, PROBE_BUDGET, seed, fixture=fixture)
    found = detect_probe_violations(list(result.observations), fixture)
    return TrialOutcome(
        candidate_id=candidate.candidate_id,
        mutation_class=candidate.mutation_class,
        split=candidate.split,
        strategy_id=strategy.strategy_id,
        seed=seed,
        gate_verdict=gate.verdict,
        gate_failure_classes=gate.failure_classes,
        violating=not gate.passed,
        detected=bool(found),
        detected_classes=tuple(sorted({c.failure_class for c in found}, key=lambda f: f.value)),
        first_violation_index=first_violation_index(found),
        decisions_used=result.decisions_used,
        universe_hash=result.universe_hash,
        duration_ms=result.duration_ms,
    )


def run_trials(
    base: Policy,
    candidates: list[Candidate],
    strategy: Strategy,
    seeds: tuple[int, ...],
    fixture: Fixture,
    gates: dict[str, GateResult] | None = None,
) -> list[TrialOutcome]:
    gates = gates or gate_verdicts(base, candidates, fixture)
    return [
        run_trial(base, c, strategy, seed, fixture, gates[c.candidate_id])
        for c in candidates
        for seed in seeds
    ]


def score(strategy: Strategy, trials: list[TrialOutcome]) -> AllocationScore:
    violating = [t for t in trials if t.violating]
    non_alias = [
        t for t in violating if FailureClass.ALIAS_INCONSISTENCY not in t.gate_failure_classes
    ]
    safe = [t for t in trials if not t.violating]
    detected = [t for t in violating if t.detected]
    costs = [t.first_violation_index or NOT_FOUND_COST for t in violating]
    return AllocationScore(
        strategy_id=strategy.strategy_id,
        alias_decisions=strategy.alias_decisions,
        violating_trials=len(violating),
        detected_trials=len(detected),
        detection_rate=len(detected) / len(violating) if violating else 0.0,
        mean_probes_to_first=sum(costs) / len(costs) if costs else float(NOT_FOUND_COST),
        non_alias_trials=len(non_alias),
        non_alias_detected=sum(t.detected for t in non_alias),
        safe_trials=len(safe),
        safe_false_alarms=sum(t.detected for t in safe),
    )


def missed_failure_classes(trials: list[TrialOutcome]) -> Counter[FailureClass]:
    """Failure classes the full gate found but the fast probe missed (train only)."""
    missed: Counter[FailureClass] = Counter()
    for t in trials:
        if t.violating and not t.detected:
            missed.update(t.gate_failure_classes)
    return missed


def improves(new: AllocationScore, cur: AllocationScore) -> bool:
    """Strictly better and no regression on non-alias detection or safe controls."""
    if new.non_alias_detected < cur.non_alias_detected:
        return False
    if new.safe_false_alarms > cur.safe_false_alarms:
        return False
    if new.detection_rate > cur.detection_rate:
        return True
    return (
        new.detection_rate == cur.detection_rate
        and new.mean_probes_to_first < cur.mean_probes_to_first
    )


class SelectionReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    current_strategy_id: str
    selected: Strategy
    changed: bool
    train_missed: dict[str, int]
    validation_scores: tuple[AllocationScore, ...]
    reason: str


def select_strategy(
    base: Policy,
    train: list[Candidate],
    validation: list[Candidate],
    seeds: tuple[int, ...],
    fixture: Fixture,
    current: Strategy = ALLOCATIONS[0],
) -> SelectionReport:
    """Bounded strategy selection: pick a 0/2/4 alias allocation, or keep current."""
    assert all(c.split == "train" for c in train)
    assert all(c.split == "validation" for c in validation)

    train_trials = run_trials(base, train, current, seeds, fixture)
    missed = missed_failure_classes(train_trials)
    missed_named = {k.value: v for k, v in missed.items()}

    val_gates = gate_verdicts(base, validation, fixture)
    scores = {
        s.strategy_id: score(s, run_trials(base, validation, s, seeds, fixture, val_gates))
        for s in ALLOCATIONS
    }
    cur_score = scores[current.strategy_id]

    if FailureClass.ALIAS_INCONSISTENCY not in missed:
        return SelectionReport(
            current_strategy_id=current.strategy_id, selected=current, changed=False,
            train_missed=missed_named, validation_scores=tuple(scores.values()),
            reason="current strategy misses no alias inconsistencies on train",
        )

    best = current
    for s in ALLOCATIONS:
        if s.strategy_id != best.strategy_id and improves(scores[s.strategy_id], scores[best.strategy_id]):
            best = s
    changed = best.strategy_id != current.strategy_id
    b, c = scores[best.strategy_id], cur_score
    reason = (
        f"validation: detection {c.detection_rate:.2f}->{b.detection_rate:.2f}, "
        f"probes-to-first {c.mean_probes_to_first:.2f}->{b.mean_probes_to_first:.2f}"
        if changed
        else "no allocation strictly improved validation without regression"
    )
    return SelectionReport(
        current_strategy_id=current.strategy_id, selected=best, changed=changed,
        train_missed=missed_named, validation_scores=tuple(scores.values()), reason=reason,
    )
