"""The shadow pair score (core/understand/pair_score.py) and its record-only call from cluster.assign.

The score runs beside today's vote rule and changes nothing it decides. These tests pin the three things that have to
hold: the shadow rejects what the vote rule lets through on weak evidence, it never reads a recent-sighting vote, and
the vote rule's own decisions are byte-identical with the shadow on or off."""
import json
import random
from datetime import date, datetime, timedelta

import numpy as np
import pytest

from core.understand import cluster, pair_score

DAY = date(2026, 9, 28)
OLD = DAY - timedelta(days=60)


def at(cos, dim=8, towards=1):
    """A unit vector at the given cosine from the first axis."""
    v = [0.0] * dim
    v[0], v[towards] = cos, float(np.sqrt(1 - cos * cos))
    return v


def a_cluster(cid, vec, keywords=(), hashtags=(), sounds=(), creators=()):
    return {"cluster_id": cid, "centroid": list(vec), "keywords": list(keywords), "hashtags": list(hashtags),
            "sounds": list(sounds), "creators": list(creators)}


def an_item(iid, vec, last_seen, keywords=(), hashtags=(), sounds=(), creators=(), birth=None):
    item = {"item_id": iid, "kind": "topic", "canonical_key": f"topic:{iid}", "label": iid, "aliases": [],
            "parent_item_id": None, "centroid": list(vec), "first_seen": date(2026, 6, 1), "first_seen_market": "za",
            "first_seen_platform": "tiktok", "last_seen": last_seen, "recurrences": 0, "lifecycle": None,
            "status": "active", "rejected_until": None, "keywords": list(keywords), "hashtags": list(hashtags),
            "sounds": list(sounds), "creators": list(creators), "valid_from": datetime(2026, 7, 1)}
    if birth is not None:
        item["birth_centroid"] = list(birth)
    return item


def background(n=24, generic=("fyp", "foryou", "viral"), skip=0):
    """n dormant items far from every test cluster. All but the first `skip` carry the generic tags, so the run's own
    items show those tags to be everywhere; each has a tag and a keyword of its own."""
    return [an_item(f"bg{i}", at(0.0, towards=2 + i % 6), OLD, keywords=[f"bgword{i}"],
                    hashtags=[f"bgtag{i}"] + ([] if i < skip else list(generic))) for i in range(n)]


def shadow_of(clusters, items, cid=None, iid=None):
    shadow = []
    decisions = cluster.assign(clusters, items, DAY, shadow=shadow)
    rows = [r for r in shadow if (cid is None or r["cluster_id"] == cid) and (iid is None or r["item_id"] == iid)]
    return decisions, rows


# The case from the review

def test_recent_vote_plus_one_generic_hashtag_at_low_cosine_is_eligible_today_and_rejected_by_the_shadow():
    c = a_cluster("c1", at(1.0), hashtags=["fyp"])
    target = an_item("target", at(0.4), DAY - timedelta(days=1), hashtags=["fyp"])
    decisions, [row] = shadow_of([c], [target] + background(), "c1", "target")
    [today] = decisions
    assert (today["kind"], today["item_id"], today["votes"]) == ("match", "target", ["hashtag_or_sound", "recent"])
    assert row["today_eligible"] is True and row["today_kind"] == "match"
    assert row["shadow_accept"] is False
    assert row["shadow_reason"] == "cosine_floor"
    assert row["shadow_kind"] == "new" and row["shadow_item_id"] is None


def test_the_same_pair_with_a_rare_hashtag_is_still_rejected_at_cosine_0_4():
    c = a_cluster("c1", at(1.0), hashtags=["bgtag3"])
    target = an_item("target", at(0.4), DAY - timedelta(days=1), hashtags=["bgtag3"])
    _, [row] = shadow_of([c], [target] + background(), "c1", "target")
    assert row["today_eligible"] is True and row["shadow_accept"] is False


def test_a_cosine_0_95_pair_with_a_dormant_item_and_no_shared_words_is_a_recurrence_only_in_the_shadow():
    c = a_cluster("c1", at(1.0), keywords=["alpha"], hashtags=["x1"])
    dormant = an_item("dormant", at(0.95), DAY - timedelta(days=40), keywords=["beta"], hashtags=["y1"])
    decisions, [row] = shadow_of([c], [dormant] + background(), "c1", "dormant")
    assert decisions[0]["kind"] == "new"
    assert row["today_eligible"] is False
    assert (row["shadow_accept"], row["shadow_kind"], row["shadow_item_id"]) == (True, "recurrence", "dormant")


def test_two_same_day_clusters_at_cosine_0_93_resolve_to_one_item_in_the_shadow():
    item = an_item("X", at(1.0), DAY - timedelta(days=2), keywords=["kota"], hashtags=["kotaday"])
    a = a_cluster("a", at(0.93), keywords=["kota"], hashtags=["kotaday"])
    b = a_cluster("b", at(0.93, towards=2), keywords=["kota"], hashtags=["kotaday"])
    decisions, rows = shadow_of([a, b], [item] + background())
    assert sorted(d["kind"] for d in decisions) == ["match", "new"]
    chosen = {r["cluster_id"]: (r["shadow_kind"], r["shadow_item_id"], r["shadow_group"])
              for r in rows if r["item_id"] == "X"}
    assert chosen == {"a": ("match", "X", ["a", "b"]), "b": ("match", "X", ["a", "b"])}


