"""Ask depth, item 3: a topic sweep that code runs beside the whole-store counts. Up to four recorded searches, balanced
by platform and creator, stored straight into the run's evidence and never returned to the research model."""

from datetime import date, datetime

import pytest

from core.agent import ask, checks
from core.agent.context import RunContext, result_hash
from core.agent.spread import spread_order
from core.agent.tests.test_ask import Harness, make_research
from core.agent.tests.test_ask_store_background import StoreWarehouse
from core.agent.tools import warehouse as wh_module
from core.agent.tools.sql_query import check_sql
from core.agent.tools.warehouse import SWEEP_TOOL, store_breadth, sweep_topic, topic_sweep

NOW = datetime(2026, 10, 10, 6, 0)
DAY = date(2026, 10, 8)


def make_row(post_id, platform="tiktok", creator=None, geo="ZA", day=DAY, **extra):
    creator = creator or f"u_{post_id}"
    return {"post_id": post_id, "platform": platform, "url": f"https://x.example/{post_id}", "creator_id": creator,
            "handle": f"h_{creator}", "published_at": None, "post_date": day, "geo_market": geo, "geo_source": "geotag",
            "text": f"text {post_id}", "views": 10, "likes": 1, "comments": None, "shares": None, "engagement": 11,
            "home_market": None, "source_sightings": [], **extra}


class SweepWarehouse:
    """Rows by search: the engagement keyword, the recent keyword, and a semantic row set per query text."""

    def __init__(self, by_engagement=(), by_recent=(), semantic=None, tags=(), fail_semantic=False):
        self.by_engagement, self.by_recent = list(by_engagement), list(by_recent)
        self.semantic = semantic or {}
        self.tags = list(tags)
        self.fail_semantic = fail_semantic
        self.runs = []

    def dry_run(self, sql, params):
        return {"bytes": 1_000, "tables": ["ogilvy-trends-v2.intelligence_42_core.posts"]}

    def run(self, sql, params, max_bytes_billed):
        self.runs.append((sql, params))
        if "co_posts" in sql:
            return [dict(r) for r in self.tags]
        if "tvf_search_posts" in sql:
            if self.fail_semantic:
                raise RuntimeError("semantic boom")
            return [dict(r) for r in self.semantic.get(params["q"], [])]
        rows = self.by_recent if "COALESCE(p.published_at" in sql else self.by_engagement
        return [dict(r) for r in rows]


@pytest.fixture
def ctx():
    return RunContext(run_id="r_sweep", tier="T1", as_of=NOW, market="ZA", window_start=date(2026, 10, 4),
                      window_end=date(2026, 10, 10))


def sweep_queries(ctx):
    return {qid: q for qid, q in ctx.queries.items() if q.get("tool") == SWEEP_TOOL}


# the topic code can name from a question


@pytest.mark.parametrize("question,topic", [
    ("#humor in South Africa this week", ["#humor"]),
    ("What is behind #Humor and #funny?", ["#humor", "#funny"]),
    ("#aa #bb #cc #dd", ["#aa", "#bb", "#cc"]),
    ("#humor then #HUMOR again", ["#humor"]),
    ('How is "load shedding jokes" doing on TikTok?', ["load shedding jokes"]),
    ("What is trending in South Africa this week?", []),
    ("C# developers and issue #1 and a lone # sign", []),
    ("", []),
])
def test_sweep_topic_reads_hashtags_and_quoted_phrases_only(question, topic):
    assert sweep_topic(question) == topic


# the spread order


def test_spread_order_keeps_every_item_once_and_rotates_platforms():
    items = ([("tiktok", f"t{i}", 10 - i) for i in range(6)] + [("x", f"x{i}", 5 - i) for i in range(3)]
             + [("instagram", "i0", 1)])
    out = spread_order(items, platform=lambda r: r[0], creator=lambda r: r[1], strength=lambda r: r[2])
    assert sorted(out) == sorted(items)
    assert {r[0] for r in out[:3]} == {"tiktok", "x", "instagram"}
    assert [r for r in out if r[0] == "tiktok"] == sorted((r for r in items if r[0] == "tiktok"),
                                                          key=lambda r: -r[2])


def test_spread_order_spends_no_creator_more_than_three_posts_before_others_are_taken():
    items = [("tiktok", "big", 100 - i) for i in range(8)] + [("tiktok", f"c{i}", 1) for i in range(4)]
    out = spread_order(items, platform=lambda r: r[0], creator=lambda r: r[1], strength=lambda r: r[2])
    assert [r[1] for r in out[:7]].count("big") == 3
    assert sorted(out) == sorted(items)


