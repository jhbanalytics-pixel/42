from core.brief import evidence
from core.detect import aggregate

from . import duck
from .fixtures import D, at, day, obs, post


def test_item_window_keeps_global_coord_hold_and_platform_scoped_profile_fields():
    con = duck.connect()
    duck.load(con, "core.posts", [post("p1", "shared", D, platform="tiktok")])
    duck.load(con, "core.post_observations", [obs("p1", D, "unbiased_rank", "sweep")])
    duck.load(con, "core.post_items", [{"post_id": "p1", "item_id": "it1", "via": "hashtag"}])
    duck.load(con, "core.creators", [
        {"creator_id": "shared", "platform": "tiktok", "coord_score": 0,
         "account_created_at": at(day(400))},
        {"creator_id": "shared", "platform": "instagram", "coord_score": 2,
         "account_created_at": at(D)},
    ])

    rows = duck.query(con, "SELECT w.posts3, w.creators3, w.young_share, w.seen7_all "
                          "FROM {core}.tvf_item_window(@d) w "
                          "WHERE w.item_id = @item_id", {"d": D, "item_id": "it1"})

    assert rows == [{"posts3": 0, "creators3": 0, "young_share": 0.0, "seen7_all": 1}]


def test_aggregate_matches_creator_profiles_by_platform():
    con = duck.connect()
    duck.load(con, "core.posts", [
        post("p_tiktok", "shared", D, platform="tiktok", tier="macro", engagement=11),
        post("p_instagram", "shared", D, platform="instagram", tier="nano", engagement=13),
        post("p_tiktok_missing", "missing", D, platform="tiktok", tier="micro", engagement=5),
    ])
    duck.load(con, "core.post_observations", [
        obs("p_tiktok", D, "unbiased_rank", "sweep", platform="tiktok"),
        obs("p_instagram", D, "unbiased_rank", "sweep", platform="instagram"),
        obs("p_tiktok_missing", D, "unbiased_rank", "sweep", platform="tiktok"),
    ])
    duck.load(con, "core.post_items", [
        {"post_id": "p_tiktok", "item_id": "it1", "via": "hashtag"},
        {"post_id": "p_instagram", "item_id": "it1", "via": "hashtag"},
        {"post_id": "p_tiktok_missing", "item_id": "it1", "via": "hashtag"},
    ])
    duck.load(con, "core.creators", [
        {"creator_id": "shared", "platform": "tiktok", "coord_score": 2},
        {"creator_id": "shared", "platform": "instagram", "coord_score": 2},
        {"creator_id": "missing", "platform": "instagram", "coord_score": 1},
    ])

    duck.query(con, aggregate.aggregate_sql(), {"d": D, "run_id": "agg1", "rule_version": "r1"})
    rows = duck.query(con, "SELECT i.platform, i.posts, i.creators, i.unflagged_creators, i.engagement, "
                          "i.tier_posts.nano nano, i.tier_posts.micro micro, i.tier_posts.macro macro "
                          "FROM {core}.item_daily i "
                          "WHERE i.item_id = @item_id AND i.lane_class = 'unbiased_rank' "
                          "ORDER BY i.platform", {"item_id": "it1"})

    assert rows == [
        {"platform": "instagram", "posts": 1, "creators": 1, "unflagged_creators": 0,
         "engagement": 13, "nano": 1, "micro": 0, "macro": 0},
        {"platform": "tiktok", "posts": 2, "creators": 2, "unflagged_creators": 0,
         "engagement": 16, "nano": 0, "micro": 1, "macro": 1},
    ]


def test_evidence_uses_platform_profile_and_preserves_global_coord_flags():
    con = duck.connect()
    duck.load(con, "core.posts", [
        post("p1", "shared", D, platform="tiktok", tier="macro", engagement=9, text="caption"),
        post("p2", "missing", D, platform="tiktok", tier="nano", engagement=8, text="caption"),
    ])
    duck.load(con, "core.post_observations", [
        obs("p1", D, "unbiased_rank", "sweep"), obs("p2", D, "unbiased_rank", "sweep")])
    duck.load(con, "core.post_items", [
        {"post_id": "p1", "item_id": "it1", "via": "hashtag"},
        {"post_id": "p2", "item_id": "it1", "via": "hashtag"},
    ])
    duck.load(con, "core.creators", [
        {"creator_id": "shared", "platform": "tiktok", "handle": "@tiktok", "coord_score": 0},
        {"creator_id": "shared", "platform": "instagram", "handle": "@instagram", "coord_score": 2},
        {"creator_id": "missing", "platform": "instagram", "handle": "@missing_ig", "coord_score": 1},
    ])

    pack, _, _ = evidence.build_pack(
        duck.Client(con), {"item_id": "it1", "run_id": "det1", "untested": True}, D, "ZA",
        core="core", agent="agent")

    assert len(pack["evidence"]) == 2
    records = {row["id"]: row for row in pack["evidence"]}
    assert records["p1"]["handle"] == "@tiktok"
    assert records["p1"]["flags"] == ["flagged", "market_assumed"]
    assert records["p1"]["creator_tier"] == "macro"
    assert records["p2"]["handle"] is None
    assert records["p2"]["flags"] == ["flagged", "market_assumed"]
    assert records["p2"]["creator_tier"] == "nano"
