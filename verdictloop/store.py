"""Atlas persistence for VerdictLoop's versioned evidence and active pointers.

The evaluator's ``verdictloop.schema`` models are the only fixture, policy, and
candidate truth. This module adds storage-only run/trial records. A Builder can
save a proposal, but only this controller can move an active pointer after a
persisted full-gate verdict and an atomic revision check.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import DuplicateKeyError

from verdictloop.schema import (
    Call,
    Candidate,
    Counterexample,
    Fixture,
    GateResult,
    Policy,
    ProbeResult,
    Strategy,
)


class StoreError(RuntimeError):
    """Persisted state or connection cannot be used safely."""


class MissingReferenceError(StoreError):
    """An active pointer or evidence references a missing document."""


class FixtureMismatchError(StoreError):
    """A fixture or evidence record disagrees with the sealed fixture hash."""


class StalePromotionError(StoreError):
    """The active-pointer compare-and-swap matched no run."""


class PromotionEvidenceError(StoreError):
    """Persisted full-gate evidence does not permit promotion."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Trial(_Record):
    """One candidate evaluation, including the evaluator's original verdict."""

    trial_id: str
    run_id: str
    candidate_id: str
    base_policy_id: str
    candidate_policy_id: str
    strategy_id: str
    fixture_hash: str
    universe_hash: str
    triggering_target: Call
    fast_probe_result: ProbeResult | None = None
    full_gate_result: GateResult
    verdict: str
    counterexamples: tuple[Counterexample, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)


class ActiveRunState(_Record):
    run_id: str
    revision: int = Field(ge=0)
    active_policy_id: str
    active_strategy_id: str
    fixture_hash: str
    objective_metrics: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ActiveRuntimeState(_Record):
    state: ActiveRunState
    policy: Policy
    strategy: Strategy
    fixture: Fixture

    @property
    def active_policy(self) -> Policy:
        return self.policy

    @property
    def active_strategy(self) -> Strategy:
        return self.strategy


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise StoreError("Persisted timestamps must have a timezone")
        return value.astimezone(timezone.utc).isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StoreError("Non-finite numbers cannot be persisted")
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) or key.startswith("$") or "." in key for key in value):
            raise StoreError("Document keys must be safe strings")
        return {key: _json_value(item) for key, item in value.items()}
    raise StoreError(f"Unsupported persisted value type: {type(value).__name__}")


def _document(value: Any) -> dict[str, Any]:
    payload = _json_value(value)
    if not isinstance(payload, dict):
        raise StoreError("A document must be a mapping")
    return json.loads(json.dumps(payload, allow_nan=False))


def _clean(document: Mapping[str, Any], *metadata: str) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key not in {"_id", *metadata}}


def _required(document: Mapping[str, Any] | None, label: str) -> Mapping[str, Any]:
    if document is None:
        raise MissingReferenceError(f"Missing {label}")
    return document


def _checked_fixture(fixture: Fixture) -> None:
    if not fixture.fixture_hash or fixture.content_hash() != fixture.fixture_hash:
        raise FixtureMismatchError("Fixture content does not match its hash")
    if len({a.approval_id for a in fixture.approvals}) != len(fixture.approvals):
        raise FixtureMismatchError("Fixture has duplicate approval IDs")


def _checked_candidate(candidate: Candidate) -> None:
    if candidate.content_hash != candidate.policy.content_hash:
        raise PromotionEvidenceError("Candidate hash does not match its policy rules")
    if candidate.policy.policy_id == candidate.parent_policy_id:
        raise PromotionEvidenceError("Candidate must propose a new policy ID")


def _insert_immutable(collection: Any, document: dict[str, Any], label: str) -> None:
    """Repeat an insert only if the existing document is byte-for-byte equal."""
    identifier = document["_id"]
    existing = collection.find_one({"_id": identifier})
    if existing is not None:
        if dict(existing) != document:
            raise StoreError(f"Conflicting {label} document already exists")
        return
    try:
        collection.insert_one(document)
    except DuplicateKeyError:
        existing = collection.find_one({"_id": identifier})
        if existing is None or dict(existing) != document:
            raise StoreError(f"Conflicting {label} document already exists") from None