def test_spread_order_is_stable_and_tolerates_missing_platform_and_creator():
    items = [(None, None, 1), (None, None, 1), ("x", None, 1)]
    first = spread_order(items, platform=lambda r: r[0], creator=lambda r: r[1], strength=lambda r: r[2])
    again = spread_order(items, platform=lambda r: r[0], creator=lambda r: r[1], strength=lambda r: r[2])
    assert first == again and len(first) == 3
    assert spread_order([], platform=lambda r: r, creator=lambda r: r, strength=lambda r: 0) == []


# the sweep


def test_the_sweep_runs_four_recorded_searches_and_stores_posts_without_returning_them(ctx):
    wh = SweepWarehouse(
        by_engagement=[make_row("e1"), make_row("e2", "x")], by_recent=[make_row("r1"), make_row("e1")],
        semantic={"humor": [make_row("s1", "instagram")], "people posting about humor, South Africa": [make_row("s2")]},
        tags=[{"tag": "funny", "co_posts": 5, "co_creators": 3, "all_posts": 9}])
    out = topic_sweep(ctx, wh, ["#humor"], platforms=None)
    assert set(ctx.evidence) == {"e1", "e2", "r1", "s1", "s2"}
    assert "evidence" not in out and out["posts"] == 5 and out["searches"] == 4 and out["failed"] == {}
    assert len(sweep_queries(ctx)) == 4
    assert out["family"] == ["funny"]
    assert all(q["purpose"].startswith("Topic sweep") for q in sweep_queries(ctx).values())


def test_the_keyword_searches_differ_in_order_and_match_and_the_semantic_ones_in_wording(ctx):
    wh = SweepWarehouse()
    topic_sweep(ctx, wh, ["#humor"], platforms=None)
    keyword = [r for r in wh.runs if "co_posts" not in r[0] and "tvf_search_posts" not in r[0]]
    semantic = [r for r in wh.runs if "tvf_search_posts" in r[0]]
    assert len(keyword) == 2 and len(semantic) == 2
    assert "CONTAINS_SUBSTR(p.text" in keyword[0][0] and "ORDER BY IFNULL(p.engagement" in keyword[0][0]
    assert "CONTAINS_SUBSTR" not in keyword[1][0] and "UNNEST(p.hashtags)" in keyword[1][0]
    assert "ORDER BY COALESCE(p.published_at" in keyword[1][0]
    assert len({r[1]["q"] for r in semantic}) == 2
    for sql, params in wh.runs:
        check_sql(sql)
        assert "humor" not in sql.lower()
        assert params.get("market", "ZA") == "ZA"


def test_a_platform_the_question_names_filters_every_search(ctx):
    wh = SweepWarehouse()
    topic_sweep(ctx, wh, ["#humor"], platforms=["tiktok"])
    for sql, params in wh.runs:
        if "co_posts" in sql:
            continue
        assert "@platform_0" in sql and params["platform_0"] == "tiktok"


def test_the_sweep_balances_platforms_and_caps_a_creator(ctx, monkeypatch):
    monkeypatch.setattr(wh_module, "SWEEP_KEEP", 12)
    heavy = [make_row(f"t{i}", "tiktok", creator=f"c{i // 3}") for i in range(60)]
    heavy += [make_row(f"big{i}", "tiktok", creator="dominant") for i in range(10)]
    wh = SweepWarehouse(by_engagement=heavy,
                        by_recent=[make_row(f"x{i}", "x") for i in range(4)] + [make_row("i0", "instagram")])
    out = topic_sweep(ctx, wh, ["#humor"], platforms=None)
    stored = list(ctx.evidence.values())
    assert len(stored) == 12 == out["posts"]
    assert {r["platform"] for r in stored[:3]} == {"tiktok", "x", "instagram"}
    assert sum(1 for r in stored if r["handle"] == "h_dominant") <= 3
    assert {r["platform"] for r in stored} == {"tiktok", "x", "instagram"}


def test_a_post_outside_the_window_is_not_stored_and_is_listed_as_skipped(ctx):
    wh = SweepWarehouse(by_engagement=[make_row("in1"), make_row("old", day=date(2026, 9, 1))])
    topic_sweep(ctx, wh, ["#humor"], platforms=None)
    assert set(ctx.evidence) == {"in1"}
    skipped = {pid for q in sweep_queries(ctx).values() for pid in q.get("skipped_ids") or ()}
    assert "old" in skipped and "in1" not in skipped


