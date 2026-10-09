"""W8-DEC-12: news public-feed posts count toward market scope only as feed evidence and never alone. An item whose
scoped posts are all news stays Market unconfirmed, keeps feed wording, and cannot satisfy the 2-local rule."""

from datetime import date
from pathlib import Path

import pytest

from core.brief import explain, holds_report, job, market_scope, payload, specificity
from core.brief.market_scope import read_market_scope
from core.brief.specificity import MIN_EVIDENCE, local_posts, showable_posts, specificity_basis
from core.brief.tests.test_brief_market_scope import add_post, sighting, world
from core.detect.tests import duck


D = date(2026, 9, 30)


def rec(evidence_id, platform="tiktok", *, market=None, source="ZA"):
    return {"id": evidence_id, "platform": platform, "handle": f"@{evidence_id}", "url": f"https://x/{evidence_id}",
            "posted_at": "2026-09-29T19:40:00+02:00", "market": market, "source_market": source,
            "text": "Dancing to the new sound at home", "quote_text": "Dancing to the new sound at home",
            "engagement": {"views": 10}, "flags": []}


def news(n, **kw):
    return [rec(f"news{i}", "news", **kw) for i in range(n)]


def located(n):
    return [rec(f"loc{i}", "tiktok", market="ZA", source=None) for i in range(n)]


def own_feed(n):
    return [rec(f"own{i}", "tiktok", market=None, source="ZA") for i in range(n)]


def candidate(evidence):
    return {"market": "ZA", "ctx": {}, "pack": {"evidence": evidence},
            "row": {"market_scope": "market", "map_status": "active", "nameless": False,
                    "authenticity": "clear", "sponsored_share": 0}}


@pytest.fixture
def open_gate(monkeypatch):
    monkeypatch.setattr(job, "gate_card", lambda *_: job.Decision(True, "today", None, None, None, False))


def gate(evidence):
    cand = candidate(evidence)
    return job._gate(cand, None), cand


# The 2-local rule at the gate


def test_an_item_whose_posts_are_all_news_cannot_reach_today(open_gate):
    decision, cand = gate(news(5))

    assert decision.where == "held_back"
    assert decision.reason == "Fewer than 2 supported local posts"
    assert cand["held_reason"] == "not_confirmed" and cand["floor_held"] is True


def test_news_plus_one_local_post_stays_under_the_two_local_rule(open_gate):
    decision, cand = gate(news(4) + located(1))

    assert decision.where == "held_back"
    assert decision.reason == "Fewer than 2 supported local posts"
    assert cand["floor_held"] is True


def test_news_plus_two_local_creators_passes(open_gate):
    decision, cand = gate(news(3) + located(2))

    assert decision.where == "today"
    assert cand["held_reason"] is None


def test_news_posts_still_count_as_posts_42_can_show(open_gate):
    # three showable posts is the other floor: one local plus two news reaches it and fails only the 2-local rule
    decision, _ = gate(news(2) + located(1))

    assert decision.reason == "Fewer than 2 supported local posts"
    assert len(showable_posts(news(2) + located(1), "ZA")) == 3


def test_a_news_post_located_in_the_market_is_still_feed_evidence_only(open_gate):
    decision, _ = gate(news(3, market="ZA") + located(1))

    assert decision.reason == "Fewer than 2 supported local posts"


def test_non_news_own_feed_posts_still_count_toward_the_two_local_rule(open_gate):
    decision, _ = gate(own_feed(2) + news(1))

    assert decision.where == "today"


def test_min_evidence_and_the_two_local_floor_are_unchanged(open_gate):
    assert MIN_EVIDENCE == 3
    decision, cand = gate(located(2))
    assert decision.reason == "Fewer than 3 posts 42 can show" and cand["floor_held"] is True
    assert gate(located(3))[0].where == "today"
    assert gate(located(1) + own_feed(1) + [rec("u", market=None, source=None)])[0].where == "today"
    assert gate(located(1) + [rec("u1", market=None, source=None), rec("u2", market=None, source=None)])[0].reason \
        == "Fewer than 2 supported local posts"


