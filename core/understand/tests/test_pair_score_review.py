"""Conditions from the independent review of the shadow pair score: inputs are never mutated, a failing sink is
visible in the run counts, the cosine-only boundary matches the vote rule's, and the pieces a mutation pass showed
unpinned (tag and keyword normalisation, the choice among accepted candidates, the variant path, the zero vector,
empty documents in the corpus)."""
import copy
import json
import pickle
import random
from datetime import timedelta

import numpy as np
import pytest

from core.understand import cluster, pair_score
from core.understand.tests.test_pair_score_shadow import (DAY, OLD, a_cluster, an_item, at, background,
                                                          shadow_of)
from core.understand.tests import test_cluster as tc


# The shadow never mutates what it is handed

TAGS = ["fyp", "#Viral", "kota", "#Amapiano", "eskom", "BGTAG1", "#fyp"]
WORDS = ["Zulu", "alpha", "Beta", "gamma", "delta", "kota", "eskom", "Stage"]


def harness_case(seed):
    """Clusters and items with None fields, # and mixed case tags, unsorted keywords with repeats, numpy centroids,
    tied cosines, some birth centroids and some malformed ones. Everything plan() reads is present."""
    rng = random.Random(seed)
    clusters, items = [], []
    for k in range(rng.randint(1, 5)):
        vec = np.array(at(rng.choice([1.0, 0.95, 0.9]), towards=rng.randint(1, 6)))
        clusters.append({
            "cluster_id": f"20260928-za-{k:03d}", "centroid": vec,
            "keywords": rng.sample(WORDS, rng.randint(0, 6)) + rng.sample(WORDS, 1),
            "hashtags": rng.sample(TAGS, rng.randint(0, 3)), "sounds": rng.sample(["s1", "s2"], rng.randint(0, 2)),
            "creators": rng.sample(["u1", "u2", "u3"], rng.randint(0, 3)), "label": f"label {k}",
            "members": [(f"p{seed}-{k}-{m}", 0.5) for m in range(2)], "market": "za", "platform": "tiktok",
            "local_terms": []})
    for k in range(rng.randint(0, 8)):
        item = an_item(f"item{k}", at(rng.choice([0.3, 0.7, 0.75, 0.9, 0.9]), towards=rng.randint(1, 6)),
                       DAY - timedelta(days=rng.choice([0, 3, 7, 8, 28, 90])),
                       keywords=rng.sample(WORDS, rng.randint(0, 6)), hashtags=rng.sample(TAGS, rng.randint(0, 3)),
                       sounds=rng.sample(["s1", "s2"], rng.randint(0, 2)),
                       creators=rng.sample(["u1", "u2", "u3"], rng.randint(0, 3)))
        for field in ("keywords", "hashtags", "sounds", "creators"):
            if rng.random() < 0.15:
                item[field] = None
        item["centroid"] = np.array(item["centroid"])
        roll = rng.random()
        if roll < 0.3:
            item["birth_centroid"] = np.array(at(rng.choice([0.2, 0.9]), towards=rng.randint(1, 6)))
        items.append(item)
    return clusters, items


@pytest.mark.parametrize("seed", range(300))
def test_the_shadow_never_mutates_its_inputs_and_leaves_the_plan_unchanged(seed):
    clusters, items = harness_case(seed)
    plain_clusters, plain_items = copy.deepcopy(clusters), copy.deepcopy(items)
    want_decisions = cluster.assign(plain_clusters, plain_items, DAY)
    want_plan = cluster.plan(plain_clusters, want_decisions, plain_items, DAY, "za")
    before = pickle.dumps((clusters, items))
    shadow = []
    got_decisions = cluster.assign(clusters, items, DAY, shadow=shadow)
    assert pickle.dumps((clusters, items)) == before
    assert json.dumps(got_decisions, sort_keys=True, default=str) == json.dumps(want_decisions, sort_keys=True, default=str)
    assert json.dumps(cluster.plan(clusters, got_decisions, items, DAY, "za"), sort_keys=True, default=str) == \
        json.dumps(want_plan, sort_keys=True, default=str)
    assert not any("shadow_error" in r for r in shadow)


