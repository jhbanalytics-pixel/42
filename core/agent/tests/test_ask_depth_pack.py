"""Ask depth, item 4: a writer pack of up to 300 posts and 300,000 bytes, ordered by how well each post matches the
question's searches and spread across platforms and creators, with the hold computed from the larger input."""

import json
from datetime import date, datetime

import pytest

from core.agent import ask, writer
from core.agent.context import STORE_TOOL, RunContext
from core.agent.tools.warehouse import SWEEP_TOOL

NOW = datetime(2026, 10, 10, 6, 0)


def make_ctx():
    return RunContext(run_id="r_pack", tier="T1", as_of=NOW, market="ZA", window_start=date(2026, 10, 4),
                      window_end=date(2026, 10, 10))


def add_post(ctx, pid, platform="tiktok", handle=None, text="a post", **extra):
    ctx.evidence[pid] = {"id": pid, "platform": platform, "handle": handle or f"h_{pid}", "url": f"https://x/{pid}",
                         "posted_at": "2026-10-08T10:00:00", "market": "ZA", "text": text, "engagement": {"views": 1},
                         "flags": [], "source_market": "ZA", **extra}
    return ctx.evidence[pid]


def record_rows(ctx, purpose, ids, tool=None):
    qid, _ = ctx.record_query("SELECT 1", {}, [{"post_id": i} for i in ids], purpose)
    if tool:
        ctx.queries[qid]["tool"] = tool
    return qid


def pack_ids(ctx, records=None):
    records = list(ctx.evidence.values()) if records is None else records
    blocks, left = writer._pack(ctx, records)
    ids = [json.loads(b.split("\n", 1)[0][len("post "):])["id"] for b in blocks if b.startswith("post ")]
    return ids, left, blocks


def test_the_pack_holds_three_hundred_posts_and_three_hundred_thousand_bytes():
    assert (writer.MAX_PACK_POSTS, writer.WRITER_PACK_BYTES, ask.WRITER_INPUT_TOKENS) == (300, 300_000, 400_000)


def test_a_full_size_pack_fits_the_larger_writer_input():
    ctx = make_ctx()
    for i in range(400):
        add_post(ctx, f"p{i}", text="w" * 900, handle=f"h{i}")
    ids, left, blocks = pack_ids(ctx)
    assert len(ids) <= 300 and left["posts"] >= 100
    assert sum(len(b.encode("utf-8")) for b in blocks) <= writer.WRITER_PACK_BYTES
    system, user = "s" * 5000, "\n\n".join(blocks)
    assert writer._input_token_upper_bound(system, user, writer.WRITER_SCHEMA) <= ask.WRITER_INPUT_TOKENS


def test_a_post_the_searches_matched_comes_before_a_sample_post_and_a_comment(monkeypatch):
    monkeypatch.setattr(writer, "MAX_PACK_POSTS", 2)
    ctx = make_ctx()
    add_post(ctx, "sample")
    add_post(ctx, "comment", parent_id="sample")
    add_post(ctx, "live")
    add_post(ctx, "matched")
    record_rows(ctx, "fetch_posts: 1 posts listed in query rows", ["sample"])
    record_rows(ctx, "search_posts: #humor", ["matched"])
    ids, left, _ = pack_ids(ctx)
    assert ids == ["matched", "live"]  # a live post counts as a hit of the search that fetched it
    assert left["posts"] == 2


def test_a_strong_late_find_beats_a_weak_early_one_when_the_pack_is_full(monkeypatch):
    monkeypatch.setattr(writer, "MAX_PACK_POSTS", 3)
    ctx = make_ctx()
    for pid in ("early1", "early2", "early3", "late_strong"):
        add_post(ctx, pid, handle=f"h_{pid}")
    record_rows(ctx, "search_posts: a", ["late_strong", "early1", "early2", "early3"])
    record_rows(ctx, "Topic sweep, keyword: humor", ["late_strong"], tool=SWEEP_TOOL)
    ids, _, _ = pack_ids(ctx)
    assert ids[0] == "late_strong" and set(ids) == {"late_strong", "early1", "early2"}


