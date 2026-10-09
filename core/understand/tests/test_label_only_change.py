"""Q14, typed 9 Oct 2026 (C4 v3 section 16, rule (b) of the label-only change): a topic keeps its id when its name
changes, and the name changes only when the new cluster is the same topic by cosine (0.82 or more), its keywords
have left the stored ones (Jaccard under 0.10) and it shares at least MIN_SHARED_MEMBERS = 2 member posts with the
item's members of the previous 3 days. Otherwise the earlier label sticks, and the pair is counted as a drift
candidate for the weekly review. The previous label goes to aliases unless it is a placeholder.

The first three tests are the identity contract's own L2, L3 and L4 (appendix I). The rest pin the boundaries."""

import math
from datetime import date, datetime, timedelta

import pytest

from core.trust.locality import MIN_SHARED_MEMBERS
from core.understand import cluster

DAY = date(2026, 10, 7)
YESTERDAY = DAY - timedelta(days=1)


def at(cos, dim=6, towards=1):
    v = [0.0] * dim
    v[0], v[towards] = cos, math.sqrt(1 - cos * cos)
    return v


def a_cluster(cid, vec, keywords=(), hashtags=(), label="new label", members=("p1", "p2")):
    return {"cluster_id": cid, "centroid": list(vec), "keywords": list(keywords), "hashtags": list(hashtags),
            "sounds": [], "creators": [], "label": label, "market": "ng", "platform": "tiktok", "local_terms": [],
            "members": [(p, 0.9) for p in members]}


def an_item(iid, vec, keywords=(), hashtags=(), label=None, recent_members=None, aliases=()):
    item = {"item_id": iid, "kind": "topic", "canonical_key": f"topic:{iid}", "label": label or iid,
            "aliases": list(aliases), "parent_item_id": None, "centroid": list(vec), "first_seen": date(2026, 10, 5),
            "first_seen_market": "ng", "first_seen_platform": "tiktok", "last_seen": YESTERDAY, "recurrences": 0,
            "lifecycle": None, "status": "active", "rejected_until": None, "keywords": list(keywords),
            "hashtags": list(hashtags), "sounds": [], "creators": [], "valid_from": datetime(2026, 10, 5)}
    if recent_members is not None:
        item["recent_members"] = list(recent_members)
    return item


OLD_WORDS = ["messi", "soccer", "argentina"]
NEW_WORDS = ["ronaldo", "portugal", "football"]
OLD_LABEL, NEW_LABEL = "messi, soccer, argentina", "ronaldo, portugal, football"


def planned(old, c):
    decisions = cluster.assign([c], [old], DAY)
    return cluster.plan([c], decisions, [old], DAY, "ng"), decisions


def drifted(recent, members, *, cos=0.9, label=NEW_LABEL, old_label=OLD_LABEL, new_words=NEW_WORDS, aliases=(),
            tags=("football", "football")):
    old = an_item("topic_a", at(cos), OLD_WORDS, [tags[0]], label=old_label, recent_members=recent, aliases=aliases)
    c = a_cluster("c9", at(1.0), new_words, [tags[1]], label=label, members=members)
    return planned(old, c)


def test_L2_a_label_whose_words_have_left_the_topic_changes_with_cosine_and_two_shared_members_and_keeps_an_alias():
    got, _ = drifted(["p1", "p2"], ["p1", "p2", "p3"])
    row = got["map_rows"][0]
    assert row["item_id"] == "topic_a" and row["label"] == NEW_LABEL and row["aliases"] == [OLD_LABEL]
    assert got["cluster_rows"][0]["item_id"] == "topic_a"
    assert got["label_changes"] == [{"item_id": "topic_a", "from": OLD_LABEL, "to": NEW_LABEL, "shared_members": 2}]
    assert got["label_drift_candidates"] == []


def test_L4_the_same_divergence_with_no_shared_member_is_not_a_label_change():
    got, _ = drifted(["z1", "z2"], ["p1", "p2"])
    row = got["map_rows"][0]
    assert row["label"] == OLD_LABEL and row["aliases"] == []
    assert got["label_changes"] == []
    assert got["label_drift_candidates"] == [{"item_id": "topic_a", "cluster_id": "c9", "shared_members": 0}]


def test_L3_a_label_whose_words_still_overlap_is_not_churned():
    got, _ = drifted(["p1", "p2"], ["p1", "p2"], new_words=["messi", "football", "argentina"], label="messi, football, argentina")
    assert got["map_rows"][0]["label"] == OLD_LABEL and got["map_rows"][0]["aliases"] == []
    assert got["label_changes"] == [] and got["label_drift_candidates"] == []


def test_MIN_SHARED_MEMBERS_is_the_typed_two():
    assert MIN_SHARED_MEMBERS == 2


@pytest.mark.parametrize(("shared", "changes"), [(0, False), (1, False), (2, True), (3, True)])
def test_the_label_changes_from_exactly_the_threshold_of_shared_members(shared, changes):
    recent = [f"p{i}" for i in range(1, shared + 1)] + ["z1", "z2", "z3"]
    got, _ = drifted(recent, ["p1", "p2", "p3", "p4"])
    assert (got["map_rows"][0]["label"] == NEW_LABEL) is changes
    assert [c["shared_members"] for c in got["label_changes"]] == ([shared] if changes else [])
    assert [c["shared_members"] for c in got["label_drift_candidates"]] == ([] if changes else [shared])


