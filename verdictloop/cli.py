"""Atlas-backed CLI for the bounded VerdictLoop permission-change demonstration."""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from uuid import uuid4

from rich.console import Console

from verdictloop.builder import propose_safe_policy
from verdictloop.fleet import RESTART, build_fixture, resolve
from verdictloop.judge import admit
from verdictloop.oracle import oracle
from verdictloop.policy import decide
from verdictloop.schema import (
    Call,
    Candidate,
    FailureClass,
    Fixture,
    Policy,
    PolicyRule,
    Selector,
    Strategy,
    WILDCARD,
)
from verdictloop.store import ActiveRunState, CaseLawStore, StoreError, Trial


console = Console()
LATER_MUTATION_SEED = 101
LATER_PROBE_SEED = 113


def _decision_label(allowed: bool) -> str:
    return "ALLOW" if allowed else "DENY"


@dataclass(frozen=True)
class MvpOutcome:
    safe_trial: Trial
    unsafe_trial: Trial
    approved_call: Call
    unapproved_call: Call
    approved_oracle_decision: bool
    initial_policy_decision: bool
    unapproved_oracle_decision: bool
    unsafe_policy_decision: bool


@dataclass(frozen=True)
class CandidateOutcome:
    """A real full-gate trial and the call used to explain its decision."""

    candidate: Candidate
    trial: Trial
    call: Call
    oracle_decision: bool
    before_decision: bool
    candidate_decision: bool


def _open_store() -> CaseLawStore:
    store = CaseLawStore.from_env()
    store.ping()
    return store


def _approved_checkout_call(fixture: Fixture) -> Call:
    """Select an oracle-authorized checkout call from the frozen fixture."""
    for resource in fixture.resources:
        if resource.service != "checkout" or resource.env_canonical != "staging":
            continue
        for approval in fixture.approvals:
            if approval.resource_id != resource.resource_id:
                continue
            valid_time = next(
                (t for t in fixture.eval_times if approval.not_before <= t < approval.expires_at),
                None,
            )
            if valid_time is None:
                continue
            label = next(
                (raw for raw in resource.labels if raw != resource.env_canonical),
                resource.labels[0],
            )
            call = Call(
                actor=approval.actor,
                tool=approval.tool,
                tenant=resource.tenant,
                service=resource.service,
                env_label=label,
                incident_id=approval.incident_id,
                approval_context_id=approval.context_id,
                eval_time=valid_time,
            )
            if oracle(call, fixture):
                return call
    raise ValueError("Fixture has no approved checkout staging restart")


def _unapproved_context_call(approved: Call, fixture: Fixture) -> Call:
    for context_id in fixture.approval_contexts:
        if context_id == approved.approval_context_id:
            continue
        call = approved.model_copy(update={"approval_context_id": context_id})
        if not oracle(call, fixture):
            return call
    raise ValueError("Fixture has no unapproved demo context")


def _is_exact_approval_removal(base: Policy, candidate: Candidate, fixture: Fixture) -> bool:
    """Match the bounded approval-free mutation of B's canonical rule template.

    This is a demo-candidate selector, not an authorization verdict. The full
    Judge and independent oracle still decide safety.
    """
    if candidate.parent_policy_id != base.policy_id or candidate.policy.version != base.version + 1:
        return False
    if candidate.policy.fixture_hash != fixture.fixture_hash or candidate.content_hash != candidate.policy.content_hash:
        return False
    target = candidate.triggering_target
    resource = resolve(target, fixture)
    if resource is None or target.tool != RESTART or not oracle(target, fixture):
        return False
    before = {rule.rule_id: rule for rule in base.rules}
    after = {rule.rule_id: rule for rule in candidate.policy.rules}
    if len(before) != len(base.rules) or len(after) != len(candidate.policy.rules):
        return False
    added = after.keys() - before.keys()
    if len(added) != 1 or not before.keys() <= after.keys():
        return False
    if any(after[rule_id] != rule for rule_id, rule in before.items()):
        return False
    expected = PolicyRule(
        rule_id=f"allow-{resource.tenant}-{resource.service}-{resource.env_canonical}",
        tools=(RESTART,),
        tenants=(resource.tenant,),
        services=(resource.service,),
        actors=(WILDCARD,),
        selector=Selector(field="env_canonical", values=(resource.env_canonical,)),
        require_valid_approval=False,
    )
    return after[next(iter(added))] == expected