def test_counted_local_posts_is_local_posts_without_news_and_changes_nothing_else():
    evidence = news(2) + located(2) + own_feed(1) + [rec("foreign", market="NG", source="NG")]

    assert [r["id"] for r in specificity.counted_local_posts(evidence, "ZA")] == ["loc0", "loc1", "own0"]
    assert [r["id"] for r in local_posts(evidence, "ZA")] == ["news0", "news1", "loc0", "loc1", "own0"]
    assert specificity.counted_local_posts(evidence, "ZA") == [r for r in local_posts(evidence, "ZA")
                                                    if r["platform"] != "news"]


@pytest.mark.parametrize("platform", ["News", " news ", "NEWS"])
def test_the_platform_is_matched_without_regard_to_case_or_padding(platform):
    assert specificity.counted_local_posts([rec("p", platform)], "ZA") == []


def test_a_post_with_no_platform_is_not_taken_for_news():
    record = rec("p", None)
    del record["platform"]

    assert specificity.counted_local_posts([record], "ZA") == [record]


# The same rule where the explanation and the hold report count local posts


def claims_citing(ids):
    return ([{"id": "c1", "evidence_ids": ids, "quotes": []}], ["c1"])


def test_an_explanation_resting_on_one_local_post_and_news_lacks_local_evidence():
    evidence = located(1) + news(2)
    claims, ids = claims_citing(["loc0", "news0", "news1"])

    basis = specificity_basis(explanation="Creators started this before the weekend.", claims=claims,
                              explanation_claim_ids=ids, evidence=evidence, market="ZA")

    assert basis["reason"] == "insufficient_local_evidence"
    assert basis["local_evidence_ids"] == ["loc0"]


def test_an_explanation_citing_two_local_posts_and_news_has_local_evidence():
    evidence = located(2) + news(1)
    claims, ids = claims_citing(["news0", "loc0", "loc1"])

    basis = specificity_basis(explanation="Creators started this before the weekend.", claims=claims,
                              explanation_claim_ids=ids, evidence=evidence, market="ZA")

    assert basis["reason"] != "insufficient_local_evidence"
    assert basis["local_evidence_ids"] == ["loc0", "loc1"]


def test_a_quote_from_a_news_post_is_not_a_local_quote():
    evidence = located(2) + news(1)
    claims = [{"id": "c1", "evidence_ids": ["loc0", "loc1", "news0"],
               "quotes": [{"evidence_id": "news0", "text": "Dancing to the new sound"}]}]

    basis = specificity_basis(explanation="Creators started this before the weekend.", claims=claims,
                              explanation_claim_ids=["c1"], evidence=evidence, market="ZA")

    assert basis["reason"] == "quote_not_local"


def test_the_hold_report_counts_local_posts_as_the_gate_does():
    evidence = news(2) + located(1)

    assert holds_report.post_counts(evidence, "ZA") == (3, 1, 3)


# Feed wording is kept for news posts


def test_a_news_post_found_in_the_markets_feed_is_still_marked_own_feed_local():
    assert explain._own_feed_local(rec("n", "news", market=None, source="ZA"), "ZA") is True
    assert explain._own_feed_local(rec("n", "news", market=None, source="NG"), "ZA") is False


# The market scope read


def news_world(*, news_posts, local_posts_=0, own_feed_posts=0):
    con = world()
    for n in range(news_posts):
        add_post(con, f"news_{n}", creator_id=f"news_creator_{n}", engagement=500 - n,
                 source_sightings=[sighting("ZA", D)])
        con.execute(f"UPDATE core.posts SET platform = 'news' WHERE post_id = 'news_{n}'")
    for n in range(local_posts_):
        add_post(con, f"local_{n}", creator_id=f"local_creator_{n}", engagement=100 - n, geo_market="ZA",
                 geo_confidence=0.9, geo_source="ext_region")
        con.execute(f"UPDATE core.posts SET platform = 'tiktok' WHERE post_id = 'local_{n}'")
    for n in range(own_feed_posts):
        add_post(con, f"own_{n}", creator_id=f"own_creator_{n}", engagement=50 - n,
                 source_sightings=[sighting("ZA", D)])
        con.execute(f"UPDATE core.posts SET platform = 'tiktok' WHERE post_id = 'own_{n}'")
    return con


def read(con, row=None):
    return read_market_scope(duck.Client(con), row or {"item_id": "i1", "geo_status": "local"}, D, "ZA",
                             core="core", agent="agent")


