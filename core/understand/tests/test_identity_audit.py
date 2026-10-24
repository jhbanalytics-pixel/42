"""Identity and label regressions from the October 2026 audit: a shared creator plus one shared sound or tag at a
low cosine, generic tags crowding a topic's own tag out of the top ten, generic or empty tags as labels, and keyword
labels that repeat a bigram's own words."""
import math
from datetime import date, timedelta

import pytest

from core.understand import cluster

DAY = date(2026, 10, 17)
GENERIC = ["fyp", "foryou", "viral", "trending", "xyzbca", "foryoupage", "tiktok", "viralvideo", "explore", "capcut"]


def _at(cos):
    return [cos, math.sqrt(max(0.0, 1 - cos * cos))]


def _cluster(**kw):
    return {"cluster_id": "c", "centroid": [1.0, 0.0], "keywords": [], "hashtags": [], "sounds": [], "creators": [],
            **kw}


def _item(cos, days, **kw):
    return {"item_id": "old", "centroid": _at(cos), "keywords": [], "hashtags": [], "sounds": [], "creators": [],
            "last_seen": DAY - timedelta(days=days), **kw}


# Finding 1: a shared creator plus one shared sound or tag is no topic identity when the centroids disagree.

@pytest.mark.parametrize("cos, days, facet", [
    (0.2, 1, {"sounds": ["snd_viral"]}),            # a trending sound reused by an aggregator account
    (0.05, 60, {"hashtags": ["ewnupdates"]}),       # a publisher's own branded tag, item dormant
    (0.0, 60, {"sounds": ["snd_viral"]}),
    (0.69, 3, {"hashtags": ["ewnupdates"]}),
])
def test_creator_and_one_facet_below_the_variant_cosine_is_not_a_match(cos, days, facet):
    c = _cluster(keywords=["skincare", "routine"], creators=["dailyclips"], **facet)
    i = _item(cos, days, keywords=["car", "wash"], creators=["dailyclips"], **facet)
    [d] = cluster.assign([c], [i], DAY)
    assert d["kind"] == "new", d
    assert {"creator", "hashtag_or_sound"} <= set(d["votes"])


def test_creator_and_one_facet_at_the_variant_cosine_still_continues():
    c = _cluster(sounds=["snd_kota"], creators=["cr"])
    i = _item(0.75, 2, sounds=["snd_kota"], creators=["cr"])
    [d] = cluster.assign([c], [i], DAY)
    assert (d["kind"], d["item_id"]) == ("match", "old")


def test_low_cosine_continuity_without_a_creator_is_unchanged():
    # keywords plus a shared tag at cosine 0.6 is still one identity: the floor applies only to creator pairs.
    c = _cluster(keywords=["japa", "abroad", "visa"], hashtags=["japa"])
    i = _item(0.6, 3, keywords=["japa", "abroad", "relocation"], hashtags=["japa"])
    [d] = cluster.assign([c], [i], DAY)
    assert (d["kind"], d["item_id"]) == ("match", "old")


def test_creator_with_keywords_on_a_dormant_item_still_recurs():
    c = _cluster(keywords=["matric", "dance"], creators=["cr"])
    i = _item(0.9, 60, keywords=["matric", "dance", "dress"], creators=["cr"])
    [d] = cluster.assign([c], [i], DAY)
    assert (d["kind"], d["votes"]) == ("recurrence", ["cosine", "keywords", "creator"])


# Finding 2: generic tags are dropped before the top-ten cut, so the topic's own tag survives to vote.

def _posts(n, tags_for):
    return [dict(post_id=f"p{k}", today=True, embedding=[1.0, 0.0], hashtags=tags_for(k), sound_id=None,
                 creator_id=f"cr{k}", market="ZA", platform="tiktok") for k in range(n)]


def test_specific_tag_survives_a_stack_of_generic_tags():
    posts = _posts(12, lambda k: [f"#{t}" for t in GENERIC] + (["#durbanjuly"] if k < 6 else []))
    [c] = cluster.build_clusters(posts, [0] * 12, [0.9] * 12, {0: ["horse", "racing"]}, DAY, "za")
    assert "durbanjuly" in c["hashtags"]
    assert not any(cluster.is_generic("hashtag", t) for t in c["hashtags"])
    item = _item(0.85, 2, hashtags=["durbanjuly"])
    [d] = cluster.assign([c], [item], DAY)
    assert (d["kind"], d["item_id"]) == ("match", "old")
    assert "hashtag_or_sound" in d["votes"]


# Finding 3: a label is never a platform-generic tag or a bare "#".

def test_generic_hashtag_is_not_a_label():
    posts = _posts(3, lambda k: ["#fyp", "#foryou", "#foryou"])
    [c] = cluster.build_clusters(posts, [0] * 3, [0.9] * 3, {0: []}, DAY, "za")
    assert c["label"] == f"Topic {cluster.short_id(c['cluster_id'])}"
    assert c["labels"] == []


def test_label_candidates_skip_generic_and_empty_tags():
    assert cluster.label_candidates([], ["#foryou", "###", "#jollofwars"], []) == ["#jollofwars"]
    assert cluster.label_candidates([], ["###", "#fyp"], ["s1"]) == ["sound s1"]
    assert cluster.label_for([], ["###"], [], short_id="abcd1234") == "Topic abcd1234"


# Finding 4: a keyword label does not repeat a bigram's own words.

@pytest.mark.parametrize("keywords, want", [
    (["load shedding", "load", "shedding", "eskom", "stage"], "load shedding, eskom, stage"),
    (["rugby", "springboks", "kolisi rugby", "bok"], "kolisi rugby, springboks, bok"),
    (["load", "load shedding", "eskom", "stage"], "load shedding, eskom, stage"),
    (["amapiano", "dance", "challenge"], "amapiano, dance, challenge"),
    # A bigram that holds one kept keyword but shares a word with another would only move the repeat.
    (["school trip", "bus", "school bus", "teacher"], "school trip, bus, teacher"),
])
def test_keyword_label_drops_repeated_words(keywords, want):
    assert cluster.label_for(keywords, [], []) == want


def test_deduped_keyword_label_is_still_age_scanned():
    # "gen" and "alpha" each pass alone; the pair formed once "gen alpha"'s repeats are dropped must still be scanned.
    assert cluster.label_candidates(["gen alpha", "gen", "alpha", "slang"], [], []) == []
