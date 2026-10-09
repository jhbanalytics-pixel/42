"""N21: post_enrichment.near_dup_size, written from the stored captions (core/detect/neardup.py).

near_dup_size of a post is 1 plus the number of other posts seen in the last 7 days whose caption is a near
duplicate of its own: Jaccard 0.8 on character 5-grams of the text without hashtags, links, handles and numbers, at
least 20 characters (the rule core/detect/coaction.py already uses). Only sizes of 2 or more are written; a post
without a row reads as 1, which is what the views and the evidence pack already do.
"""

import random
import re

import pytest

from core.detect import job, neardup
from core.detect.tests import duck
from core.detect.tests.fixtures import D, at, day, obs, post, rid, run

CAPTION = "Capetonians are going mad for the new shaya step challenge this weekend"


@pytest.fixture
def con():
    return duck.connect()


def rows(*texts):
    return [{"post_id": f"p{i}", "text": t} for i, t in enumerate(texts)]


# The size of a post's near-duplicate neighbourhood


def test_three_copies_of_one_caption_are_each_size_three_and_an_unrelated_post_is_not_listed():
    sizes = neardup.near_dup_sizes(rows(CAPTION, CAPTION.upper(), CAPTION + " #shayastep https://t.example/x",
                                        "My gran learned the new school dance in one afternoon"))
    assert sizes == {"p0": 3, "p1": 3, "p2": 3}


def test_links_handles_numbers_and_hashtags_do_not_count_toward_similarity():
    sizes = neardup.near_dup_sizes(rows(f"{CAPTION} https://a.example/1 @one 2026 #a",
                                        f"{CAPTION} https://b.example/2 @two 2027 #b #c"))
    assert sizes == {"p0": 2, "p1": 2}


def test_short_or_missing_captions_never_count():
    assert neardup.near_dup_sizes(rows("so true", "so true", None, "", "#fyp #fyp")) == {}


def test_a_different_caption_of_the_same_length_is_not_a_near_duplicate():
    assert neardup.near_dup_sizes(rows(CAPTION, "Nobody told me the shaya step was this hard on the knees")) == {}


def test_the_sizes_do_not_depend_on_the_order_of_the_rows():
    base = rows(CAPTION, CAPTION + "!", CAPTION.upper(), "Matric farewell rehearsal went completely off the rails",
                "Matric farewell rehearsal went completely off the rails.", "something else altogether new today")
    expected = neardup.near_dup_sizes(base)
    assert expected == {"p0": 3, "p1": 3, "p2": 3, "p3": 2, "p4": 2}
    rng = random.Random(21)
    for _ in range(5):
        shuffled = base[:]
        rng.shuffle(shuffled)
        assert neardup.near_dup_sizes(shuffled) == expected


# Reading and writing post_enrichment


def load(con, posts, observations, enrichment=()):
    duck.load(con, "core.posts", posts)
    duck.load(con, "core.post_observations", observations)
    if enrichment:
        duck.load(con, "core.post_enrichment", list(enrichment))


def copies(con, n, *, seen=D, lane_class="unbiased_rank", lane="sweep", text=CAPTION, prefix="c", enrichment=()):
    load(con, [post(f"{prefix}{i}", f"u{i}", seen, text=text) for i in range(n)],
         [obs(f"{prefix}{i}", seen, lane_class, lane) for i in range(n)], enrichment)


def written(con):
    return duck.query(con, "SELECT pe.post_id, pe.near_dup_size FROM {core}.post_enrichment pe "
                           "ORDER BY pe.post_id, pe.near_dup_size")


def test_run_writes_one_row_with_only_post_id_and_size_for_each_near_duplicate_post(con):
    copies(con, 3)
    load(con, [post("other", "u9", D, text="My gran learned the new school dance in one afternoon")],
         [obs("other", D, "unbiased_rank", "sweep")])
    counts = neardup.run_neardup(duck.Client(con), D, core="core", agent="agent")
    assert written(con) == [{"post_id": f"c{i}", "near_dup_size": 3} for i in range(3)]
    assert counts == {"posts": 4, "near_dup_posts": 3, "written": 3}


def test_a_second_run_appends_nothing_and_a_larger_stored_size_is_never_lowered(con):
    copies(con, 3, enrichment=[{"post_id": "c0", "near_dup_size": 5}, {"post_id": "c1", "near_dup_size": 2}])
    client = duck.Client(con)
    neardup.run_neardup(client, D, core="core", agent="agent")
    assert written(con) == [{"post_id": "c0", "near_dup_size": 5}, {"post_id": "c1", "near_dup_size": 2},
                            {"post_id": "c1", "near_dup_size": 3}, {"post_id": "c2", "near_dup_size": 3}]
    counts = neardup.run_neardup(client, D, core="core", agent="agent")
    assert counts["written"] == 0 and len(written(con)) == 4


def test_posts_outside_the_seven_day_window_or_in_excluded_lanes_are_not_read(con):
    copies(con, 2, seen=day(7), prefix="old")
    copies(con, 2, lane_class="legacy", lane="legacy", prefix="leg")
    copies(con, 2, lane_class="search_presence", lane="placebo", prefix="plc")
    copies(con, 2, lane_class="search_presence", lane="agent_live", prefix="live")
    copies(con, 1, prefix="one")
    counts = neardup.run_neardup(duck.Client(con), D, core="core", agent="agent")
    assert counts == {"posts": 1, "near_dup_posts": 0, "written": 0}
    assert written(con) == []