def test_rows_the_sweep_did_not_keep_are_listed_as_skipped_so_the_gate_does_not_fetch_them(ctx, monkeypatch):
    monkeypatch.setattr(wh_module, "SWEEP_KEEP", 2)
    wh = SweepWarehouse(by_engagement=[make_row(f"p{i}") for i in range(6)])
    topic_sweep(ctx, wh, ["#humor"], platforms=None)
    assert len(ctx.evidence) == 2
    skipped = {pid for q in sweep_queries(ctx).values() for pid in q.get("skipped_ids") or ()}
    assert skipped == {f"p{i}" for i in range(6)} - set(ctx.evidence)
    assert ask.listed_post_ids(ctx) == []


def test_one_failed_search_leaves_the_others_and_never_raises(ctx):
    wh = SweepWarehouse(by_engagement=[make_row("e1")], fail_semantic=True)
    out = topic_sweep(ctx, wh, ["#humor"], platforms=None)
    assert set(ctx.evidence) == {"e1"} and out["searches"] == 2
    assert set(out["failed"]) == {"semantic a", "semantic b"}
    assert "semantic boom" in out["failed"]["semantic a"]


def test_every_search_failing_still_returns(ctx):
    class Down(SweepWarehouse):
        def run(self, sql, params, max_bytes_billed):
            raise RuntimeError("down")

    out = topic_sweep(ctx, Down(), ["#humor"], platforms=None)
    assert out["posts"] == 0 and out["searches"] == 0 and len(out["failed"]) == 4 and ctx.evidence == {}


def test_a_sweep_query_reruns_to_the_same_rows_for_k2(ctx):
    wh = SweepWarehouse(by_engagement=[make_row("e1"), make_row("e2")], by_recent=[make_row("r1")])
    topic_sweep(ctx, wh, ["#humor"], platforms=None)
    for qid, query in sweep_queries(ctx).items():
        rerun = checks._rerun(wh, query, ctx)
        assert not isinstance(rerun, Exception)
        assert result_hash(rerun) == query["result_hash"]


def test_a_phrase_topic_is_searched_by_its_words_and_wording(ctx):
    wh = SweepWarehouse(by_engagement=[make_row("e1")])
    out = topic_sweep(ctx, wh, ["load shedding jokes"], platforms=None)
    keyword = [r for r in wh.runs if "co_posts" not in r[0] and "tvf_search_posts" not in r[0]]
    assert [v for k, v in keyword[0][1].items() if k.startswith("term_")] == ["load", "shedding", "jokes"]
    assert out["family"] == [] and not [r for r in wh.runs if "co_posts" in r[0]]


# the wiring in ask.py


QUESTION = "What is behind #humor in South Africa this week?"


class AskWarehouse(StoreWarehouse):
    def run(self, sql, params, max_bytes_billed):
        if "ORDER BY COALESCE(p.published_at" in sql or params.get("k") == 100:  # the sweep's searches only
            return [make_row("sw1", "x", day=date(2026, 9, 25)), make_row("sw2", "tiktok", day=date(2026, 9, 25))]
        if "co_posts" in sql:
            return []
        return super().run(sql, params, max_bytes_billed)


def sweep_harness(research=None):
    h = Harness(research=research or make_research())
    h.warehouse = h.deps.warehouse = AskWarehouse()
    h.deps.store_counts = store_breadth
    h.deps.topic_sweep = topic_sweep
    return h


def test_a_named_topic_runs_the_sweep_beside_the_store_counts_and_its_posts_reach_the_answer_run():
    plain = sweep_harness()
    plain.deps.topic_sweep = None
    base = plain.run(question=QUESTION)
    h = sweep_harness()
    out = h.run(question=QUESTION)
    swept = [r for r in out["query_receipts"].values() if r["purpose"].startswith("Topic sweep")]
    assert len(swept) == 4
    assert out["run"]["posts"] == base["run"]["posts"] + 2
    assert not any(r["purpose"].startswith("Topic sweep") for r in base["query_receipts"].values())


def test_a_question_with_no_hashtag_runs_no_sweep():
    h = sweep_harness()
    out = h.run(question="What is behind amapiano in South Africa this week?")
    assert not any(r["purpose"].startswith("Topic sweep") for r in out["query_receipts"].values())


def test_a_sweep_that_raises_never_fails_the_ask():
    def boom(ctx, warehouse, topic, platforms=None):
        raise RuntimeError("sweep exploded")

    h = sweep_harness()
    h.deps.topic_sweep = boom
    out = h.run(question=QUESTION)
    assert out["answer"] and out["run"]["posts"] >= 1


def test_the_default_deps_wire_the_sweep():
    import inspect

    assert "topic_sweep=topic_sweep" in inspect.getsource(ask._default_deps)
