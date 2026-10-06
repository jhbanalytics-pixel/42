from datetime import date, datetime

import pytest

from core.agent import checks
from core.agent.context import RunContext
from core.agent.tests.test_history import DuckWarehouse
from core.agent.tools.warehouse import discover_creators, is_creator_question


SPR_03 = (
    "Is amapiano still spreading in Kenya? Which Kenyan creators and DJs are posting it in the last couple of "
    "months, and is it being localised (Sheng or Swahili lyrics, local dance moves, Kenyan collaborations) or just "
    "imported as is?"
)
CRE_01 = (
    "Who are the mid-sized Nigerian creators (not the big skit makers) driving food and home-cooking content right "
    "now? What formats are they using, and how do their comment sections behave?"
)


@pytest.fixture
def ctx():
    return RunContext(run_id="creator_test", tier="T0", as_of=datetime(2026, 9, 30),
                      window_start=date(2026, 8, 1), window_end=date(2026, 9, 30))


@pytest.fixture
def creator_con():
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE SCHEMA intelligence_42_core")
    con.execute("CREATE TABLE intelligence_42_core.creators (creator_id VARCHAR, platform VARCHAR, handle VARCHAR, "
                "followers BIGINT, tier VARCHAR, account_created_at TIMESTAMP, home_market VARCHAR, "
                "verified_region VARCHAR, coord_score BIGINT, display_name VARCHAR, verified BOOLEAN, "
                "profile_location VARCHAR, first_seen TIMESTAMP, last_seen TIMESTAMP)")
    con.execute("CREATE TABLE intelligence_42_core.posts (post_id VARCHAR, platform VARCHAR, url VARCHAR, "
                "creator_id VARCHAR, published_at TIMESTAMP, post_date DATE, geo_market VARCHAR, geo_source VARCHAR, "
                "text VARCHAR, views BIGINT, likes BIGINT, comments BIGINT, shares BIGINT, engagement BIGINT)")
    con.execute("CREATE TABLE intelligence_42_core.v_post_source_markets (post_id VARCHAR, "
                "source_sightings STRUCT(source_market VARCHAR, source_region VARCHAR, route VARCHAR, "
                "protocol VARCHAR, observed_at TIMESTAMP, obs_date DATE)[])")
    con.execute("CREATE TABLE intelligence_42_core.suppression_fixture (creator_id VARCHAR)")
    con.execute("CREATE VIEW intelligence_42_core.v_suppressed_creators AS "
                "SELECT creator_id FROM intelligence_42_core.suppression_fixture")
    creators = [
        ("shared", "tiktok", "kenyan_dj", "macro", "KE"),
        ("shared", "x", "nigerian_creator", "macro", "NG"),
        ("ke_gap", "instagram", "kenyan_gap", "macro", "KE"),
        ("ke_old", "tiktok", "kenyan_old", "macro", "KE"),
        ("ke_mid", "instagram", "hidden_mid", "mid", "KE"),
        ("ke_suppressed", "tiktok", "hidden_creator", "macro", "KE"),
        ("ng_food", "tiktok", "food_creator", "macro", "NG"),
        ("ng_gap", "instagram", "food_gap", "mega", "NG"),
    ]
    con.executemany(
        "INSERT INTO intelligence_42_core.creators "
        "(creator_id, platform, handle, tier, home_market, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
        [(*row, datetime(2026, 9, 20)) for row in creators],
    )
    con.execute("INSERT INTO intelligence_42_core.suppression_fixture VALUES ('ke_suppressed')")
    posts = [
        ("spr_located", "tiktok", "shared", date(2026, 9, 12), "Amapiano with Sheng lyrics", "KE", "ext_region", 4),
        ("spr_profile", "tiktok", "shared", date(2026, 9, 13), "Amapiano dance clip", "KE", "home_market", 5),
        ("spr_cross_platform", "x", "shared", date(2026, 9, 14), "Amapiano in Nigeria", "NG", "ext_region", 6),
        ("spr_old", "tiktok", "ke_old", date(2026, 7, 30), "Amapiano from before the window", None, None, 3),
        ("cre_food", "tiktok", "ng_food", date(2026, 9, 15), "Home cooking stew recipe", None, None, 22),
    ]
    con.executemany(
        "INSERT INTO intelligence_42_core.posts "
        "(post_id, platform, url, creator_id, published_at, post_date, geo_market, geo_source, text, "
        "comments, engagement) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
        [(pid, platform, f"https://example.test/{pid}", creator, posted, geo, source, text, comments, comments)
         for pid, platform, creator, posted, text, geo, source, comments in posts],
    )
    sighting = [{"source_market": "KE", "source_region": "KE", "route": "tiktok/trending",
                 "protocol": "tiktok/trending?region=KE", "observed_at": datetime(2026, 9, 12),
                 "obs_date": date(2026, 9, 12)}]
    con.execute("INSERT INTO intelligence_42_core.v_post_source_markets VALUES (?, ?)", ["spr_located", sighting])
    return con