def init_run(store: CaseLawStore, run_id: str, seed: int) -> ActiveRunState:
    """Persist one frozen B fixture and create a version-one active run."""
    fixture = build_fixture()
    policy = Policy(
        policy_id=f"{run_id}-policy-v1",
        version=1,
        fixture_hash=fixture.fixture_hash,
        rules=(),
    )
    strategy = Strategy(strategy_id="uniform-v1", alias_decisions=0, budget=8)
    store.save_fixture(fixture)
    store.save_approvals(fixture)
    store.save_policy(policy)
    store.save_strategy(strategy, run_id=run_id, version=1)
    return store.initialize_run(
        ActiveRunState(
            run_id=run_id,
            revision=0,
            active_policy_id=policy.policy_id,
            active_strategy_id=strategy.strategy_id,
            fixture_hash=fixture.fixture_hash,
            objective_metrics={"approved_restart_allowed": False, "probe_seed": seed},
        )
    )


def _evaluate_safe(store: CaseLawStore, run_id: str) -> CandidateOutcome:
    active = store.load_active_state(run_id)
    if active.policy.version != 1:
        raise ValueError("The safe run requires a fresh policy-v1 state")
    fixture = active.fixture
    target = _approved_checkout_call(fixture)
    truth = oracle(target, fixture)
    before = decide(active.policy, target, fixture)
    if not truth or before:
        raise ValueError("The blocked legitimate target is not in the expected state")

    safe = propose_safe_policy(target, active.policy, fixture)
    gate = admit(active.policy, safe.policy, target, fixture)
    after = decide(safe.policy, target, fixture)
    if not (
        gate.passed
        and gate.verdict == "PROMOTE"
        and gate.target_outcome
        and gate.regression_count == 0
        and gate.full_sweep_count > 0
        and gate.universe_hash
        and after
    ):
        raise ValueError("The full gate did not promote the safe proposal")
    trial = Trial(
        trial_id=f"trial-{uuid4().hex}",
        run_id=run_id,
        candidate_id=safe.candidate_id,
        base_policy_id=active.policy.policy_id,
        candidate_policy_id=safe.policy.policy_id,
        strategy_id=active.strategy.strategy_id,
        fixture_hash=fixture.fixture_hash,
        universe_hash=gate.universe_hash,
        triggering_target=target,
        full_gate_result=gate,
        verdict=gate.verdict,
        counterexamples=gate.counterexamples,
    )
    return CandidateOutcome(safe, trial, target, truth, before, after)


