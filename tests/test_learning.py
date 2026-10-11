import pytest
from verdictloop.learning import ALLOCATIONS, AllocationScore, improves, select_strategy
from verdictloop.mutations import mutate
from verdictloop.probes import candidate_universe, probe
from verdictloop.schema import FailureClass

SEEDS = (1, 2, 3, 4, 5)


@pytest.fixture(scope="module")
def report(fx):
    from tests.conftest import make_policy
    v1 = make_policy(fx, policy_id="policy-v1", version=1)
    return select_strategy(v1, mutate(v1, fx, 1, "train"), mutate(v1, fx, 1, "validation"), SEEDS, fx)


def test_train_identifies_alias_inconsistency_as_missed(report):
    assert report.train_missed.get(FailureClass.ALIAS_INCONSISTENCY.value, 0) > 0


def test_selection_changes_to_an_alias_allocation(report):
    assert report.changed and report.selected.alias_decisions in (2, 4)


def test_selected_does_not_regress_non_alias_or_safe(report):
    scores = {s.strategy_id: s for s in report.validation_scores}
    cur, new = scores["uniform-v1"], scores[report.selected.strategy_id]
    assert new.non_alias_detected >= cur.non_alias_detected
    assert new.safe_false_alarms == cur.safe_false_alarms == 0


def test_selection_is_deterministic(fx, report):
    from tests.conftest import make_policy
    v1 = make_policy(fx, policy_id="policy-v1", version=1)
    again = select_strategy(v1, mutate(v1, fx, 1, "train"), mutate(v1, fx, 1, "validation"), SEEDS, fx)
    assert again.selected == report.selected


def test_heldout_candidates_rejected_by_selection(fx, v1):
    held = mutate(v1, fx, 1, "heldout")
    with pytest.raises(AssertionError):
        select_strategy(v1, held, mutate(v1, fx, 1, "validation"), SEEDS, fx)


def _score(rate, first, non_alias=5, alarms=0):
    return AllocationScore(strategy_id="s", alias_decisions=0, violating_trials=10, detected_trials=0,
                           detection_rate=rate, mean_probes_to_first=first, non_alias_trials=5,
                           non_alias_detected=non_alias, safe_trials=5, safe_false_alarms=alarms)


def test_improves_requires_strict_gain_without_regression():
    cur = _score(0.5, 5.0)
    assert improves(_score(0.6, 5.0), cur)
    assert improves(_score(0.5, 4.0), cur)
    assert not improves(_score(0.5, 5.0), cur)          # no gain: keep current
    assert not improves(_score(0.9, 2.0, non_alias=4), cur)  # non-alias regression
    assert not improves(_score(0.9, 2.0, alarms=1), cur)     # safe-control regression


# ---- alias-pair probing -------------------------------------------------


@pytest.mark.parametrize("strategy", ALLOCATIONS)
def test_allocation_never_exceeds_budget(fx, v1, raw_label, strategy):
    u = candidate_universe(v1, raw_label, fx)
    r = probe(raw_label, u, strategy, 8, seed=3, fixture=fx)
    assert r.decisions_used == 8
    assert len({o.case_id for o in r.observations}) == 8
    assert {o.case_id for o in r.observations} <= {c.case_id for c in u}


def test_alias_pairs_lead_and_share_resource(fx, v1, raw_label):
    u = candidate_universe(v1, raw_label, fx)
    r = probe(raw_label, u, ALLOCATIONS[2], 8, seed=3, fixture=fx)
    for a, b in ((0, 1), (2, 3)):
        oa, ob = r.observations[a], r.observations[b]
        assert oa.resource_id == ob.resource_id
        assert oa.call.env_label != ob.call.env_label
        assert oa.call.model_dump(exclude={"env_label"}) == ob.call.model_dump(exclude={"env_label"})


def test_same_universe_hash_across_strategies(fx, v1, raw_label):
    u = candidate_universe(v1, raw_label, fx)
    hashes = {probe(raw_label, u, s, 8, 17, fixture=fx).universe_hash for s in ALLOCATIONS}
    assert len(hashes) == 1