def test_scoped_posts_that_are_all_news_stay_in_the_market_list_as_market_unconfirmed():
    result = read(news_world(news_posts=4))

    assert result == {"market_scope": "market", "market_posts7": 4, "total_posts7": 4, "market_share7": 1.0,
                      "geo_status": "market_unconfirmed"}


def test_a_news_item_that_detect_called_not_local_is_not_relabelled():
    result = read(news_world(news_posts=3), {"item_id": "i1", "geo_status": "not_local"})

    assert "geo_status" not in result


def test_news_plus_one_local_post_is_not_all_news():
    result = read(news_world(news_posts=3, local_posts_=1))

    assert result == {"market_scope": "market", "market_posts7": 4, "total_posts7": 4, "market_share7": 1.0}


def test_news_plus_two_local_creators_is_not_all_news():
    result = read(news_world(news_posts=3, local_posts_=2))

    assert "geo_status" not in result and result["market_scope"] == "market"


def test_non_news_own_feed_posts_alone_are_not_relabelled():
    result = read(news_world(news_posts=0, own_feed_posts=3))

    assert result == {"market_scope": "market", "market_posts7": 3, "total_posts7": 3, "market_share7": 1.0}


def test_no_scoped_posts_is_not_all_news():
    result = read(world())

    assert result == {"market_scope": "global", "market_posts7": 0, "total_posts7": 0, "market_share7": None}


def add_other_market(con, n, *, platform="tiktok"):
    for i in range(n):
        add_post(con, f"ng_{i}", creator_id=f"ng_creator_{i}", engagement=40 - i, geo_market="NG",
                 geo_confidence=0.9, geo_source="ext_region")
        con.execute(f"UPDATE core.posts SET platform = '{platform}' WHERE post_id = 'ng_{i}'")


def test_market_scope_that_rests_on_news_alone_is_market_unconfirmed_though_other_posts_fill_the_twelve():
    # 7 news posts sighted in the ZA feeds plus 5 TikTok posts located in NG: the 7 posts that make the market
    # share are all news, so the news count is compared with them and not with the twelve
    con = news_world(news_posts=7)
    add_other_market(con, 5)
    result = read(con)

    assert result == {"market_scope": "market", "market_posts7": 7, "total_posts7": 12, "market_share7": 7 / 12,
                      "geo_status": "market_unconfirmed"}


def test_one_non_news_market_post_among_the_market_posts_is_not_news_alone():
    con = news_world(news_posts=6, local_posts_=1)
    add_other_market(con, 5)
    result = read(con)

    assert result["market_posts7"] == 7 and result["total_posts7"] == 12 and "geo_status" not in result


def test_news_posts_outside_the_market_do_not_make_the_market_posts_news_alone():
    # 6 news posts sighted only in NG (not market posts), 3 located ZA TikTok posts: market posts are the 3 TikTok
    con = news_world(news_posts=0, local_posts_=3)
    for n in range(6):
        add_post(con, f"ngnews_{n}", creator_id=f"ngnews_creator_{n}", engagement=500 - n,
                 source_sightings=[sighting("NG", D)])
        con.execute(f"UPDATE core.posts SET platform = 'news' WHERE post_id = 'ngnews_{n}'")
    result = read(con)

    assert result["market_posts7"] == 3 and result["total_posts7"] == 9 and "geo_status" not in result


def test_news_posts_with_no_market_posts_do_not_relabel():
    con = news_world(news_posts=0)
    for n in range(4):
        add_post(con, f"ngnews_{n}", creator_id=f"ngnews_creator_{n}", engagement=500 - n,
                 source_sightings=[sighting("NG", D)])
        con.execute(f"UPDATE core.posts SET platform = 'news' WHERE post_id = 'ngnews_{n}'")
    result = read(con)

    assert result["market_posts7"] == 0 and result["total_posts7"] == 4 and "geo_status" not in result


def test_news_counts_only_the_posts_in_the_scoped_set():
    # twelve non-news posts outrank two news posts, so the scoped twelve hold no news and the item is not all news
    con = news_world(news_posts=2, local_posts_=12)
    result = read(con)

    assert result["total_posts7"] == 12 and "geo_status" not in result


