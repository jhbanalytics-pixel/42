"""Identity and label regressions from the October 2026 audit."""
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
