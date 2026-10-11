"""Optional local Workshop trace for a completed VerdictLoop trial.

Tracing is observational. It never changes the probe, oracle, Judge, or
promotion path, and its absence/failure cannot change an authorization result.
"""

from __future__ import annotations

import logging
import os
import json

from verdictloop.detector import detect_probe_violations, first_violation_index
from verdictloop.schema import Fixture
from verdictloop.store import Trial

logger = logging.getLogger(__name__)


def trace_trial(trial: Trial, fixture: Fixture) -> bool:
    """Mirror bounded probe and independent gate evidence to local Workshop."""
    if os.getenv("VERDICTLOOP_WORKSHOP") != "1":
        return False
    fast = trial.fast_probe_result
    if fast is None:
        return False
    try:
        import raindrop.analytics as raindrop

        raindrop.init(
            api_key=None,
            # SDK 0.0.75 disables OTel spans without a cloud API key;
            # local Workshop still accepts explicit begin/finish events.
            tracing_enabled=False,
            auto_instrument=False,
            app_git=False,
            local_workshop_url=os.getenv(
                "RAINDROP_LOCAL_DEBUGGER", "http://127.0.0.1:5899/v1/"
            ),
        )
        detected = detect_probe_violations(list(fast.observations), fixture)
        trace_input = {
            "candidate_id": trial.candidate_id,
            "strategy_id": trial.strategy_id,
            "fixture_hash": trial.fixture_hash,
            "universe_hash": trial.universe_hash,
            "probe_seed": fast.seed,
            "probe_budget": fast.budget,
            "probe_decisions": [
                {"index": observation.index,
                 "case_id": observation.case_id,
                 "decision": observation.decision}
                for observation in fast.observations
            ],
        }
        trace_output = {
            "fast_probe_detected": bool(detected),
            "first_violation_index": first_violation_index(detected) or "NOT FOUND",
            "full_gate_verdict": trial.full_gate_result.verdict,
            "full_gate_failure_classes": [
                item.value for item in trial.full_gate_result.failure_classes
            ],
            "full_sweep_count": trial.full_gate_result.full_sweep_count,
            "blind_spot": not detected and trial.full_gate_result.verdict == "REJECT",
        }
        interaction = raindrop.begin(
            user_id="verdictloop-local",
            event="verdictloop_evaluator_trial",
            input=json.dumps(trace_input, sort_keys=True),
            properties={"execution_kind": "deterministic_evaluator"},
        )
        interaction.finish(output=json.dumps(trace_output, sort_keys=True))
        raindrop.flush()
        return True
    except Exception as exc:
        logger.warning("Optional Workshop trace unavailable: %s", type(exc).__name__)
        return False