@pytest.mark.parametrize(("row", "message"), [
    ({"total_posts7": 4, "market_posts7": 2}, "missing required count fields"),
    ({"total_posts7": 4, "market_posts7": 2, "market_news_posts7": 3}, "between zero and the market posts"),
    ({"total_posts7": 4, "market_posts7": 2, "market_news_posts7": -1}, "between zero and the market posts"),
    ({"total_posts7": 4, "market_posts7": 2, "market_news_posts7": 1.5}, "finite non-boolean integers"),
    ({"total_posts7": 4, "market_posts7": 2, "market_news_posts7": True}, "finite non-boolean integers"),
    ({"total_posts7": 4, "market_posts7": 2, "news_posts7": 1}, "missing required count fields"),
])
def test_a_scope_row_without_a_valid_news_count_fails_closed(monkeypatch, row, message):
    monkeypatch.setattr(market_scope.sqlrun, "query", lambda *args, **kwargs: [row])

    with pytest.raises(ValueError, match=message):
        read_market_scope(object(), {"item_id": "i1"}, D, "ZA")


@pytest.mark.parametrize(("counts", "relabelled"), [
    ({"total_posts7": 12, "market_posts7": 7, "market_news_posts7": 7}, True),
    ({"total_posts7": 12, "market_posts7": 7, "market_news_posts7": 6}, False),
    ({"total_posts7": 12, "market_posts7": 0, "market_news_posts7": 0}, False),
    ({"total_posts7": 0, "market_posts7": 0, "market_news_posts7": 0}, False),
    ({"total_posts7": 5, "market_posts7": 1, "market_news_posts7": 1}, True),
])
def test_the_relabel_fires_only_when_the_market_posts_are_all_news(monkeypatch, counts, relabelled):
    monkeypatch.setattr(market_scope.sqlrun, "query", lambda *args, **kwargs: [counts])

    result = read_market_scope(object(), {"item_id": "i1"}, D, "ZA")

    assert (result.get("geo_status") == "market_unconfirmed") is relabelled


def test_the_label_reaches_the_card_flag():
    card = {"decision": {"flag": None}, "geo_status": "market_unconfirmed", "authenticity": "clear"}

    assert payload._flag(card, {"flag": None}) == "market_unconfirmed"


def test_the_scope_query_matches_the_news_platform_without_regard_to_case_or_padding():
    con = news_world(news_posts=3)
    con.execute("UPDATE core.posts SET platform = ' News ' WHERE post_id = 'news_0'")
    con.execute("UPDATE core.posts SET platform = 'NEWS' WHERE post_id = 'news_1'")

    assert read(con)["geo_status"] == "market_unconfirmed"


# DEC-12 on the candidate rank: market scope earned on news posts alone does not take the first tier of brief.sql

TIER_START = "CASE WHEN ms.market_scope = 'market'"


def tier_case():
    """The tier CASE of brief.sql's candidates query, as written in the file."""
    sql = (Path(job.__file__).parent / "sql" / "brief.sql").read_text(encoding="utf-8")
    start = sql.index(TIER_START)
    end = sql.index("END,", start) + len("END")
    return sql[start:end]


def tier(market_scope, market, news, total):
    import duckdb
    con = duckdb.connect()
    con.execute("CREATE TABLE ms AS SELECT ? AS market_scope, ? AS market_posts7, ? AS market_news_posts7, "
                "? AS total_posts7", [market_scope, market, news, total])
    return con.execute(f"SELECT {tier_case()} FROM ms").fetchone()[0]


@pytest.mark.parametrize(("scope", "market", "news", "total", "expected"), [
    ("market", 7, 7, 12, 2),    # 7 news posts and 5 others: news alone, so not the first tier
    ("market", 7, 6, 12, 0),    # one market post that is not news keeps it
    ("market", 7, 0, 12, 0),    # a mixed case with no news at all
    ("market", 3, 1, 3, 0),
    ("market", 1, 0, 12, 1),    # market scope on one post: second tier, as before
    ("market", 1, 1, 12, 2),
    ("market", 2, 2, 2, 2),
    ("global", 7, 0, 12, 2),
    ("global", 0, 0, 0, 2),
])
def test_the_candidate_tier_leaves_a_news_only_market_item_out_of_the_first_tier(scope, market, news, total, expected):
    assert tier(scope, market, news, total) == expected


def test_the_tier_case_is_the_one_in_the_candidates_query():
    sql = (Path(job.__file__).parent / "sql" / "brief.sql").read_text(encoding="utf-8")
    assert sql.count(TIER_START) == 1
    assert "ms.market_news_posts7 < ms.market_posts7" in tier_case()
