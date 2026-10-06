"""Unit tests for scripts/hashtags_driving.py pure helpers.

The BQ-touching `_fetch`/`build` are validated by the live run in the PR;
the ranking + stoplist logic that decides what the team sees is tested
here without BigQuery.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.hashtags_driving import (
    is_generic,
    rank_topic_hashtags,
)


def _row(market, topic, tag, posts, eng):
    return {"market": market, "topic": topic, "hashtag": tag, "posts": posts, "engagement": eng}


# --- is_generic --------------------------------------------------------


def test_generic_tags_are_dropped():
    assert is_generic("#fyp")
    assert is_generic("#ForYou")
    assert is_generic("viral")  # no leading hash, still matches
    assert is_generic("#TIKTOK")


def test_topic_tags_are_kept():
    assert not is_generic("#amapiano")
    assert not is_generic("#mpesa")
    assert not is_generic("#nyamachoma")
    assert not is_generic("#mamamboga")


def test_numeric_artifact_tags_are_dropped():
    # #8217 / #039 / #254 are HTML-entity numbers from apostrophes in text.
    assert is_generic("#8217")
    assert is_generic("#039")
    assert is_generic("#254")


# --- rank_topic_hashtags ----------------------------------------------


def test_ranking_drops_generic_and_keeps_topic_tags():
    rows = [
        _row("ke", "fintech_mpesa", "#mpesa", 50, 6_700_000),
        _row("ke", "fintech_mpesa", "#fyp", 40, 9_000_000),  # generic, dropped
        _row("ke", "fintech_mpesa", "#safaricom", 8, 2_000_000),
    ]
    out = rank_topic_hashtags(rows, top=5)
    assert len(out) == 1
    tags = [h["tag"] for h in out[0].hashtags]
    assert "#fyp" not in tags
    assert tags == ["#mpesa", "#safaricom"]


def test_ranking_sorts_by_posts_then_engagement():
    rows = [
        _row("za", "music_amapiano", "#yanos", 10, 100),
        _row("za", "music_amapiano", "#amapiano", 10, 500),  # tie on posts, higher eng first
        _row("za", "music_amapiano", "#groove", 20, 1),  # most posts -> first
    ]
    out = rank_topic_hashtags(rows, top=5)
    tags = [h["tag"] for h in out[0].hashtags]
    assert tags == ["#groove", "#amapiano", "#yanos"]


def test_ranking_respects_top_n():
    rows = [_row("ng", "music_afrobeats", f"#tag{i}", 100 - i, 1) for i in range(10)]
    out = rank_topic_hashtags(rows, top=3)
    assert len(out[0].hashtags) == 3
    assert [h["tag"] for h in out[0].hashtags] == ["#tag0", "#tag1", "#tag2"]


def test_ranking_groups_by_market_and_topic():
    rows = [
        _row("ke", "a", "#one", 5, 1),
        _row("ng", "a", "#two", 5, 1),
        _row("ke", "b", "#three", 5, 1),
    ]
    out = rank_topic_hashtags(rows, top=5)
    keys = sorted((t.market, t.topic) for t in out)
    assert keys == [("ke", "a"), ("ke", "b"), ("ng", "a")]


def test_topic_with_only_generic_tags_is_omitted():
    rows = [_row("za", "x", "#fyp", 9, 1), _row("za", "x", "#viral", 8, 1)]
    out = rank_topic_hashtags(rows, top=5)
    assert out == []