def _evaluate_unsafe(store: CaseLawStore, run_id: str, base_policy: Policy) -> CandidateOutcome:
    active = store.load_active_state(run_id)
    if active.state.fixture_hash != base_policy.fixture_hash:
        raise ValueError("Unsafe candidate fixture differs from the active run")
    fixture = active.fixture
    try:
        from verdictloop.mutations import mutate
    except ImportError as exc:
        raise NotImplementedError("Developer B mutation generator is not integrated") from exc

    variants = mutate(base_policy, fixture, seed=17, split="train")
    unsafe = next((v for v in variants if _is_exact_approval_removal(base_policy, v, fixture)), None)
    if unsafe is None:
        raise ValueError("Developer B returned no exact approval-removal candidate")
    # B's mutation recipe uses global IDs such as train-17-1. Its rules depend
    # on this run's safe base, so the same ID can denote different content in
    # two Atlas runs. Scope storage identities without changing the candidate
    # rule diff, content hash, target, or Judge evaluation conditions.
    unsafe = unsafe.model_copy(update={
        "candidate_id": f"{run_id}-{unsafe.candidate_id}",
        "policy": unsafe.policy.model_copy(update={
            "policy_id": f"{run_id}-{unsafe.policy.policy_id}"
        }),
    })
    unapproved = _unapproved_context_call(unsafe.triggering_target, fixture)
    truth = oracle(unapproved, fixture)
    before = decide(base_policy, unapproved, fixture)
    after = decide(unsafe.policy, unapproved, fixture)
    if truth or before or not after:
        raise ValueError("The unsafe candidate does not demonstrate an unauthorized grant")
    # B's mutation carries its own legitimate target, which may be a different
    # tenant or environment from the Builder's original checkout call.
    gate = admit(base_policy, unsafe.policy, unsafe.triggering_target, fixture)
    if (
        gate.passed
        or gate.verdict != "REJECT"
        or FailureClass.UNAUTHORIZED_GRANT not in gate.failure_classes
        or not gate.universe_hash
    ):
        raise ValueError("The full gate did not reject the unauthorized grant")
    trial = Trial(
        trial_id=f"trial-{uuid4().hex}",
        run_id=run_id,
        candidate_id=unsafe.candidate_id,
        base_policy_id=base_policy.policy_id,
        candidate_policy_id=unsafe.policy.policy_id,
        strategy_id=active.strategy.strategy_id,
        fixture_hash=fixture.fixture_hash,
        universe_hash=gate.universe_hash,
        triggering_target=unsafe.triggering_target,
        full_gate_result=gate,
        verdict=gate.verdict,
        counterexamples=gate.counterexamples,
    )
    return CandidateOutcome(unsafe, trial, unapproved, truth, before, after)


def _persist_outcome(store: CaseLawStore, outcome: CandidateOutcome) -> None:
    store.save_candidate(outcome.candidate)
    store.save_policy(outcome.candidate.policy)
    store.save_trial(outcome.trial)


def run_safe(store: CaseLawStore, run_id: str) -> CandidateOutcome:
    """Evaluate, persist, and CAS-promote the bounded safe Builder proposal."""
    active = store.load_active_state(run_id)
    outcome = _evaluate_safe(store, run_id)
    _persist_outcome(store, outcome)
    store.promote_policy(outcome.candidate, outcome.trial, active.state.revision)
    return outcome


def run_unsafe(store: CaseLawStore, run_id: str) -> CandidateOutcome:
    """Evaluate and persist a rejected mutation; never move the policy pointer."""
    active = store.load_active_state(run_id)
    if active.policy.version != 2:
        raise ValueError("Run the safe proposal before the unsafe mutation")
    outcome = _evaluate_unsafe(store, run_id, active.policy)
    _persist_outcome(store, outcome)
    reloaded = store.load_active_state(run_id)
    if reloaded.state != active.state:
        raise ValueError("Rejected candidate changed Atlas active state")
    return outcome


def run_mvp(store: CaseLawStore, run_id: str) -> MvpOutcome:
    """Evaluate both real gate paths before active-pointer mutation."""
    active = store.load_active_state(run_id)
    safe = _evaluate_safe(store, run_id)
    unsafe = _evaluate_unsafe(store, run_id, safe.candidate.policy)
    _persist_outcome(store, safe)
    _persist_outcome(store, unsafe)
    promoted = store.promote_policy(safe.candidate, safe.trial, active.state.revision)
    reloaded = store.load_active_state(run_id)
    if (
        reloaded.state.revision != promoted.revision
        or reloaded.state.active_policy_id != safe.candidate.policy.policy_id
    ):
        raise ValueError("Rejected candidate changed Atlas active policy")
    return MvpOutcome(
        safe_trial=safe.trial,
        unsafe_trial=unsafe.trial,
        approved_call=safe.call,
        unapproved_call=unsafe.call,
        approved_oracle_decision=safe.oracle_decision,
        initial_policy_decision=safe.before_decision,
        unapproved_oracle_decision=unsafe.oracle_decision,
        unsafe_policy_decision=unsafe.candidate_decision,
    )