def test_creator_dispatch_flags_the_supplied_questions_only():
    assert is_creator_question(SPR_03)
    assert is_creator_question(CRE_01)
    assert not is_creator_question("Is amapiano spreading in Kenya?")


def test_creator_lookup_replays_platform_market_window_and_provenance(ctx, creator_con):
    out = discover_creators(ctx, DuckWarehouse(creator_con), SPR_03)

    assert [row["handle"] for row in out["creator_candidates"]] == ["kenyan_dj", "kenyan_gap", "kenyan_old"]
    assert {row["id"] for row in out["evidence"]} == {"spr_located", "spr_profile"}
    assert [row["handle"] for row in out["discovery_gaps"]] == ["kenyan_gap", "kenyan_old"]
    assert ctx.evidence["spr_located"]["market"] == "KE"
    assert ctx.evidence["spr_located"]["source_market"] == "KE"
    assert ctx.evidence["spr_profile"]["market"] == "KE"
    assert ctx.evidence["spr_profile"]["source_market"] == "KE"
    assert ctx.evidence["spr_profile"]["flags"] == ["market_assumed"]
    assert not checks._located(ctx.evidence["spr_profile"])
    assert set(ctx.evidence) == {"spr_located", "spr_profile"}

    creator_query_id, posts_query_id = out["query_ids"]
    creator_query = ctx.queries[creator_query_id]
    posts_query = ctx.queries[posts_query_id]
    assert "intelligence_42_core.v_suppressed_creators" in creator_query["sql"]
    assert "@market" in creator_query["sql"] and creator_query["params"]["market"] == "KE"
    assert "amapiano" not in posts_query["sql"].lower()
    assert "amapiano" in posts_query["params"].values()
    assert "p.platform = @creator_platform_0 AND p.creator_id = @creator_id_0" in posts_query["sql"]
    assert creator_query["purpose"].startswith("discover_creators")
    assert posts_query["purpose"].startswith("discover_creator_posts")
    assert out["geography_scope"] == "discovery_only"


def test_creator_lookup_keeps_size_and_comment_behavior_unknown(ctx, creator_con):
    ctx.market = "ZA"
    out = discover_creators(ctx, DuckWarehouse(creator_con), CRE_01)

    assert out["discovery_market"] == "NG"
    assert [row["handle"] for row in out["creator_candidates"]] == [
        "food_creator", "food_gap", "nigerian_creator"
    ]
    assert {row["id"] for row in out["evidence"]} == {"cre_food"}
    assert [row["handle"] for row in out["discovery_gaps"]] == ["food_gap", "nigerian_creator"]
    assert out["creator_size_status"] == "unknown"
    assert out["comment_text_status"] == "unavailable"
    assert (
        "macro and mega page tiers" in out["creator_size_note"]
    )
    assert ctx.evidence["cre_food"]["market"] == "NG"
    assert ctx.evidence["cre_food"]["source_market"] == "NG"
    assert ctx.evidence["cre_food"]["flags"] == ["market_assumed"]
    assert not checks._located(ctx.evidence["cre_food"])
    assert ctx.evidence["cre_food"]["engagement"]["comments"] == 22
    assert ctx.evidence["cre_food"]["text"] == "Home cooking stew recipe"

    ctx.evidence["cre_food"]["posted_at"] = "2026-09-15T10:00:00+00:00"
    window = (date(2026, 8, 1), date(2026, 9, 30))
    people_claim, _ = checks._k3("Nigerian creators drove home cooking.", ["cre_food"], ctx.evidence, window, ["NG"])
    feed_claim, _ = checks._k3("Posts seen in Nigeria's feeds discuss home cooking.", ["cre_food"], ctx.evidence,
                               window, ["NG"])
    assert any("only seen in Nigeria's feeds" in problem for problem in people_claim)
    assert feed_claim == []