def test_a_full_pack_still_spreads_across_platforms_and_creators(monkeypatch):
    monkeypatch.setattr(writer, "MAX_PACK_POSTS", 30)
    ctx = make_ctx()
    tiktok = [f"t{i}" for i in range(100)]
    for i, pid in enumerate(tiktok):
        add_post(ctx, pid, "tiktok", handle=f"c{i // 10}")  # ten posts a creator
    for i in range(5):
        add_post(ctx, f"x{i}", "x")
        add_post(ctx, f"i{i}", "instagram")
    record_rows(ctx, "search_posts: a", tiktok + [f"x{i}" for i in range(5)] + [f"i{i}" for i in range(5)])
    ids, _, _ = pack_ids(ctx)
    platforms = [ctx.evidence[i]["platform"] for i in ids]
    assert platforms.count("x") == 5 and platforms.count("instagram") == 5
    tiktok_creators = [ctx.evidence[i]["handle"] for i in ids if ctx.evidence[i]["platform"] == "tiktok"]
    assert max(tiktok_creators.count(c) for c in set(tiktok_creators)) <= 10
    assert len(set(tiktok_creators)) >= 7  # 20 tiktok places over creators capped at three before any repeats


def test_the_pack_order_is_deterministic_and_drops_nothing_when_nothing_is_cut():
    ctx = make_ctx()
    for i in range(20):
        add_post(ctx, f"p{i}", "tiktok" if i % 2 else "x", handle=f"h{i % 5}")
    record_rows(ctx, "search_posts: a", [f"p{i}" for i in range(0, 20, 3)])
    first, left, _ = pack_ids(ctx)
    again, _, _ = pack_ids(ctx)
    assert first == again and sorted(first) == sorted(ctx.evidence) and left == {"posts": 0, "queries": 0}


def test_a_sweep_query_adds_no_block_of_its_own_to_the_pack():
    ctx = make_ctx()
    add_post(ctx, "p1")
    record_rows(ctx, "Topic sweep, keyword: humor", ["p1"], tool=SWEEP_TOOL)
    qid, _ = ctx.record_query("SELECT 2", {}, [{"n": 1}], "Whole-store totals per platform")
    ctx.queries[qid]["tool"] = STORE_TOOL
    ids, left, blocks = pack_ids(ctx)
    assert ids == ["p1"] and left == {"posts": 0, "queries": 0}
    assert not any("Topic sweep" in b for b in blocks) and any("Whole-store totals" in b for b in blocks)


def test_the_model_hold_rises_by_exactly_the_two_larger_writer_inputs(monkeypatch):
    for model in (ask.MODEL, ask.FALLBACK_MODEL):
        new = ask.hold_usd("T1", model)
        monkeypatch.setattr(ask, "WRITER_INPUT_TOKENS", 200_000)
        old = ask.hold_usd("T1", model)
        monkeypatch.undo()
        grown = ask.call_usd(model, 400_000, ask.WRITER_MAX_TOKENS) - ask.call_usd(model, 200_000, ask.WRITER_MAX_TOKENS)
        assert new - old == pytest.approx(2 * grown) and new > old


# Review, Important 3: a comment the run paid for takes its parent's strength


def test_paid_comments_on_strong_posts_reach_the_pack_when_the_posts_alone_fill_it():
    ctx = make_ctx()
    ids = [f"p{i}" for i in range(300)]
    for pid in ids:
        add_post(ctx, pid, handle=f"h_{pid}")
    record_rows(ctx, "Topic sweep, keyword: humor", ids, tool=SWEEP_TOOL)
    record_rows(ctx, "search_posts: humor", ids)
    for i in range(12):
        add_post(ctx, f"c{i}", handle=f"commenter{i}", parent_id=f"p{i}")
    packed, left, _ = pack_ids(ctx)
    assert len(packed) == 300 and left["posts"] == 12
    assert {f"c{i}" for i in range(12)} <= set(packed)
    assert not {f"p{i}" for i in range(288, 300)} & set(packed)  # the twelve weakest posts make the room


def test_a_comment_on_a_weak_post_stays_behind_the_strong_ones_and_an_orphan_comment_weighs_nothing(monkeypatch):
    monkeypatch.setattr(writer, "MAX_PACK_POSTS", 3)
    ctx = make_ctx()
    for pid in ("strong", "weak", "other"):
        add_post(ctx, pid)
    record_rows(ctx, "search_posts: a", ["strong", "other", "weak"])
    add_post(ctx, "on_weak", handle="c1", parent_id="weak")
    add_post(ctx, "orphan", handle="c2", parent_id="not_in_the_run")
    packed, _, _ = pack_ids(ctx)
    assert packed == ["strong", "other", "weak"]  # on_weak ties with its parent and comes after it
    assert "orphan" not in packed and "on_weak" not in packed
