import json

import pytest

from verdictloop.evaluation import TRAIN_VALIDATION_SEEDS, build_scorecard, evaluate
from verdictloop.learning import ALLOCATIONS
from verdictloop.mutations import mutate
from verdictloop.schema import HELD_OUT_SEEDS


@pytest.fixture(scope="module")
def card():
    return build_scorecard()


def test_train_validation_seeds_disjoint_from_heldout():
    assert not set(TRAIN_VALIDATION_SEEDS) & set(HELD_OUT_SEEDS)


def test_scorecard_is_json_serialisable(card):
    assert json.loads(json.dumps(card))["budget"] == 8


def test_five_heldout_candidates_by_five_seeds(card):
    assert len(card["candidates"]) == 5
    for side in ("baseline", "learned"):
        s = card["summary"][side]
        assert s["violating_trials_total"] + s["safe_control_trials"] == 25
        assert s["decisions_used"] == 25 * 8


def test_first_hits_in_range(card):
    for row in card["candidates"]:
        for strat in row["strategies"].values():
            assert all(h == "NOT FOUND" or 1 <= h <= 8 for h in strat["first_hits"])


def test_full_gate_verdicts_independent_of_strategy(card):
    for row in card["candidates"]:
        assert row["gate_verdict"] in ("PROMOTE", "REJECT")
        assert set(row["strategies"]) == {"uniform-v1", card["selection"]["selected"]["strategy_id"]}


def test_manifest_covers_all_splits_before_selection(card):
    splits = {c["split"] for c in card["manifest"]["candidates"]}
    assert splits == {"train", "validation", "heldout"}


def test_evaluate_refuses_non_heldout(fx, v1):
    with pytest.raises(AssertionError):
        evaluate(v1, mutate(v1, fx, 1, "validation"), ALLOCATIONS[0], ALLOCATIONS[1], (17,), fx)