def test_a_post_listed_twice_among_the_members_counts_once():
    got, _ = drifted(["p1", "p1", "p2"], ["p1", "p1"])
    assert got["map_rows"][0]["label"] == OLD_LABEL


def test_an_item_with_no_recent_members_field_keeps_its_label():
    old = an_item("topic_a", at(0.9), OLD_WORDS, ["football"], label=OLD_LABEL)          # recent_members absent
    c = a_cluster("c9", at(1.0), NEW_WORDS, ["football"], label=NEW_LABEL)
    got, _ = planned(old, c)
    assert got["map_rows"][0]["label"] == OLD_LABEL and got["label_changes"] == []


def test_a_neutral_new_label_never_replaces_a_name():
    got, _ = drifted(["p1", "p2"], ["p1", "p2"], label="Topic 4f2d3671")
    assert got["map_rows"][0]["label"] == OLD_LABEL and got["label_changes"] == []


def test_the_same_label_is_not_a_change():
    got, _ = drifted(["p1", "p2"], ["p1", "p2"], label=OLD_LABEL)
    assert got["map_rows"][0]["aliases"] == [] and got["label_changes"] == []


def test_a_placeholder_old_label_is_not_kept_as_an_alias():
    got, _ = drifted(["p1", "p2"], ["p1", "p2"], old_label="Topic 4f2d3671")
    row = got["map_rows"][0]
    assert row["label"] == NEW_LABEL and row["aliases"] == []


def test_an_existing_alias_is_kept_and_the_old_label_is_added_once():
    got, _ = drifted(["p1", "p2"], ["p1", "p2"], aliases=["earlier name", OLD_LABEL])
    assert got["map_rows"][0]["aliases"] == ["earlier name", OLD_LABEL]


def test_a_variant_below_the_match_cosine_never_renames_anything():
    got, decisions = drifted(["p1", "p2"], ["p1", "p2"], cos=0.75, tags=("old", "new"))
    assert decisions[0]["kind"] == "variant"
    assert got["label_changes"] == [] and got["map_rows"][0]["kind"] == "topic" and got["map_rows"][0]["change"] == "insert"


def test_the_cosine_threshold_is_the_match_cosine():
    at_threshold, _ = drifted(["p1", "p2"], ["p1", "p2"], cos=cluster.MATCH_COSINE)
    assert at_threshold["map_rows"][0]["label"] == NEW_LABEL


def test_a_recurrence_that_drifted_is_also_governed_by_the_rule():
    old = an_item("topic_a", at(0.9), OLD_WORDS, ["football"], label=OLD_LABEL, recent_members=["z1"])
    old["last_seen"] = DAY - timedelta(days=40)
    c = a_cluster("c9", at(1.0), NEW_WORDS, ["football"], label=NEW_LABEL)
    got, decisions = planned(old, c)
    assert decisions[0]["kind"] == "recurrence" and got["map_rows"][0]["label"] == OLD_LABEL


# The item profile the rule reads: cluster_items.sql carries the item's member posts of the previous 3 days.

from core.understand.tests.test_cluster import CORE, DAY as WORLD_DAY, clean_net, duck_execute, world  # noqa: E402,F401


def test_cluster_items_carries_the_members_of_the_previous_three_days(world):  # noqa: F811
    execute = duck_execute(world["con"])
    on = lambda run_date: {r["item_id"]: r for r in execute(cluster.load("cluster_items"), {"run_date": run_date})}
    assert list(on(WORLD_DAY)["item_a"]["recent_members"]) == ["hist_a"]                # hist-a is 3 days back
    assert list(on(WORLD_DAY - timedelta(days=1))["item_a"]["recent_members"]) == ["hist_a"]
    assert "amapiano_00" in on(WORLD_DAY + timedelta(days=1))["item_a"]["recent_members"]   # the run's own day is now prior
    assert list(on(WORLD_DAY + timedelta(days=4))["item_a"]["recent_members"] or []) == []  # all more than 3 days back
    assert list(on(WORLD_DAY)["item_b"]["recent_members"] or []) == []



def _run(monkeypatch, recent):
    from core.understand.tests.test_cluster import DAY as RUN_DAY, NetModel, fake_run

    old = an_item("topic_a", at(0.9), OLD_WORDS, ["football"], label=OLD_LABEL, recent_members=recent)
    old["last_seen"] = RUN_DAY - timedelta(days=1)
    old["first_seen"] = RUN_DAY - timedelta(days=5)
    c = a_cluster("c9", at(1.0), NEW_WORDS, ["football"], label=NEW_LABEL)
    c["cluster_id"], c["market"] = f"{RUN_DAY:%Y%m%d}-za-000", "za"
    return fake_run(monkeypatch, [c], items=[old], model=NetModel())


def test_run_cluster_writes_the_new_label_and_counts_the_rename(monkeypatch):
    counts, writes = _run(monkeypatch, ["p1", "p2"])
    import json
    [row] = json.loads(writes[0]["map_rows"])
    assert (row["item_id"], row["label"], row["aliases"]) == ("topic_a", NEW_LABEL, [OLD_LABEL])
    assert counts["label_changes"] == 1 and "label_drift_candidates" not in counts


def test_run_cluster_keeps_the_label_and_counts_the_drift_candidate_when_nothing_is_shared(monkeypatch):
    counts, writes = _run(monkeypatch, ["z1"])
    import json
    [row] = json.loads(writes[0]["map_rows"])
    assert (row["label"], row["aliases"]) == (OLD_LABEL, [])
    assert counts["label_drift_candidates"] == 1 and "label_changes" not in counts