def evaluation_id(run_id: str, fixture_hash: str) -> str:
    return f"heldout-{run_id}-{fixture_hash}"


def load_run_evaluation(store: CaseLawStore, run_id: str, fixture_hash: str) -> dict | None:
    row = store.evaluations.find_one({"_id": evaluation_id(run_id, fixture_hash)})
    if row is None:
        return None
    payload = {key: value for key, value in row.items() if key != "_id"}
    if payload.get("run_id") != run_id or payload.get("fixture_hash") != fixture_hash:
        raise StoreError("Persisted evaluation identity differs from the active run")
    return payload


def compare_run(store: CaseLawStore, run_id: str) -> dict:
    """Persist B's frozen five-seed scorecard and measured whole-run duration."""
    from verdictloop.evaluation import build_scorecard
    from verdictloop.learning import ALLOCATIONS
    from verdictloop.schema import HELD_OUT_SEEDS

    active = store.load_active_state(run_id)
    if active.policy.version < 2:
        raise ValueError("Promote the safe proposal before running the held-out comparison")
    existing = load_run_evaluation(store, run_id, active.fixture.fixture_hash)
    if existing is not None:
        return existing
    base = store.load_policy(f"{run_id}-policy-v1")
    if base.version != 1 or base.rules or base.fixture_hash != active.fixture.fixture_hash:
        raise StoreError("Run's original policy does not match the frozen benchmark base")
    started = time.perf_counter()
    card = build_scorecard()
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    selected = Strategy.model_validate(card["selection"]["selected"])
    if (
        card["fixture_hash"] != active.fixture.fixture_hash
        or card["budget"] != 8
        or card["heldout_seeds"] != list(HELD_OUT_SEEDS)
        or selected not in ALLOCATIONS
        or len(card["candidates"]) != 5
        or len(card["manifest"]["candidates"]) != 15
    ):
        raise StoreError("Held-out scorecard differs from the frozen evaluation contract")
    evaluation = {
        "evaluation_id": evaluation_id(run_id, active.fixture.fixture_hash),
        "run_id": run_id,
        "fixture_hash": active.fixture.fixture_hash,
        "base_policy_id": base.policy_id,
        "base_policy_content_hash": base.content_hash,
        "strategy_id": selected.strategy_id,
        "promotion_eligible": bool(card["selection"]["changed"]),
        "evaluation_duration_ms": duration_ms,
        "scorecard": card,
    }
    store.save_evaluation(evaluation)
    return evaluation


