"""rival_window computes what tvf_item_window computes, edge by edge, from a snapshot of the pack-time post links.

The fixture has one post for each rule the copy could drift on: lanes that do not count, a post first seen exactly
seven days back, a near duplicate size of 2, two posts 12 minutes apart, a measured small post that only a panel lane
saw, and an earlier small post that no measured lane saw. Each expected value is counted by hand in the comment.
"""

import hashlib
from datetime import datetime

import pytest

from core.brief import evidence
from core.brief.tests.test_brief_evidence import add_post, build, utc, world
from core.brief.tests.test_brief_rivals_evidence import window_row
from core.detect.tests import duck
from core.detect.tests.fixtures import D, day

RANK = (("sweep", "unbiased_rank"),)
# id, creator, published, tier, sightings, first seen, near_dup_size
POSTS = [
    ("m1", "c1", utc(day(2), 8, 0), "micro", RANK, D, 1),
    ("m2", "c2", utc(day(2), 8, 12), "micro", RANK, D, 1),          # 12 minutes after m1: two 10 minute buckets
    ("m3", "c3", utc(day(3), 13), "macro", RANK, D, 3),             # the one near duplicate, and large_at
    ("m4", "c4", utc(day(4), 15), "micro", RANK, D, 2),             # size 2 is not a near duplicate
    ("m5", "c5", utc(day(5), 9), "micro", (("panel", "panel"),), D, 1),            # small_at: a panel is measured
    ("m6", "c6", utc(day(6), 9), "nano", (("exploration", "search_presence"),), D, 1),   # not measured: not small_at
    ("b1", "c7", utc(day(4), 20), "micro", RANK, day(7), 1),         # first seen exactly 7 days back: not in the 7 days
    ("m7", "c11", utc(day(2), 12), "micro", RANK, D, 1),             # also seen in a placebo lane 8 days back
    ("q1", "c8", utc(day(1), 10), "micro", (("placebo", "search_presence"),), D, 1),
    ("q2", "c9", utc(day(1), 11), "micro", (("agent_live", "search_presence"),), D, 1),
    ("q3", "c10", utc(day(1), 12), "micro", (("legacy", "legacy"),), D, 1),
]
# posts7 counts m1..m7 = 7: m7's placebo sighting 8 days back is outside the lanes that count, so its first day is D.
# near_dup_share = 1 (m3) over 7 known sizes. top3_share = 3/7 (seven creators with one post each).
# burst_share = 1/7 (largest 10 minute bucket holds one post). small_at = m5, large_at = m3.


def edge_world():
    con = world(diffusion="bottom_up")
    for pid, creator, published, tier, sightings, seen, size in POSTS:
        add_post(con, pid, creator, published, 10, sightings=sightings, seen=seen, creator_tier_at_post=tier)
        duck.load(con, "core.post_enrichment", [{"post_id": pid, "near_dup_size": size, "sponsored": False}])
    duck.load(con, "core.post_observations", [{
        "post_id": "m7", "observed_at": utc(day(8), 10), "observed_date": day(8), "market": "ZA", "platform": "tiktok",
        "lane": "placebo", "lane_class": "unbiased_rank", "run_id": "collect-old"}])
    return con


def window(pack):
    return {e["rival_field"]: e["value"] for e in pack["numbers"] + pack.get("pinned", [])
            if e.get("rival_field") in ("posts7", "burst_share", "top3_share", "near_dup_share", "small_at", "large_at")}


def test_each_window_value_is_what_a_hand_count_of_the_edge_fixture_gives():
    pack, *_ = build(edge_world())
    got = window(pack)
    assert got["posts7"] == 7
    assert got["near_dup_share"] == pytest.approx(1 / 7)
    assert got["top3_share"] == pytest.approx(3 / 7)
    assert got["burst_share"] == pytest.approx(1 / 7)
    assert datetime.fromisoformat(got["small_at"]) == utc(day(5), 9)
    assert datetime.fromisoformat(got["large_at"]) == utc(day(3), 13)


def test_the_edge_fixture_values_equal_the_detect_table_function_row():
    con = edge_world()
    pack, *_ = build(con)
    live, got = window_row(con), window(pack)
    for field in ("posts7", "near_dup_share", "top3_share", "burst_share"):
        assert got[field] == pytest.approx(live[field]), field
    assert datetime.fromisoformat(got["small_at"]) == live["small_at"]
    assert datetime.fromisoformat(got["large_at"]) == live["large_at"]


def test_the_snapshot_is_the_sorted_ids_of_the_posts_linked_at_pack_time():
    pack, *_ = build(edge_world())
    counted = sorted(p[0] for p in POSTS if p[0] not in ("q1", "q2", "q3"))     # b1 is linked and in the 28 days
    digest = "sha256:" + hashlib.sha256("\n".join(counted).encode("utf-8")).hexdigest()
    entries = [e for e in pack["numbers"] + pack["pinned"] if e.get("rival_field") in window(pack)]
    assert entries and {e["post_snapshot"] for e in entries} == {digest}
    assert evidence.QUERIES["rival_posts"]