# A failing sink is visible, and a sink that is off costs nothing

def run_with_sink(monkeypatch, sink):
    items = [an_item("X", at(0.95), DAY - timedelta(days=1), keywords=["kota"], hashtags=["fyp"])] + background(6)
    clusters = tc.synthetic_clusters(3, dim=8, spread=0.05)
    for c in clusters:
        c["centroid"], c["hashtags"] = at(1.0), ["fyp"]
    monkeypatch.setattr(cluster, "SHADOW_SINK", sink)
    return tc.fake_run(monkeypatch, clusters, items=items)


def test_a_sink_that_raises_is_named_in_the_run_counts_and_changes_nothing_else(monkeypatch):
    off_counts, off_writes = run_with_sink(monkeypatch, None)

    def broken(market, run_date, records):
        raise RuntimeError("sink down with a secret detail")

    on_counts, on_writes = run_with_sink(monkeypatch, broken)
    assert on_counts.pop("shadow_error") == "RuntimeError"
    assert "shadow_error" not in off_counts
    assert json.dumps(on_counts, sort_keys=True, default=str) == json.dumps(off_counts, sort_keys=True, default=str)
    assert json.dumps(on_writes, sort_keys=True, default=str) == json.dumps(off_writes, sort_keys=True, default=str)


def test_a_shadow_that_fails_inside_a_run_is_named_in_the_run_counts(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("secret detail")

    monkeypatch.setattr(pair_score, "shadow_records", boom)
    got = []
    counts, _ = run_with_sink(monkeypatch, lambda market, run_date, records: got.append(records))
    assert counts["shadow_error"] == "ValueError"
    assert got == [[{"shadow_error": "ValueError"}]]


def test_with_no_sink_the_shadow_is_never_computed_and_the_counts_carry_no_key(monkeypatch):
    calls = []
    real = pair_score.shadow_records
    monkeypatch.setattr(pair_score, "shadow_records", lambda *a, **k: calls.append(1) or real(*a, **k))
    counts, _ = run_with_sink(monkeypatch, None)
    assert calls == [] and "shadow_error" not in counts


def test_a_working_sink_leaves_no_error_key(monkeypatch):
    counts, _ = run_with_sink(monkeypatch, lambda market, run_date, records: None)
    assert "shadow_error" not in counts


# The cosine only boundary is the vote rule's

def verdict_at(cos, pool=None):
    c = a_cluster("c1", at(1.0), keywords=["alpha"], hashtags=["x1"])
    item = an_item("m", at(1.0), OLD, keywords=["beta"], hashtags=["y1"])
    idf = pair_score.build_idf([c], [item] + (pool if pool is not None else background()))
    return pair_score.score_pair(c, item, cos, idf)


def test_the_shadow_midpoint_is_the_vote_rules_cosine_vote_threshold():
    assert pair_score.COSINE_MID == cluster.MATCH_COSINE


@pytest.mark.parametrize("cos, accept", [(0.799999, False), (0.80, False), (0.81, False), (0.819999, False),
                                         (0.82, True), (0.820001, True), (0.95, True)])
def test_with_nothing_shared_a_pair_is_accepted_from_exactly_the_vote_rules_cosine_threshold(cos, accept):
    assert verdict_at(cos)["accept"] is accept


def test_the_shadow_and_the_vote_rule_agree_on_which_side_of_the_boundary_a_cosine_only_pair_is():
    for cos, vote in [(0.81, False), (0.82, True)]:
        c = a_cluster("c1", at(1.0))
        item = an_item("m", at(cos), OLD)
        [today] = cluster.assign([c], [item], DAY)
        assert ("cosine" in today["votes"]) is vote
        assert verdict_at(cos)["accept"] is vote


# Normalisation, the empty documents and the zero vector

def test_tags_compare_without_case_or_hash():
    pool = background()
    plain = shadow_of([a_cluster("c1", at(1.0), hashtags=["kota"])], [an_item("m", at(0.85), OLD, hashtags=["kota"])] + pool, "c1", "m")[1][0]
    marked = shadow_of([a_cluster("c1", at(1.0), hashtags=["#KOTA"])], [an_item("m", at(0.85), OLD, hashtags=["kota"])] + pool, "c1", "m")[1][0]
    none = shadow_of([a_cluster("c1", at(1.0), hashtags=["other"])], [an_item("m", at(0.85), OLD, hashtags=["kota"])] + pool, "c1", "m")[1][0]
    assert marked["shadow_logit"] == plain["shadow_logit"] > none["shadow_logit"]


def test_keywords_compare_without_case():
    pool = background()
    low = shadow_of([a_cluster("c1", at(1.0), keywords=["kota"])], [an_item("m", at(0.85), OLD, keywords=["kota"])] + pool, "c1", "m")[1][0]
    up = shadow_of([a_cluster("c1", at(1.0), keywords=["KoTa"])], [an_item("m", at(0.85), OLD, keywords=["kota"])] + pool, "c1", "m")[1][0]
    none = shadow_of([a_cluster("c1", at(1.0), keywords=["other"])], [an_item("m", at(0.85), OLD, keywords=["kota"])] + pool, "c1", "m")[1][0]
    assert up["shadow_logit"] == low["shadow_logit"] > none["shadow_logit"]


def test_a_sound_and_a_hashtag_of_the_same_text_are_different_facets():
    a = pair_score._facets({"hashtags": ["x"], "sounds": []})
    b = pair_score._facets({"hashtags": [], "sounds": ["x"]})
    assert a.isdisjoint(b)


def test_empty_documents_do_not_change_any_weight():
    docs = [{"a", "b"}, {"a"}, {"c"}, {"a", "d"}]
    assert pair_score.idf_weights(docs) == pair_score.idf_weights(docs + [set()] * 5)
    pool = background()
    empties = [an_item(f"e{i}", at(0.0, towards=3), OLD) for i in range(10)]
    c, item = a_cluster("c1", at(1.0), hashtags=["bgtag1"], keywords=["bgword1"]), an_item("m", at(0.9), OLD, hashtags=["bgtag1"], keywords=["bgword1"])
    without = pair_score.build_idf([c], [item] + pool)
    with_empties = pair_score.build_idf([c], [item] + pool + empties)
    assert without["facets"] == with_empties["facets"] and without["keywords"] == with_empties["keywords"]


def test_a_zero_vector_has_cosine_zero_and_a_zero_birth_centroid_is_drift():
    assert pair_score._cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert pair_score._cosine([1.0, 0.0], [0.0, 0.0]) == 0.0
    c = a_cluster("c1", at(1.0), keywords=["kota"], hashtags=["bgtag1"])
    item = an_item("m", at(0.95), DAY - timedelta(days=2), keywords=["kota"], hashtags=["bgtag1"], birth=[0.0] * 8)
    got = pair_score.score_pair(c, item, 0.95, pair_score.build_idf([c], [item] + background()))
    assert (got["drift_cosine"], got["reason"]) == (0.0, "drift")


def test_a_malformed_birth_centroid_costs_only_its_own_pair():
    c = a_cluster("c1", at(1.0), keywords=["kota"], hashtags=["bgtag1"])
    bad = an_item("bad", at(0.95), DAY - timedelta(days=2), keywords=["kota"], hashtags=["bgtag1"], birth=[1.0, 2.0])
    good = an_item("good", at(0.95, towards=2), DAY - timedelta(days=2), keywords=["kota"], hashtags=["bgtag1"])
    _, rows = shadow_of([c], [bad, good] + background())
    assert not any("shadow_error" in r for r in rows)
    by = {r["item_id"]: r for r in rows}
    assert by["bad"]["drift_cosine"] is None and by["good"]["shadow_accept"] is True


@pytest.mark.parametrize("birth", [[float("nan")] + [1.0] * 7, [float("inf")] + [1.0] * 7, [None] + [1.0] * 7,
                                   "abc", {"a": 1}, [[1.0], [1.0, 2.0]], 5, [1.0, 2.0]])
def test_any_malformed_birth_centroid_leaves_the_other_pairs_scored_and_the_rows_clean(birth):
    c = a_cluster("c1", at(1.0), keywords=["kota"], hashtags=["bgtag1"])
    bad = an_item("bad", at(0.95), DAY - timedelta(days=2), keywords=["kota"], hashtags=["bgtag1"])
    bad["birth_centroid"] = birth
    good = an_item("good", at(0.95, towards=2), DAY - timedelta(days=2), keywords=["kota"], hashtags=["bgtag1"])
    _, rows = shadow_of([c], [bad, good] + background())
    assert not any("shadow_error" in r for r in rows)
    by = {r["item_id"]: r for r in rows}
    assert by["bad"]["drift_cosine"] is None and by["bad"]["shadow_reason"] == "accept"
    assert by["good"]["shadow_accept"] is True
    json.dumps(rows, allow_nan=False)  # a nan would not survive the sink's JSON


# The shadow decision among candidates

def test_among_several_accepted_candidates_the_highest_score_wins_and_a_tie_goes_to_the_smaller_item_id():
    c = a_cluster("c1", at(1.0), keywords=["alpha"], hashtags=["x1"])
    low = an_item("a_low", at(0.85), OLD)
    high = an_item("z_high", at(0.95), OLD)
    mid = an_item("m_mid", at(0.90), OLD)
    _, rows = shadow_of([c], [low, high, mid] + background())
    assert {r["shadow_item_id"] for r in rows} == {"z_high"}
    first, second = an_item("b_twin", at(0.95), OLD), an_item("a_twin", at(0.95, towards=2), OLD)
    for order in ([first, second], [second, first]):  # the smaller id wins whichever is listed first
        _, rows = shadow_of([c], order + background())
        assert {r["shadow_item_id"] for r in rows} == {"a_twin"}


def test_a_higher_score_beats_a_higher_cosine_among_accepted_candidates():
    c = a_cluster("c1", at(1.0), keywords=["bgword1"], hashtags=["bgtag1"])
    near = an_item("a_near", at(0.90), OLD)
    shared = an_item("z_shared", at(0.88, towards=2), OLD, keywords=["bgword1"], hashtags=["bgtag1"])
    decisions, rows = shadow_of([c], [near, shared] + background())
    by = {r["item_id"]: r for r in rows}
    assert by["a_near"]["shadow_accept"] and by["z_shared"]["shadow_accept"]
    assert by["z_shared"]["shadow_score"] > by["a_near"]["shadow_score"] and by["a_near"]["cosine"] > by["z_shared"]["cosine"]
    assert {r["shadow_item_id"] for r in rows} == {"z_shared"}


def test_a_recurrence_needs_28_days_in_the_shadow_as_in_the_vote_rule():
    c = a_cluster("c1", at(1.0))
    for days, kind in [(27, "match"), (28, "recurrence")]:
        _, [row] = shadow_of([c], [an_item("m", at(0.95), DAY - timedelta(days=days))], "c1", "m")
        assert row["shadow_kind"] == kind


def test_a_refused_pair_is_a_variant_from_cosine_0_70_and_new_below_it():
    c = a_cluster("c1", at(1.0))
    for cos, kind, parent in [(0.75, "variant", "m"), (0.70, "variant", "m"), (0.69, "new", None)]:
        _, [row] = shadow_of([c], [an_item("m", at(cos), OLD)], "c1", "m")
        assert row["shadow_accept"] is False
        assert (row["shadow_kind"], row["shadow_item_id"]) == (kind, parent)


def test_the_variant_parent_is_the_nearest_candidate_and_a_drift_refusal_still_gives_a_variant():
    c = a_cluster("c1", at(1.0))
    far, near = an_item("a_far", at(0.72), OLD), an_item("b_near", at(0.76, towards=2), OLD)
    _, rows = shadow_of([c], [far, near])
    assert {(r["shadow_kind"], r["shadow_item_id"]) for r in rows} == {("variant", "b_near")}
    drifted = an_item("d", at(0.92), OLD, birth=at(0.1, towards=3))
    _, [row] = shadow_of([c], [drifted], "c1", "d")
    assert (row["shadow_reason"], row["shadow_kind"], row["shadow_item_id"]) == ("drift", "variant", "d")