def resume_run(store: CaseLawStore, run_id: str) -> dict:
    """Load a saved strategy, CAS-promote if eligible, then run one real later trial.

    This function is also used by a new ``python -m verdictloop.cli resume``
    process. That invocation creates its own Atlas client and proves reuse
    across process boundaries rather than relying on an in-memory pointer.
    """
    from verdictloop.detector import detect_probe_violations, first_violation_index
    from verdictloop.mutations import mutate
    from verdictloop.probes import candidate_universe, probe
    from verdictloop.schema import PROBE_BUDGET

    active = store.load_active_state(run_id)
    evaluation = load_run_evaluation(store, run_id, active.fixture.fixture_hash)
    if evaluation is None:
        raise ValueError("Run the held-out comparison before resuming the learned strategy")
    selected = Strategy.model_validate(evaluation["scorecard"]["selection"]["selected"])
    if evaluation["strategy_id"] != selected.strategy_id:
        raise StoreError("Persisted selection and evaluation strategy differ")
    already_active = active.strategy.strategy_id == selected.strategy_id
    promoted = False
    if evaluation["promotion_eligible"] and not already_active:
        if active.strategy.alias_decisions != 0:
            raise ValueError("Active strategy changed since the evaluation was frozen")
        store.save_strategy(
            selected, run_id=run_id, version=2,
            parent_strategy_id=active.strategy.strategy_id,
        )
        store.promote_strategy(selected, evaluation, expected_revision=active.state.revision)
        promoted = True
    reloaded = store.load_active_state(run_id)
    if promoted and reloaded.strategy.strategy_id != selected.strategy_id:
        raise StoreError("Promoted strategy was not visible after Atlas readback")

    # Predetermined unseen candidate and probe seeds. No outcome-dependent
    # case selection is permitted in this later demo trial.
    base = store.load_policy(f"{run_id}-policy-v1")
    source = mutate(base, reloaded.fixture, LATER_MUTATION_SEED, "heldout")[0]
    frozen_hashes = {
        row["content_hash"] for row in evaluation["scorecard"]["manifest"]["candidates"]
    }
    if source.content_hash in frozen_hashes:
        raise StoreError("Later trial candidate repeats a frozen evaluation candidate")
    candidate = source.model_copy(update={
        "candidate_id": f"later-{run_id}-{source.candidate_id}",
        "policy": source.policy.model_copy(update={
            "policy_id": f"later-{run_id}-{source.policy.policy_id}"
        }),
    })
    universe = candidate_universe(base, candidate.policy, reloaded.fixture)
    seed = LATER_PROBE_SEED
    fast = probe(candidate.policy, universe, reloaded.strategy, PROBE_BUDGET, seed,
                 fixture=reloaded.fixture)
    gate = admit(base, candidate.policy, candidate.triggering_target, reloaded.fixture)
    if (
        fast.decisions_used != PROBE_BUDGET
        or fast.universe_hash != gate.universe_hash
        or fast.fixture_hash != reloaded.fixture.fixture_hash
    ):
        raise StoreError("Later trial probe and full gate used different evaluation conditions")
    detected = detect_probe_violations(list(fast.observations), reloaded.fixture)
    trial = Trial(
        trial_id=f"later-trial-{uuid4().hex}", run_id=run_id,
        candidate_id=candidate.candidate_id,
        base_policy_id=base.policy_id,
        candidate_policy_id=candidate.policy.policy_id,
        strategy_id=reloaded.strategy.strategy_id,
        fixture_hash=reloaded.fixture.fixture_hash,
        universe_hash=gate.universe_hash,
        triggering_target=candidate.triggering_target,
        fast_probe_result=fast,
        full_gate_result=gate,
        verdict=gate.verdict,
        counterexamples=gate.counterexamples,
    )
    store.save_candidate(candidate)
    store.save_policy(candidate.policy)
    store.save_trial(trial)
    after = store.load_active_state(run_id)
    if after.state != reloaded.state:
        raise StoreError("Later rejected trial unexpectedly changed active state")
    return {
        "run_id": run_id,
        "evaluation_id": evaluation["evaluation_id"],
        "promotion_eligible": evaluation["promotion_eligible"],
        "promoted": promoted,
        "already_active": already_active,
        "selected_strategy": selected.model_dump(mode="json"),
        "active": {
            "policy_id": after.policy.policy_id,
            "policy_version": after.policy.version,
            "strategy_id": after.strategy.strategy_id,
            "revision": after.state.revision,
            "fixture_hash": after.fixture.fixture_hash,
        },
        "later_trial": {
            "trial_id": trial.trial_id,
            "candidate_id": candidate.candidate_id,
            "strategy_id": trial.strategy_id,
            "seed": seed,
            "mutation_seed": LATER_MUTATION_SEED,
            "probe_seed": seed,
            "decisions_used": fast.decisions_used,
            "detected": bool(detected),
            "detected_classes": sorted({item.failure_class.value for item in detected}),
            "first_violation_index": first_violation_index(detected) or "NOT FOUND",
            "full_gate_verdict": gate.verdict,
            "full_gate_failure_classes": [item.value for item in gate.failure_classes],
            "full_sweep_count": gate.full_sweep_count,
            "universe_hash": gate.universe_hash,
            "observations": [item.model_dump(mode="json") for item in fast.observations],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VerdictLoop policy promotion demo")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("smoke-atlas")
    init_parser = commands.add_parser("init")
    init_parser.add_argument("--run-id", required=True)
    init_parser.add_argument("--seed", type=int, default=17)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--run-id", required=True)
    mvp_parser = commands.add_parser("mvp")
    mvp_parser.add_argument("--run-id", required=True)
    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("--run-id", required=True)
    resume_parser = commands.add_parser("resume")
    resume_parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        store = _open_store()
        if args.command == "smoke-atlas":
            console.print("Atlas connection: OK")
        elif args.command == "init":
            state = init_run(store, args.run_id, args.seed)
            console.print(
                f"Initialized {state.run_id}: policy={state.active_policy_id} "
                f"strategy={state.active_strategy_id} revision={state.revision}"
            )
        elif args.command == "status":
            active = store.load_active_state(args.run_id)
            console.print(
                f"Run {active.state.run_id}: policy={active.policy.policy_id} "
                f"strategy={active.strategy.strategy_id} revision={active.state.revision} "
                f"fixture={active.state.fixture_hash}"
            )
        elif args.command == "mvp":
            outcome = run_mvp(store, args.run_id)
            console.print(
                f"Approved call {outcome.approved_call.case_id}: "
                f"oracle={_decision_label(outcome.approved_oracle_decision)}; "
                f"policy v1={_decision_label(outcome.initial_policy_decision)}"
            )
            console.print(
                f"Unsafe call {outcome.unapproved_call.case_id}: "
                f"oracle={_decision_label(outcome.unapproved_oracle_decision)}; "
                f"candidate={_decision_label(outcome.unsafe_policy_decision)}"
            )
            console.print(
                f"Safe candidate: {outcome.safe_trial.verdict}; "
                f"trial={outcome.safe_trial.trial_id}"
            )
            console.print(
                f"Unsafe candidate: {outcome.unsafe_trial.verdict}; "
                f"trial={outcome.unsafe_trial.trial_id}"
            )
            active = store.load_active_state(args.run_id)
            console.print(f"Active policy: {active.policy.policy_id}; revision={active.state.revision}")
        elif args.command == "compare":
            evaluation = compare_run(store, args.run_id)
            card = evaluation["scorecard"]
            baseline = card["summary"]["baseline"]
            learned = card["summary"]["learned"]
            console.print(
                f"Evaluation {evaluation['evaluation_id']}: "
                f"uniform {baseline['violating_trials_detected']}/{baseline['violating_trials_total']} "
                f"vs learned {learned['violating_trials_detected']}/{learned['violating_trials_total']} "
                f"violating trials detected; duration={evaluation['evaluation_duration_ms']} ms"
            )
            console.print(
                f"Selected {evaluation['strategy_id']}; "
                f"promotion_eligible={evaluation['promotion_eligible']}"
            )
        elif args.command == "resume":
            result = resume_run(store, args.run_id)
            trial = result["later_trial"]
            console.print(
                f"Atlas strategy readback: {result['active']['strategy_id']}; "
                f"revision={result['active']['revision']}; promoted={result['promoted']}"
            )
            console.print(
                f"Later trial {trial['trial_id']}: strategy={trial['strategy_id']} "
                f"probe decisions={trial['decisions_used']} "
                f"detected={trial['detected']} first={trial['first_violation_index']} "
                f"full gate={trial['full_gate_verdict']}"
            )
    except (StoreError, ValueError, NotImplementedError) as exc:
        console.print(f"Error: {exc}", style="red")
        return 1
    except Exception as exc:
        # Driver exceptions can embed connection details in their messages.
        console.print(
            f"Error: {type(exc).__name__}; details withheld to protect credentials",
            style="red",
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
