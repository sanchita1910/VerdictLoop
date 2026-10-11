from verdictloop import mutations
from verdictloop.judge import admit
from verdictloop.mutations import mutate


def test_five_candidates_per_split_with_provenance(fx, v1):
    for split in ("train", "validation", "heldout"):
        cands = mutate(v1, fx, seed=17, split=split)
        assert len(cands) == 5
        for c in cands:
            assert c.parent_policy_id == v1.policy_id
            assert c.split == split and c.seed == 17
            assert c.content_hash == c.policy.content_hash
            assert c.mutation_class in mutations.RECIPE


def test_mutation_is_deterministic(fx, v1):
    assert mutate(v1, fx, 17, "heldout") == mutate(v1, fx, 17, "heldout")


def test_splits_use_disjoint_resources(fx, v1):
    services = {
        split: {c.triggering_target.service for c in mutate(v1, fx, 17, split)}
        for split in ("train", "validation", "heldout")
    }
    assert services["train"].isdisjoint(services["validation"])
    assert services["train"].isdisjoint(services["heldout"])
    assert services["validation"].isdisjoint(services["heldout"])


def test_gate_not_mutation_decides(fx, v1):
    verdicts = {}
    for c in mutate(v1, fx, 17, "heldout"):
        verdicts.setdefault(c.mutation_class, set()).add(
            admit(v1, c.policy, c.triggering_target, fx).verdict)
    assert verdicts[mutations.UNMUTATED] == {"PROMOTE"}
    assert verdicts[mutations.TENANT_WIDENED] == {"PROMOTE"}
    assert verdicts[mutations.APPROVAL_REMOVED] == {"REJECT"}
    assert verdicts[mutations.RAW_LABEL] == {"REJECT"}


def test_manifest_is_stable(fx, v1):
    a = mutations.manifest(mutate(v1, fx, 17, "heldout"))
    b = mutations.manifest(mutate(v1, fx, 17, "heldout"))
    assert a == b and len(a["candidates"]) == 5