# The shadow decides nothing

def battery(seed=7, n=60):
    rng = random.Random(seed)
    tags = ["fyp", "viral", "kota", "amapiano", "eskom", "bgtag1"]
    cases = []
    for _ in range(n):
        cos = round(rng.uniform(0.2, 0.99), 3)
        pick = lambda: rng.sample(tags, rng.randint(0, 3))  # noqa: E731
        c = a_cluster("c1", at(1.0), keywords=rng.sample(["a", "b", "c", "d"], rng.randint(0, 3)), hashtags=pick(),
                      creators=rng.sample(["u1", "u2"], rng.randint(0, 2)))
        item = an_item("m1", at(cos), DAY - timedelta(days=rng.choice([0, 3, 7, 8, 30, 90])),
                       keywords=rng.sample(["a", "b", "c", "d"], rng.randint(0, 3)), hashtags=pick(),
                       creators=rng.sample(["u1", "u2"], rng.randint(0, 2)))
        cases.append(([c, a_cluster("c2", at(0.9, towards=2), hashtags=pick())], [item] + background(6)))
    return cases


def test_decisions_are_byte_identical_with_the_shadow_on_or_off():
    for clusters, items in battery():
        off = json.dumps(cluster.assign(clusters, items, DAY), sort_keys=True, default=str)
        sink = []
        on = json.dumps(cluster.assign(clusters, items, DAY, shadow=sink), sort_keys=True, default=str)
        assert off == on
        assert sink


def test_the_shadow_records_exactly_the_candidates_the_vote_rule_looked_at():
    for clusters, items in battery(seed=3, n=20):
        decisions = cluster.assign(clusters, items, DAY)
        shadow = []
        cluster.assign(clusters, items, DAY, shadow=shadow)
        for d in decisions:
            seen = [(r["item_id"], r["cosine"]) for r in shadow if r["cluster_id"] == d["cluster_id"]]
            assert seen == d["candidates"]
            assert {r["today_kind"] for r in shadow if r["cluster_id"] == d["cluster_id"]} == {d["kind"]}


def test_today_eligibility_in_the_record_is_the_vote_rule_unchanged():
    for clusters, items in battery(seed=11, n=30):
        shadow = []
        cluster.assign(clusters, items, DAY, shadow=shadow)
        for r in shadow:
            ok = len(r["today_votes"]) >= cluster.MIN_VOTES and bool(
                {"cosine", "keywords", "hashtag_or_sound"}.intersection(r["today_votes"]))
            assert r["today_eligible"] is ok


def test_a_failing_shadow_leaves_the_decisions_alone_and_says_so(monkeypatch):
    clusters, items = battery(n=1)[0]
    want = json.dumps(cluster.assign(clusters, items, DAY), sort_keys=True, default=str)

    def boom(*args, **kwargs):
        raise ValueError("secret detail")

    monkeypatch.setattr(pair_score, "shadow_records", boom)
    sink = []
    got = json.dumps(cluster.assign(clusters, items, DAY, shadow=sink), sort_keys=True, default=str)
    assert got == want
    assert sink == [{"shadow_error": "ValueError"}]


def test_cluster_py_keeps_the_vote_rule_constants_and_the_eligibility_expression():
    assert (cluster.MATCH_COSINE, cluster.KEYWORD_JACCARD, cluster.MIN_VOTES, cluster.RECENT_DAYS) == (
        0.82, 0.10, 2, 7)
    source = (cluster.Path(cluster.__file__)).read_text(encoding="utf-8")
    assert 'if len(got) >= MIN_VOTES and {"cosine", "keywords", "hashtag_or_sound"}.intersection(got)}' in source


# run_cluster

def test_the_sink_is_off_by_default():
    assert cluster.SHADOW_SINK is None


def test_a_run_writes_the_same_rows_with_the_sink_on_or_off_and_hands_it_the_records(monkeypatch):
    from core.understand.tests import test_cluster as tc

    items = [an_item("X", at(0.95), DAY - timedelta(days=1), keywords=["kota"], hashtags=["fyp"])] + background(6)
    clusters = tc.synthetic_clusters(3, dim=8, spread=0.05)
    for c in clusters:
        c["centroid"] = at(1.0)
        c["hashtags"] = ["fyp"]
    monkeypatch.setattr(cluster, "SHADOW_SINK", None)
    off_counts, off_writes = tc.fake_run(monkeypatch, clusters, items=items)
    got = []
    monkeypatch.setattr(cluster, "SHADOW_SINK", lambda market, run_date, records: got.append((market, run_date, records)))
    on_counts, on_writes = tc.fake_run(monkeypatch, clusters, items=items)
    assert json.dumps(off_counts, sort_keys=True, default=str) == json.dumps(on_counts, sort_keys=True, default=str)
    strip = lambda ws: [{k: v for k, v in w.items()} for w in ws]  # noqa: E731
    assert json.dumps(strip(off_writes), sort_keys=True, default=str) == json.dumps(strip(on_writes), sort_keys=True,
                                                                                    default=str)
    [(market, run_date, records)] = got
    assert (market, run_date) == ("za", DAY) and records and all("shadow_score" in r for r in records)
