"""Shared, frozen data contracts for VerdictLoop.

Every module exchanges these Pydantic models. Change them only by agreement.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROBE_BUDGET = 8
HELD_OUT_SEEDS = (17, 29, 43, 71, 97)
WILDCARD = "*"

EnvCanonical = Literal["staging", "production"]
Split = Literal["train", "validation", "heldout", "demo"]
Verdict = Literal["PROMOTE", "REJECT"]


def stable_hash(obj: Any) -> str:
    """Deterministic content hash of any JSON-serialisable value or model."""
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------
# Fleet / fixture (trusted metadata)
# --------------------------------------------------------------------------


class Resource(_Frozen):
    resource_id: str
    tenant: str
    service: str
    env_canonical: EnvCanonical
    labels: tuple[str, ...]  # raw env labels that resolve here within tenant/service


class ResolvedResource(_Frozen):
    resource_id: str
    tenant: str
    service: str
    env_canonical: EnvCanonical


class Actor(_Frozen):
    actor_id: str
    tenant: str | None  # None = belongs to no tenant
    services: tuple[str, ...] = ()


class Approval(_Frozen):
    approval_id: str
    context_id: str
    actor: str
    tool: str
    resource_id: str
    incident_id: str
    not_before: int
    expires_at: int  # exclusive


class Fixture(_Frozen):
    fixture_id: str
    resources: tuple[Resource, ...]
    actors: tuple[Actor, ...]
    tools: tuple[str, ...]
    incidents: tuple[str, ...]
    approval_contexts: tuple[str, ...]
    approvals: tuple[Approval, ...]
    eval_times: tuple[int, ...]
    fixture_hash: str = ""

    def content_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json", exclude={"fixture_hash"}))

    def approvals_for(self, context_id: str) -> list[Approval]:
        return [a for a in self.approvals if a.context_id == context_id]


# --------------------------------------------------------------------------
# Calls
# --------------------------------------------------------------------------


class Call(_Frozen):
    actor: str
    tool: str
    tenant: str
    service: str
    env_label: str  # raw caller input, untrusted
    incident_id: str
    approval_context_id: str
    eval_time: int

    @property
    def case_id(self) -> str:
        return "case-" + stable_hash(self)


# --------------------------------------------------------------------------
# Policy (bounded, allow-only, default deny)
# --------------------------------------------------------------------------


class Selector(_Frozen):
    field: Literal["resource_id", "env_canonical", "env_label"]
    values: tuple[str, ...] = Field(min_length=1)


class PolicyRule(_Frozen):
    rule_id: str
    effect: Literal["allow"] = "allow"
    tools: tuple[str, ...] = Field(min_length=1)
    tenants: tuple[str, ...] = Field(min_length=1)  # may contain "*"
    services: tuple[str, ...] = Field(min_length=1)  # may contain "*"
    actors: tuple[str, ...] = (WILDCARD,)
    selector: Selector | None = None  # None = any environment in scope
    require_valid_approval: bool = True


class Policy(_Frozen):
    policy_id: str
    version: int
    fixture_hash: str
    rules: tuple[PolicyRule, ...] = ()

    @property
    def content_hash(self) -> str:
        return stable_hash([r.model_dump(mode="json") for r in self.rules])


class Candidate(_Frozen):
    candidate_id: str
    parent_policy_id: str
    policy: Policy
    mutation_class: str
    seed: int
    split: Split
    content_hash: str
    producer: str = "deterministic"


# --------------------------------------------------------------------------
# Probing, detection, verdicts
# --------------------------------------------------------------------------


class Strategy(_Frozen):
    strategy_id: str
    alias_decisions: Literal[0, 2, 4] = 0
    budget: int = PROBE_BUDGET


class ProbeObservation(_Frozen):
    index: int = Field(ge=1, le=PROBE_BUDGET)
    case_id: str
    call: Call
    decision: bool
    resource_id: str | None
    approval_context_id: str
    fixture_hash: str
    universe_hash: str


class ProbeResult(_Frozen):
    strategy_id: str
    alias_decisions: int
    seed: int
    budget: int
    decisions_used: int
    candidate_hash: str
    universe_hash: str
    fixture_hash: str
    observations: tuple[ProbeObservation, ...]
    duration_ms: float


class FailureClass(str, Enum):
    ALIAS_INCONSISTENCY = "ALIAS_INCONSISTENCY"
    UNAUTHORIZED_GRANT = "UNAUTHORIZED_GRANT"
    INCORRECT_DENIAL = "INCORRECT_DENIAL"
    LEGITIMATE_TASK_REGRESSION = "LEGITIMATE_TASK_REGRESSION"
    TARGET_NOT_SATISFIED = "TARGET_NOT_SATISFIED"
    INCOMPLETE_EVALUATION = "INCOMPLETE_EVALUATION"


class Counterexample(_Frozen):
    failure_class: FailureClass
    case_ids: tuple[str, ...]
    policy_decisions: tuple[bool, ...]
    oracle_decisions: tuple[bool, ...] = ()
    # Oracle-derived severity of an alias inconsistency (UNAUTHORIZED_GRANT or
    # INCORRECT_DENIAL); None for single-call counterexamples.
    severity: FailureClass | None = None
    # Probe index (1..8) at which evidence became sufficient; None in the full gate.
    first_evidence_index: int | None = None
    detail: str = ""


class GateResult(_Frozen):
    passed: bool
    verdict: Verdict
    failure_classes: tuple[FailureClass, ...]
    evidence_case_ids: tuple[str, ...]
    counterexamples: tuple[Counterexample, ...]
    full_sweep_count: int
    target_outcome: bool
    regression_count: int
    candidate_hash: str
    base_hash: str
    fixture_hash: str
    universe_hash: str = ""