class CaseLawStore:
    """Seven-collection persistence boundary; ``db`` can be PyMongo or a fake."""

    def __init__(self, db: Any, *, client: Any = None) -> None:
        self.db = db
        self._client = client
        self.fleet = db["fleet"]
        self.approvals = db["approvals"]
        self.policies = db["policies"]
        self.strategies = db["strategies"]
        self.trials = db["trials"]
        self.evaluations = db["evaluations"]
        self.runs = db["runs"]

    @classmethod
    def from_env(cls) -> CaseLawStore:
        """Read only a Git-ignored local .env; never display the URI or credentials."""
        from dotenv import load_dotenv
        from pymongo import MongoClient

        repository = Path(__file__).resolve().parent.parent
        env_path = repository / ".env"
        if env_path.exists() or env_path.is_symlink():
            if env_path.is_symlink():
                raise StoreError("Refusing to read a symlinked .env file")
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", ".env"],
                cwd=repository,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ignored.returncode != 0:
                raise StoreError("Repository .env is not Git-ignored; refusing to read it")
            load_dotenv(dotenv_path=env_path, override=False)
        uri = os.getenv("MONGODB_URI")
        if not uri:
            raise StoreError("MONGODB_URI is not set")
        if not uri.startswith(("mongodb://", "mongodb+srv://")):
            raise StoreError("MONGODB_URI must start with mongodb:// or mongodb+srv://")
        db_name = os.getenv("CASELAW_DB", "caselaw")
        if not db_name or any(char in db_name for char in ("/", "\\", "\x00")):
            raise StoreError("CASELAW_DB is invalid")
        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            return cls(client[db_name], client=client)
        except Exception:
            raise StoreError("Atlas client setup failed; check connection settings") from None

    def ping(self) -> bool:
        try:
            if self._client is None:
                self.db.command("ping")
            else:
                self._client.admin.command("ping")
        except Exception:
            raise StoreError("Atlas connection failed; check access and network settings") from None
        return True

    def save_fixture(self, fixture: Fixture) -> None:
        _checked_fixture(fixture)
        payload = _document(fixture)
        payload.pop("approvals")
        _insert_immutable(
            self.fleet,
            {"_id": f"fixture:{fixture.fixture_hash}", "approval_count": len(fixture.approvals), **payload},
            "fixture",
        )

    def save_approvals(self, fixture: Fixture) -> None:
        _checked_fixture(fixture)
        for ordinal, approval in enumerate(fixture.approvals):
            _insert_immutable(
                self.approvals,
                {
                    "_id": f"approval:{fixture.fixture_hash}:{approval.approval_id}",
                    "fixture_hash": fixture.fixture_hash,
                    "ordinal": ordinal,
                    **_document(approval),
                },
                "approval",
            )

    def load_fixture(self, fixture_hash: str) -> Fixture:
        stored = _required(self.fleet.find_one({"_id": f"fixture:{fixture_hash}"}), "fixture")
        if stored.get("fixture_hash") != fixture_hash:
            raise FixtureMismatchError("Stored fixture hash differs from requested hash")
        approvals = list(self.approvals.find({"fixture_hash": fixture_hash}))
        if len(approvals) != stored.get("approval_count"):
            raise FixtureMismatchError("Stored approval count differs from fixture")
        ordinals = [a.get("ordinal") for a in approvals]
        if any(type(value) is not int for value in ordinals) or sorted(ordinals) != list(range(len(approvals))):
            raise FixtureMismatchError("Stored approval order is invalid")
        payload = {
            **_clean(stored, "approval_count"),
            "approvals": [_clean(a, "fixture_hash", "ordinal") for a in sorted(approvals, key=lambda a: a["ordinal"])],
        }
        try:
            fixture = Fixture.model_validate(payload)
            _checked_fixture(fixture)
        except (ValueError, TypeError) as exc:
            raise FixtureMismatchError("Persisted fixture or approvals failed validation") from exc
        return fixture

    def save_policy(self, policy: Policy) -> None:
        self.load_fixture(policy.fixture_hash)
        _insert_immutable(
            self.policies,
            {"_id": f"policy:{policy.policy_id}", "document_type": "policy", **_document(policy)},
            "policy",
        )

    def load_policy(self, policy_id: str) -> Policy:
        stored = _required(self.policies.find_one({"_id": f"policy:{policy_id}"}), "policy")
        if stored.get("document_type") != "policy" or stored.get("policy_id") != policy_id:
            raise MissingReferenceError("Referenced policy has inconsistent identity")
        policy = Policy.model_validate(_clean(stored, "document_type"))
        self.load_fixture(policy.fixture_hash)
        return policy

    def save_candidate(self, candidate: Candidate) -> None:
        _checked_candidate(candidate)
        base = self.load_policy(candidate.parent_policy_id)
        if candidate.policy.fixture_hash != base.fixture_hash or candidate.policy.version <= base.version:
            raise FixtureMismatchError("Candidate version or fixture differs from parent")
        _insert_immutable(
            self.policies,
            {"_id": f"candidate:{candidate.candidate_id}", "document_type": "candidate", **_document(candidate)},
            "candidate",
        )

    def load_candidate(self, candidate_id: str) -> Candidate:
        stored = _required(self.policies.find_one({"_id": f"candidate:{candidate_id}"}), "candidate")
        if stored.get("document_type") != "candidate" or stored.get("candidate_id") != candidate_id:
            raise MissingReferenceError("Referenced candidate has inconsistent identity")
        candidate = Candidate.model_validate(_clean(stored, "document_type"))
        _checked_candidate(candidate)
        return candidate

    def save_trial(self, trial: Trial) -> None:
        self.load_fixture(trial.fixture_hash)
        _insert_immutable(self.trials, {"_id": trial.trial_id, **_document(trial)}, "trial")

    def load_trial(self, trial_id: str) -> Trial:
        stored = _required(self.trials.find_one({"_id": trial_id}), "trial")
        if stored.get("trial_id") != trial_id:
            raise MissingReferenceError("Referenced trial has inconsistent identity")
        return Trial.model_validate(_clean(stored))

    def save_strategy(
        self, strategy: Strategy, *, run_id: str, version: int = 1,
        parent_strategy_id: str | None = None,
    ) -> None:
        # model_copy(update=...) bypasses validation. Revalidate the eight-decision budget.
        Strategy.model_validate(strategy.model_dump(mode="json"))
        if strategy.budget != 8 or version < 1:
            raise StoreError("Strategy violates its frozen budget or version")
        _insert_immutable(
            self.strategies,
            {
                "_id": f"strategy:{run_id}:{strategy.strategy_id}",
                "run_id": run_id,
                "version": version,
                "parent_strategy_id": parent_strategy_id,
                **_document(strategy),
            },
            "strategy",
        )

    def load_strategy(self, strategy_id: str, run_id: str) -> Strategy:
        stored = _required(
            self.strategies.find_one({"_id": f"strategy:{run_id}:{strategy_id}"}), "strategy"
        )
        if stored.get("run_id") != run_id or stored.get("strategy_id") != strategy_id:
            raise MissingReferenceError("Referenced strategy has inconsistent identity")
        strategy = Strategy.model_validate(_clean(stored, "run_id", "version", "parent_strategy_id"))
        if strategy.budget != 8:
            raise StoreError("Stored strategy violates its frozen budget")
        return strategy

    def save_evaluation(self, evaluation: BaseModel | Mapping[str, Any]) -> None:
        payload = _document(evaluation)
        evaluation_id = payload.get("evaluation_id")
        if not isinstance(evaluation_id, str) or not evaluation_id:
            raise StoreError("Evaluation must have a nonempty evaluation_id")
        _insert_immutable(self.evaluations, {"_id": evaluation_id, **payload}, "evaluation")

    def initialize_run(self, state: ActiveRunState) -> ActiveRunState:
        if state.revision != 0:
            raise StoreError("Initial run revision must be zero")
        fixture = self.load_fixture(state.fixture_hash)
        policy = self.load_policy(state.active_policy_id)
        self.load_strategy(state.active_strategy_id, state.run_id)
        if policy.fixture_hash != fixture.fixture_hash:
            raise FixtureMismatchError("Initial policy and run fixture differ")
        _insert_immutable(self.runs, {"_id": state.run_id, **_document(state)}, "run")
        return state

    def load_active_state(self, run_id: str) -> ActiveRuntimeState:
        stored = _required(self.runs.find_one({"_id": run_id}), "active run")
        if stored.get("run_id") != run_id:
            raise MissingReferenceError("Active run has inconsistent identity")
        state = ActiveRunState.model_validate(_clean(stored))
        fixture = self.load_fixture(state.fixture_hash)
        policy = self.load_policy(state.active_policy_id)
        strategy = self.load_strategy(state.active_strategy_id, run_id)
        if policy.fixture_hash != fixture.fixture_hash:
            raise FixtureMismatchError("Active policy fixture differs from run")
        return ActiveRuntimeState(state=state, policy=policy, strategy=strategy, fixture=fixture)

    def promote_policy(self, candidate: Candidate, passing_trial: Trial, expected_revision: int) -> ActiveRunState:
        """Move a policy pointer only with persisted full-gate PROMOTE evidence and CAS."""
        active = self.load_active_state(passing_trial.run_id)
        state = active.state
        if state.revision != expected_revision:
            raise StalePromotionError("Active run revision changed")
        _checked_candidate(candidate)
        gate = passing_trial.full_gate_result
        if (
            candidate.parent_policy_id != state.active_policy_id
            or candidate.policy.fixture_hash != state.fixture_hash
            or passing_trial.fixture_hash != state.fixture_hash
            or passing_trial.base_policy_id != state.active_policy_id
            or passing_trial.candidate_id != candidate.candidate_id
            or passing_trial.candidate_policy_id != candidate.policy.policy_id
            or passing_trial.strategy_id != state.active_strategy_id
            or passing_trial.verdict != "PROMOTE"
            or not gate.passed
            or gate.verdict != "PROMOTE"
            or gate.failure_classes
            or not gate.target_outcome
            or gate.regression_count != 0
            or gate.full_sweep_count <= 0
            or gate.candidate_hash != candidate.content_hash
            or gate.base_hash != active.policy.content_hash
            or gate.fixture_hash != state.fixture_hash
            or not gate.universe_hash
            or gate.universe_hash != passing_trial.universe_hash
        ):
            raise PromotionEvidenceError("Candidate, active state, and full-gate evidence do not align")
        # Reuse B's syntactic universe constructor here. This validates that
        # the persisted full-gate count/hash covers the complete candidate
        # universe, without reproducing evaluator or oracle logic in storage.
        from verdictloop.probes import candidate_universe, universe_hash

        try:
            universe = candidate_universe(active.policy, candidate.policy, active.fixture)
            complete = (
                len(universe) == gate.full_sweep_count
                and universe_hash(universe) == gate.universe_hash
            )
        except Exception:
            complete = False
        if not complete:
            raise PromotionEvidenceError("Full-gate evidence does not cover the candidate universe")
        if self.load_candidate(candidate.candidate_id) != candidate:
            raise PromotionEvidenceError("Persisted candidate differs from promotion request")
        if self.load_trial(passing_trial.trial_id) != passing_trial:
            raise PromotionEvidenceError("Persisted trial differs from promotion request")
        if self.load_policy(candidate.policy.policy_id) != candidate.policy:
            raise PromotionEvidenceError("Persisted policy differs from promotion request")
        update = self.runs.update_one(
            {
                "_id": state.run_id,
                "run_id": state.run_id,
                "revision": expected_revision,
                "active_policy_id": state.active_policy_id,
                "fixture_hash": state.fixture_hash,
            },
            {
                "$set": {
                    "active_policy_id": candidate.policy.policy_id,
                    "objective_metrics": {
                        **state.objective_metrics,
                        "approved_restart_allowed": gate.target_outcome,
                    },
                    "updated_at": utc_now().isoformat(),
                },
                "$inc": {"revision": 1},
            },
        )
        if update.matched_count != 1:
            raise StalePromotionError("Policy promotion compare-and-swap failed")
        promoted = self.load_active_state(state.run_id).state
        if promoted.revision != expected_revision + 1 or promoted.active_policy_id != candidate.policy.policy_id:
            raise StalePromotionError("Active state changed again after promotion")
        return promoted

    def promote_strategy(
        self, strategy: Strategy, evaluation: BaseModel | Mapping[str, Any], expected_revision: int
    ) -> ActiveRunState:
        """Move the evaluator pointer only after a persisted eligible evaluation."""
        payload = _document(evaluation)
        evaluation_id = payload.get("evaluation_id")
        if not isinstance(evaluation_id, str) or not evaluation_id:
            raise PromotionEvidenceError("Evaluation ID is required")
        stored = _required(self.evaluations.find_one({"_id": evaluation_id}), "evaluation")
        if _clean(stored) != payload or payload.get("promotion_eligible") is not True:
            raise PromotionEvidenceError("Persisted evaluation is missing or ineligible")
        run_id = payload.get("run_id")
        if not isinstance(run_id, str):
            raise PromotionEvidenceError("Evaluation run ID is required")
        active = self.load_active_state(run_id)
        state = active.state
        if state.revision != expected_revision:
            raise StalePromotionError("Active run revision changed")
        saved = _required(
            self.strategies.find_one({"_id": f"strategy:{run_id}:{strategy.strategy_id}"}), "strategy"
        )
        if (
            payload.get("fixture_hash") != state.fixture_hash
            or payload.get("strategy_id") != strategy.strategy_id
            or saved.get("parent_strategy_id") != state.active_strategy_id
            or self.load_strategy(strategy.strategy_id, run_id) != strategy
        ):
            raise PromotionEvidenceError("Strategy evaluation does not match active state")
        update = self.runs.update_one(
            {
                "_id": run_id,
                "run_id": run_id,
                "revision": expected_revision,
                "active_strategy_id": state.active_strategy_id,
                "fixture_hash": state.fixture_hash,
            },
            {
                "$set": {
                    "active_strategy_id": strategy.strategy_id,
                    "updated_at": utc_now().isoformat(),
                },
                "$inc": {"revision": 1},
            },
        )
        if update.matched_count != 1:
            raise StalePromotionError("Strategy promotion compare-and-swap failed")
        promoted = self.load_active_state(run_id).state
        if promoted.revision != expected_revision + 1 or promoted.active_strategy_id != strategy.strategy_id:
            raise StalePromotionError("Active state changed again after promotion")
        return promoted


VerdictLoopStore = CaseLawStore
