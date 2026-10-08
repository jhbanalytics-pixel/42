"""The shadow pair score on its own (core/understand/pair_score.py): the weights come from the run's own items, the
score never reads a recent sighting, and no combination of weak features passes the floors."""
from datetime import date, timedelta

import numpy as np
import pytest

from core.understand import cluster, pair_score

DAY = date(2026, 9, 28)
OLD = DAY - timedelta(days=60)


def at(cos, dim=8, towards=1):
    v = [0.0] * dim
    v[0], v[towards] = cos, float(np.sqrt(1 - cos * cos))
    return v


def a_cluster(vec, keywords=(), hashtags=(), sounds=()):
    return {"cluster_id": "c1", "centroid": list(vec), "keywords": list(keywords), "hashtags": list(hashtags),
            "sounds": list(sounds), "creators": []}


def an_item(vec, last_seen=OLD, keywords=(), hashtags=(), sounds=(), birth=None):
    item = {"item_id": "m", "centroid": list(vec), "last_seen": last_seen, "keywords": list(keywords),
            "hashtags": list(hashtags), "sounds": list(sounds), "creators": []}
    if birth is not None:
        item["birth_centroid"] = list(birth)
    return item


def background(n=24, generic=("fyp", "foryou", "viral"), skip=0):
    """n dormant items with a tag and a keyword of their own; all but the first `skip` carry the generic tags."""
    return [{"item_id": f"bg{i}", "centroid": at(0.0, towards=2 + i % 6), "last_seen": OLD,
             "keywords": [f"bgword{i}"], "hashtags": [f"bgtag{i}"] + ([] if i < skip else list(generic)),
             "sounds": [], "creators": []} for i in range(n)]


def verdict(c, item, pool):
    idf = pair_score.build_idf([c], [item] + pool)
    return pair_score.score_pair(c, item, pair_score._cosine(c["centroid"], item["centroid"]), idf)


def test_a_tag_on_nearly_every_item_weighs_near_zero_and_a_rare_one_weighs_much_without_any_list():
    docs = [{"zzcommon", f"own{i}"} for i in range(20)] + [{"fyp"}]
    w = pair_score.idf_weights(docs)
    assert w["zzcommon"] < 0.1
    assert w["fyp"] > 0.6
    assert w["own3"] > 0.6
    assert pair_score.idf_weights([{"a"}, {"a"}, {"a"}])["a"] == 0.0


def test_a_shared_generic_tag_adds_almost_nothing_to_the_score():
    c = a_cluster(at(1.0), hashtags=["fyp"])
    item = an_item(at(0.78), hashtags=["fyp"])
    generic, rare = verdict(c, item, background()), verdict(c, item, background(skip=23))
    assert generic["score"] < 0.5 <= rare["score"]
    assert (generic["accept"], rare["accept"]) == (False, True)


def test_the_score_is_identical_whatever_the_item_last_seen():
    c = a_cluster(at(1.0), keywords=["kota"], hashtags=["bgtag1"])
    scores = {verdict(c, an_item(at(0.8), DAY - timedelta(days=d), ["kota"], ["bgtag1"]), background())["score"]
              for d in (0, 1, 7, 8, 27, 28, 400)}
    assert len(scores) == 1


def test_a_recent_sighting_cannot_lift_a_weak_pair_over_the_line():
    c = a_cluster(at(1.0), hashtags=["bgtag1"])
    out = {d: verdict(c, an_item(at(0.72), DAY - timedelta(days=d), hashtags=["bgtag1"]), background())
           for d in (1, 60)}
    assert out[1] == out[60]


def test_no_amount_of_lexical_overlap_lifts_a_pair_under_the_cosine_floor():
    kw = [f"k{i}" for i in range(6)]
    c = a_cluster(at(1.0), keywords=kw, hashtags=["bgtag1", "bgtag2"])
    item = an_item(at(0.4), DAY - timedelta(days=1), kw, ["bgtag1", "bgtag2"])
    got = verdict(c, item, background())
    # Without the floor this pair would pass: the lexical terms outweigh the cosine term.
    assert got["logit"] > 0
    assert got["accept"] is False and got["reason"] == "cosine_floor"


def test_the_cosine_floor_is_in_code_and_below_the_variant_band_start():
    assert 0.5 <= pair_score.COSINE_FLOOR <= cluster.VARIANT_COSINE


def test_drift_guard_refuses_a_match_far_from_the_birth_centroid():
    c = a_cluster(at(1.0), keywords=["kota"], hashtags=["bgtag1"])
    seen = DAY - timedelta(days=2)
    drifted = verdict(c, an_item(at(0.92), seen, ["kota"], ["bgtag1"], birth=at(0.3, towards=3)), background())
    fresh = verdict(c, an_item(at(0.92), seen, ["kota"], ["bgtag1"], birth=at(0.9, towards=2)), background())
    unknown = verdict(c, an_item(at(0.92), seen, ["kota"], ["bgtag1"]), background())
    assert (drifted["accept"], drifted["reason"]) == (False, "drift")
    assert drifted["drift_cosine"] < pair_score.DRIFT_FLOOR
    assert fresh["accept"] is True and fresh["drift_cosine"] >= pair_score.DRIFT_FLOOR
    assert unknown["accept"] is True and unknown["drift_cosine"] is None


def test_idf_weights_are_the_plain_log_ratio_at_known_points():
    docs = [{"rare"}] + [{"x", f"own{i}"} for i in range(19)]
    w = pair_score.idf_weights(docs)
    assert w["rare"] == pytest.approx(0.7733, abs=1e-3)  # ln(21/2) / ln(21)
    assert w["x"] == pytest.approx(0.0153, abs=1e-3)  # ln(21/20) / ln(21)
    assert pair_score.IDF_POWER == 1.0


def test_a_tag_on_every_document_lifts_the_logit_by_nothing():
    c, item = a_cluster(at(1.0), hashtags=["fyp"]), an_item(at(0.78), hashtags=["fyp"])
    pool = background(70, generic=("fyp",))
    bare = verdict(a_cluster(at(1.0)), an_item(at(0.78)), pool)["logit"]
    assert verdict(c, item, pool)["logit"] == bare


def test_the_score_is_identical_whether_or_not_the_two_share_a_creator():
    c = a_cluster(at(1.0), keywords=["kota"], hashtags=["bgtag1"])
    c["creators"] = ["u1", "u2"]
    alone, shared = an_item(at(0.8), keywords=["kota"], hashtags=["bgtag1"]), an_item(at(0.8), keywords=["kota"], hashtags=["bgtag1"])
    alone["creators"], shared["creators"] = ["u9"], ["u1", "u2"]
    assert verdict(c, alone, background()) == verdict(c, shared, background())
