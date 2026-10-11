"""Frozen held-out differential evaluation: baseline vs learned probe strategy.

Both strategies get the same candidates, candidate universes, seeds, 8-decision
budget and shared detector; only call selection differs. Descriptive results
only: no statistical significance is claimed.

Run:  python -m verdictloop.evaluation [--out results/scorecard.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from verdictloop import fleet
from verdictloop.learning import ALLOCATIONS, TrialOutcome, gate_verdicts, run_trials, score, select_strategy
from verdictloop.mutations import manifest, mutate
from verdictloop.schema import HELD_OUT_SEEDS, PROBE_BUDGET, Candidate, Fixture, Policy, Strategy

MUTATION_SEED = 1
TRAIN_VALIDATION_SEEDS = (1, 2, 3, 4, 5)  # disjoint from HELD_OUT_SEEDS


def _hit(index: int | None) -> int | str:
    return index if index is not None else "NOT FOUND"


def _summary(strategy: Strategy, trials: list[TrialOutcome]) -> dict:
    s = score(strategy, trials)
    violating = [t for t in trials if t.violating]
    violating_ids = sorted({t.candidate_id for t in violating})
    return {
        "strategy_id": strategy.strategy_id,
        "alias_decisions": strategy.alias_decisions,
        "violating_trials_detected": s.detected_trials,
        "violating_trials_total": s.violating_trials,
        "detection_rate": round(s.detection_rate, 4),
        "distinct_violating_detected": len({t.candidate_id for t in violating if t.detected}),
        "distinct_violating_total": len(violating_ids),
        "mean_probes_to_first": round(s.mean_probes_to_first, 3),
        "non_alias_detected": s.non_alias_detected,
        "non_alias_total": s.non_alias_trials,
        "safe_control_false_alarms": s.safe_false_alarms,
        "safe_control_trials": s.safe_trials,
        "decisions_used": sum(t.decisions_used for t in trials),
        "probe_duration_ms": round(sum(t.duration_ms for t in trials), 3),
    }


def evaluate(
    base: Policy,
    heldout: list[Candidate],
    baseline: Strategy,
    learned: Strategy,
    seeds: tuple[int, ...],
    fixture: Fixture,
) -> dict:
    assert all(c.split == "heldout" for c in heldout)
    gates = gate_verdicts(base, heldout, fixture)
    trials = {
        s.strategy_id: run_trials(base, heldout, s, seeds, fixture, gates) for s in (baseline, learned)
    }
    rows = []
    for c in heldout:
        g = gates[c.candidate_id]
        row = {
            "candidate_id": c.candidate_id,
            "mutation_class": c.mutation_class,
            "gate_verdict": g.verdict,
            "gate_failure_classes": [f.value for f in g.failure_classes],
            "target_outcome": g.target_outcome,
            "regression_count": g.regression_count,
            "full_sweep_count": g.full_sweep_count,
            "universe_hash": g.universe_hash,
            "strategies": {},
        }
        for sid, ts in trials.items():
            mine = [t for t in ts if t.candidate_id == c.candidate_id]
            row["strategies"][sid] = {
                "detected_seeds": [t.seed for t in mine if t.detected],
                "first_hits": [_hit(t.first_violation_index) for t in mine],
                "decisions_used": [t.decisions_used for t in mine],
                "duration_ms": round(sum(t.duration_ms for t in mine), 3),
            }
        rows.append(row)
    return {
        "candidates": rows,
        "summary": {
            "baseline": _summary(baseline, trials[baseline.strategy_id]),
            "learned": _summary(learned, trials[learned.strategy_id]),
        },
        "legitimate_task_accuracy": {
            "targets_satisfied": sum(gates[c.candidate_id].target_outcome for c in heldout),
            "candidates": len(heldout),
            "promoted": sum(gates[c.candidate_id].passed for c in heldout),
            "total_regressions": sum(gates[c.candidate_id].regression_count for c in heldout),
        },
        "full_gate_decisions": sum(3 * gates[c.candidate_id].full_sweep_count for c in heldout),
    }


def build_scorecard(seeds: tuple[int, ...] = HELD_OUT_SEEDS) -> dict:
    fixture = fleet.build_fixture()
    base = Policy(policy_id="policy-v1", version=1, fixture_hash=fixture.fixture_hash)

    # 1. Freeze every split and the manifest before any strategy selection.
    train = mutate(base, fixture, MUTATION_SEED, "train")
    validation = mutate(base, fixture, MUTATION_SEED, "validation")
    heldout = mutate(base, fixture, MUTATION_SEED, "heldout")
    frozen = manifest(train + validation + heldout)

    # 2. Learn on train/validation only.
    selection = select_strategy(base, train, validation, TRAIN_VALIDATION_SEEDS, fixture)

    # 3. Evaluate once on held-out.
    results = evaluate(base, heldout, ALLOCATIONS[0], selection.selected, seeds, fixture)
    return {
        "manifest": frozen,
        "fixture_hash": fixture.fixture_hash,
        "budget": PROBE_BUDGET,
        "heldout_seeds": list(seeds),
        "train_validation_seeds": list(TRAIN_VALIDATION_SEEDS),
        "selection": selection.model_dump(mode="json"),
        **results,
        "notes": "Descriptive results on a bounded synthetic fixture; "
        "no statistical significance claimed. The full gate, not the probe, decides promotion.",
    }


def print_scorecard(card: dict) -> None:
    b, l = card["summary"]["baseline"], card["summary"]["learned"]
    print(f"Manifest {card['manifest']['manifest_id']} | fixture {card['fixture_hash']} | "
          f"budget {card['budget']} | seeds {card['heldout_seeds']}")
    print(f"Selection: {card['selection']['selected']['strategy_id']} ({card['selection']['reason']})\n")
    print(f"{'candidate':34} {'mutation':24} {'gate':8} {'uniform hits':28} learned hits")
    for r in card["candidates"]:
        hits = [r["strategies"][s["strategy_id"]]["first_hits"] for s in (b, l)]
        print(f"{r['candidate_id']:34} {r['mutation_class']:24} {r['gate_verdict']:8} "
              f"{str(hits[0]):28} {hits[1]}")
    print()
    for label, s in (("baseline", b), ("learned", l)):
        print(f"{label:8} {s['strategy_id']:11} detected {s['violating_trials_detected']}/"
              f"{s['violating_trials_total']} trials ({s['detection_rate']:.0%}), "
              f"{s['distinct_violating_detected']}/{s['distinct_violating_total']} candidates, "
              f"mean probes-to-first {s['mean_probes_to_first']}, "
              f"safe false alarms {s['safe_control_false_alarms']}/{s['safe_control_trials']}")
    acc = card["legitimate_task_accuracy"]
    print(f"legit tasks: {acc['targets_satisfied']}/{acc['candidates']} targets satisfied, "
          f"{acc['total_regressions']} regressions, {acc['promoted']} promoted")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="results/scorecard.json")
    args = parser.parse_args()
    card = build_scorecard()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(card, indent=2))
    print_scorecard(card)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