def test_a_post_seen_in_two_lanes_counts_once(con):
    copies(con, 1, prefix="a")
    load(con, [post("b0", "u8", D, text=CAPTION)], [obs("b0", D, "unbiased_rank", "sweep"),
                                                    obs("b0", D, "panel", "panel_hub")])
    neardup.run_neardup(duck.Client(con), D, core="core", agent="agent")
    assert written(con) == [{"post_id": "a0", "near_dup_size": 2}, {"post_id": "b0", "near_dup_size": 2}]


def test_the_statements_only_read_and_insert(con):
    copies(con, 3)
    client = duck.Client(con)
    neardup.run_neardup(client, D, core="core", agent="agent")
    for sql in client.sql:
        upper = re.sub(r"--[^\n]*", "", sql).upper()
        for word in ("DELETE", "DROP", "TRUNCATE", "ALTER", "REPLACE", "CREATE", "MERGE", "UPDATE"):
            assert word not in upper
        assert re.match(r"\s*(SELECT|WITH|INSERT)\b", upper)


def test_inserts_are_chunked(con, monkeypatch):
    monkeypatch.setattr(neardup, "CHUNK", 2)
    copies(con, 3)
    client = duck.Client(con)
    neardup.run_neardup(client, D, core="core", agent="agent")
    assert len([s for s in client.sql if "INSERT INTO" in s.upper()]) == 2


# The evidence pack and the views read what it wrote


def test_the_views_read_the_written_size_as_a_near_duplicate_from_three_posts(con):
    copies(con, 3)
    copies(con, 2, prefix="d", text="My gran learned the new school dance in one afternoon")
    neardup.run_neardup(duck.Client(con), D, core="core", agent="agent")
    assert {r["post_id"]: r["near_dup_size"] for r in written(con)} == {"c0": 3, "c1": 3, "c2": 3, "d0": 2, "d1": 2}
    flagged = duck.query(con, "SELECT pe.post_id, IFNULL(MAX(pe.near_dup_size), 1) >= 3 near_dup "
                              "FROM {core}.post_enrichment pe GROUP BY pe.post_id ORDER BY 1")
    assert [r["post_id"] for r in flagged if r["near_dup"]] == ["c0", "c1", "c2"]


# The detect job runs it before state and it never stops detect


class Chain:
    def __init__(self, con):
        self.con = con
        self.events = []

    def begin(self, stage, d):
        class R:
            run_id = rid(stage, d)
        return R()

    def finish(self, r, status, counts, error=None):
        self.events.append((status, counts, error))

    def start_next(self, stage, d):
        pass


def test_the_step_returns_the_counts(con):
    copies(con, 3)
    assert job.run_neardup_step(duck.Client(con), D, "core", "agent") == {"posts": 3, "near_dup_posts": 3,
                                                                          "written": 3}


@pytest.mark.parametrize("error, status", [(ImportError("No module named datasketch"), "skipped"),
                                           (ValueError("boom"), "failed")])
def test_a_failing_step_is_returned_not_raised(con, monkeypatch, error, status):
    def boom(*a, **k):
        raise error
    monkeypatch.setattr(neardup, "run_neardup", boom)
    out = job.run_neardup_step(duck.Client(con), D, "core", "agent")
    assert out["status"] == status and type(error).__name__ in out["error"]


def test_the_job_runs_the_step_after_coaction_and_before_state(con, monkeypatch):
    order = []
    monkeypatch.setattr(job, "run_coaction_step", lambda *a, **k: order.append("coaction") or {})
    monkeypatch.setattr(job, "run_neardup_step", lambda *a, **k: order.append("neardup") or {})
    monkeypatch.setattr(job, "run_state", lambda *a, **k: order.append("state") or 0)
    for name in ("run_breakout_step", "run_watch_step", "run_seeds_step", "run_forecast_step", "run_centroids_step",
                 "apply_spread_step", "apply_agent_views_step", "apply_news_step"):
        monkeypatch.setattr(job, name, lambda *a, **k: {})
    monkeypatch.setattr(job.sqlrun, "apply_views", lambda *a, **k: None)
    monkeypatch.setattr(job, "apply_waves", lambda *a, **k: None)
    monkeypatch.setattr(job, "_step", lambda *a, **k: {"series_test": 0})
    monkeypatch.setattr(job.sqlrun, "query", lambda *a, **k: [])
    counts = job.run(duck.Client(con), D, chain=Chain(con), core="core", agent="agent")
    assert order == ["coaction", "neardup", "state"] and "near_dup" in counts


# The candidate pairs are confirmed, and links alone are not a caption


def test_a_hash_candidate_below_the_threshold_is_not_a_near_duplicate():
    a = "ixlzwxuq oyhu fdlp mrdsh xgni ymfyz etto eaagy ffjkg vugfwgmj lnfe ckjtsa vwkcjljp fppwfb"
    b = "ixlzwxuq oyhu fdlp mrdsh xgni ymfyz etto eaagy ffjkg tufhyvf lnfe ckjtsa vwkcjljp fppwfb"
    assert neardup.similar(neardup.shingles(neardup.plain_text(a)), neardup.shingles(neardup.plain_text(b))) is False
    assert neardup.near_dup_sizes(rows(a, b)) == {}


def test_a_caption_that_is_only_links_is_not_long_enough():
    links = "look https://a.example/{n}1 https://a.example/{n}2 https://a.example/{n}3 https://a.example/{n}4"
    assert neardup.plain_text(links.format(n="x")) is None
    assert neardup.near_dup_sizes(rows(links.format(n="x"), links.format(n="y"))) == {}
