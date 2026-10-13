from datetime import date, timedelta

import pytest

from core.detect.items import canonical_key
from core.understand import cluster


DAY = date(2026, 10, 13)


@pytest.mark.parametrize("tag", ["Ａｍａｐｉａｎｏ", "＃Ａｍａｐｉａｎｏ", " #Amapiano ", "#AMAPIANO"])
def test_equivalent_hashtags_recur_under_the_existing_vote_rule(tag):
    current = {"cluster_id": "current", "centroid": [1.0, 0.0], "keywords": [],
               "hashtags": [tag], "sounds": [], "creators": []}
    existing = {"item_id": "existing", "centroid": [1.0, 0.0], "keywords": [],
                "hashtags": ["amapiano"], "sounds": [], "creators": [],
                "last_seen": DAY - timedelta(days=40)}
    assert canonical_key("hashtag", tag) == "amapiano"
    result = cluster.assign([current], [existing], DAY)[0]
    assert result["kind"] == "recurrence"
    assert result["item_id"] == "existing"
    assert result["votes"] == ["cosine", "hashtag_or_sound"]


def test_casefold_equivalent_hashtags_share_the_matching_vote():
    assert cluster._tag("＃ＳＴＲＡＳＳＥ") == cluster._tag("#Straße") == "strasse"


def test_normalized_hashtag_labels_still_pass_the_age_scan():
    assert cluster.label_candidates([], ["＃ＧｅｎＺ"], []) == []
    assert cluster.label_candidates([], ["＃Ａｍａｐｉａｎｏ"], []) == ["#amapiano"]


def test_empty_hashtag_remains_empty_without_breaking_the_run():
    assert cluster._tag("###") == ""
