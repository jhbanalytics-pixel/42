"""Unit tests for src/scoring/driving_hashtags.py pure helpers + producer.

The BQ + Gemini paths are exercised via injected mocks; the live BQ + cloud
Gemini path is validated end-to-end during gate-3 shadow-validate. Mirrors
the test layout for src/analysis/comment_sentiment.py.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.scoring.driving_hashtags import (
    DEFAULT_STOPLIST,
    MOOD_CHAR_CAP,
    _fetch_content_mentioning_tag,
    _fetch_topic_tag_counts,
    _harden_mood,
    _scrub_dashes,
    compute_driving_hashtags,
    is_generic,
    mood_for_tag,
    rank_tags,
    tag_briefs_with_driving_hashtags,
)

# ----- is_generic ------------------------------------------------------


def test_generic_platform_tags_dropped():
    assert is_generic("#fyp")
    assert is_generic("#ForYou")
    assert is_generic("viral")
    assert is_generic("#TIKTOK")


def test_numeric_artifact_tags_dropped():
    # HTML-entity remnants like &#8217; become "#8217" via the regex.
    assert is_generic("#8217")
    assert is_generic("#039")
    assert is_generic("#254")


def test_empty_tag_drops():
    assert is_generic("#")
    assert is_generic("")


def test_topic_tags_kept():
    assert not is_generic("#amapiano")
    assert not is_generic("#mpesa")
    assert not is_generic("#mamamboga")
    assert not is_generic("#nyamachoma")


# ----- _scrub_dashes ---------------------------------------------------


def test_scrub_dashes_strips_all_three():
    # Build the em/en chars via chr() so the test source itself stays free of
    # literal em/en dashes (the repo audit hard-fails on those everywhere).
    em = chr(0x2014)
    en = chr(0x2013)
    s = f"double--hyphen {em}em {en}en"
    out = _scrub_dashes(s)
    assert "--" not in out
    assert em not in out
    assert en not in out


# ----- rank_tags -------------------------------------------------------


def _row(tag, posts, eng):
    return {"tag": tag, "posts": posts, "engagement": eng}


def test_rank_drops_generic_and_keeps_topic_tags():
    rows = [
        _row("#mpesa", 50, 6_700_000),
        _row("#fyp", 40, 9_000_000),
        _row("#safaricom", 8, 2_000_000),
    ]
    out = rank_tags(rows, top_n=5, min_posts=3)
    tags = [r["tag"] for r in out]
    assert "#fyp" not in tags
    assert tags == ["#mpesa", "#safaricom"]


def test_rank_respects_min_posts_floor():
    rows = [_row("#a", 10, 1), _row("#b", 2, 99)]  # b below floor
    out = rank_tags(rows, top_n=5, min_posts=3)
    assert [r["tag"] for r in out] == ["#a"]


def test_rank_sorts_by_posts_then_engagement():
    rows = [
        _row("#yanos", 10, 100),
        _row("#amapiano", 10, 500),  # tie on posts, higher eng wins
        _row("#groove", 20, 1),  # most posts -> first
    ]
    out = rank_tags(rows, top_n=5, min_posts=3)
    assert [r["tag"] for r in out] == ["#groove", "#amapiano", "#yanos"]


def test_rank_caps_at_top_n():
    rows = [_row(f"#t{i}", 100 - i, 1) for i in range(10)]
    out = rank_tags(rows, top_n=3, min_posts=3)
    assert len(out) == 3
    assert [r["tag"] for r in out] == ["#t0", "#t1", "#t2"]


def test_rank_includes_share_pct_of_total_tagged_posts():
    rows = [_row("#a", 30, 1), _row("#b", 70, 1)]
    out = rank_tags(rows, top_n=5, min_posts=3)
    shares = {r["tag"]: r["share_pct"] for r in out}
    # Total tagged posts after filter = 100; #a is 30%, #b is 70%.
    assert shares["#a"] == 30.0
    assert shares["#b"] == 70.0


def test_rank_share_pct_denominator_counts_pruned_tags():
    # card.py renders share_pct as "X% of tagged posts", so the denominator must
    # be every extracted tag, not just the survivors. Here #fyp (generic) and #z
    # (below the min-posts floor) are pruned but still count toward the total, so
    # #real is 30 / (30 + 60 + 2) = 32.6%, not 30 / 30 = 100%.
    rows = [
        _row("#real", 30, 1),
        _row("#fyp", 60, 1),  # generic, dropped from output but counted in total
        _row("#z", 2, 1),  # below floor, dropped from output but counted in total
    ]
    out = rank_tags(rows, top_n=5, min_posts=3)
    assert [r["tag"] for r in out] == ["#real"]
    assert out[0]["share_pct"] == 32.6


def test_rank_returns_empty_when_all_generic():
    rows = [_row("#fyp", 9, 1), _row("#viral", 8, 1)]
    out = rank_tags(rows, top_n=5, min_posts=3)
    assert out == []


def test_rank_returns_empty_when_all_below_floor():
    rows = [_row("#a", 1, 1), _row("#b", 2, 1)]
    out = rank_tags(rows, top_n=5, min_posts=3)
    assert out == []


# ----- _harden_mood ----------------------------------------------------


def test_harden_mood_clamps_length():
    long = "x" * (MOOD_CHAR_CAP + 50)
    out = _harden_mood(long)
    assert out is not None
    assert len(out) <= MOOD_CHAR_CAP


def test_harden_mood_strips_double_quotes_via_reject():
    # A smuggled quote means the model copied verbatim. Reject the whole field.
    assert _harden_mood('"verbatim"') is None
    assert _harden_mood("ok mood") == "ok mood"


def test_harden_mood_scrubs_dashes():
    out = _harden_mood("calm--practical")
    assert out is not None
    assert "--" not in out


def test_harden_mood_rejects_non_string():
    assert _harden_mood(None) is None
    assert _harden_mood(123) is None
    assert _harden_mood([]) is None


def test_harden_mood_rejects_blank():
    assert _harden_mood("") is None
    assert _harden_mood("   ") is None


# ----- mood_for_tag (Gemini call) --------------------------------------


class _MockResp:
    def __init__(self, parsed):
        self.parsed = parsed


class _MockClient:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = 0

    def generate_brief(self, *_a, **_kw):
        self.calls += 1
        return _MockResp(self.parsed)


class _RaisingClient:
    def generate_brief(self, *_a, **_kw):
        raise RuntimeError("transient 503")


def test_mood_for_tag_happy_path():
    client = _MockClient({"mood": "practical and supportive"})
    out = mood_for_tag("#stokvel", ["c1", "c2"], "za", "finance_stokvel", client)
    assert out == "practical and supportive"
    assert client.calls == 1


def test_mood_for_tag_empty_corpus_skips_call():
    client = _MockClient({"mood": "ignored"})
    out = mood_for_tag("#x", [], "za", "topic", client)
    assert out is None
    assert client.calls == 0


def test_mood_for_tag_non_fatal_on_raise():
    out = mood_for_tag("#x", ["c"], "za", "topic", _RaisingClient())
    assert out is None


def test_mood_for_tag_rejects_smuggled_quote():
    client = _MockClient({"mood": 'they said "yes"'})
    assert mood_for_tag("#x", ["c"], "za", "topic", client) is None


# ----- compute_driving_hashtags (top-level orchestration) --------------


class _BqMock:
    """Captures topic_group + tag and returns the right rows for each call.

    The producer issues TWO query shapes: ``_fetch_topic_tag_counts`` (no
    tag param) and ``_fetch_content_mentioning_tag`` (tag param present).
    Records every SQL string issued so a test can assert the corpus query is
    no longer reddit-content-type-scoped.
    """

    project = "ogilvy-trends-v2"

    def __init__(self, tag_rows_by_topic, comments_by_tag):
        self.tag_rows_by_topic = tag_rows_by_topic
        self.comments_by_tag = comments_by_tag
        self.sqls: list[str] = []

    def query(self, _sql, *, job_config):
        self.sqls.append(_sql)
        params = {p.name: p.value for p in job_config.query_parameters}
        topic = params.get("topic_group")
        tag_regex = params.get("tag_regex")
        if tag_regex is None:
            rows = self.tag_rows_by_topic.get(topic, [])
        else:
            # tag_regex is re.escape("#tag") + r"(\W|$)"; recover the bare tag
            # by dropping the trailing boundary group and unescaping.
            import re as _re

            bare = tag_regex[: -len(r"(\W|$)")]
            tag = _re.sub(r"\\(.)", r"\1", bare)
            rows = [{"text": c} for c in self.comments_by_tag.get(tag, [])]
        return _BqJob(rows)


class _BqJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return iter([_BqRow(r) for r in self._rows])


class _BqRow:
    def __init__(self, d):
        self._d = d

    def items(self):
        return self._d.items()

    def keys(self):
        return self._d.keys()

    def __iter__(self):
        return iter(self._d)

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)


# ----- _fetch_content_mentioning_tag (corpus breadth) -----------------


def test_corpus_query_is_not_reddit_content_type_scoped():
    # A TikTok caption carrying the tag must be returned, and the SQL must no
    # longer pin content_type to reddit_comment/reddit_post. Hashtags live in
    # caption text across TikTok/Instagram/Threads, almost never in reddit.
    bq = _BqMock(
        tag_rows_by_topic={},
        comments_by_tag={"#amapiano": ["yano season is here", "kabza dropped a set"]},
    )
    out = _fetch_content_mentioning_tag(
        bq,
        "ogilvy-trends-v2",
        "trends_v2_dev",
        "za",
        "music_amapiano",
        "#amapiano",
        datetime.date(2026, 6, 7),
    )
    # The non-reddit caption rows came back (mock keys purely on the tag LIKE).
    assert out == ["yano season is here", "kabza dropped a set"]
    # The issued SQL no longer filters by reddit content_type.
    assert len(bq.sqls) == 1
    sql = bq.sqls[0].lower()
    assert "content_type" not in sql
    assert "reddit_comment" not in sql
    assert "reddit_post" not in sql
    # The corpus match is a word-boundary regex, not a prefix-colliding LIKE.
    assert "regexp_contains" in sql
    assert "@tag_regex" in sql
    assert "like @tag_like" not in sql


def test_corpus_tag_regex_anchors_on_word_boundary(monkeypatch):
    # The regex passed to BigQuery must match #japa as a whole tag but NOT
    # prefix-match #japan, and the LIKE single-char wildcard '_' must be a
    # literal, not a wildcard.
    import re

    captured = {}

    class _CaptureBQ:
        project = "ogilvy-trends-v2"

        def query(self, _sql, *, job_config):
            params = {p.name: p.value for p in job_config.query_parameters}
            captured["regex"] = params["tag_regex"]
            return _BqJob([])

    _fetch_content_mentioning_tag(
        _CaptureBQ(),
        "ogilvy-trends-v2",
        "trends_v2_dev",
        "ng",
        "diaspora_japa",
        "#japa",
        datetime.date(2026, 6, 7),
    )
    pat = re.compile(captured["regex"])
    assert pat.search("dreaming of #japa life")
    assert pat.search("packed and ready, #japa!")
    assert not pat.search("visiting #japan next week")


def test_tag_count_query_dedupes_tags_per_post():
    # A caption repeating a tag must count the post once, not once per
    # occurrence, so the aggregate counts distinct posts and sums engagement
    # once per post rather than per hashtag occurrence.
    captured = {}

    class _CaptureBQ:
        project = "ogilvy-trends-v2"

        def query(self, _sql, *, job_config):
            captured["sql"] = _sql
            return _BqJob([])

    _fetch_topic_tag_counts(
        _CaptureBQ(),
        "ogilvy-trends-v2",
        "trends_v2_dev",
        "za",
        "music_amapiano",
        datetime.date(2026, 6, 7),
    )
    sql = captured["sql"].lower()
    assert "select distinct id" in sql
    assert "count(distinct id)" in sql


def test_compute_skips_geo_homonym_topic(monkeypatch):
    # diaspora_japa is geo-homonym deny-listed: the card must CAGE (absent from
    # output) even when the corpus would return a rankable tag + a valid mood,
    # because "japa" collides with Brazilian-Portuguese / Sanskrit. A clean topic
    # in the same batch still renders.
    monkeypatch.setattr("src.utils.bigquery.get_dataset", lambda: "trends_v2_dev")
    bq = _BqMock(
        tag_rows_by_topic={
            "diaspora_japa": [{"tag": "#sushi", "posts": 40, "engagement": 9}],
            "music_afrobeats": [{"tag": "#afrobeats", "posts": 50, "engagement": 9}],
        },
        comments_by_tag={"#sushi": ["c"], "#afrobeats": ["great vibes"]},
    )
    gemini = _MockClient({"mood": "vibrant"})
    out = compute_driving_hashtags(
        datetime.date(2026, 6, 8),
        {("ng", "diaspora_japa"): {}, ("ng", "music_afrobeats"): {}},
        gemini_client=gemini,
        bq_client=bq,
        dataset="trends_v2_dev",
    )
    assert ("ng", "diaspora_japa") not in out
    assert ("ng", "music_afrobeats") in out


def test_compute_skips_topic_with_no_tag_rows(monkeypatch):
    monkeypatch.setattr("src.utils.bigquery.get_dataset", lambda: "trends_v2_dev")
    bq = _BqMock(tag_rows_by_topic={}, comments_by_tag={})
    gemini = _MockClient({"mood": "ok"})
    out = compute_driving_hashtags(
        datetime.date(2026, 6, 7),
        {("za", "finance_stokvel"): {}},
        gemini_client=gemini,
        bq_client=bq,
        dataset="trends_v2_dev",
    )
    assert out == {}
    assert gemini.calls == 0


def test_compute_skips_topic_with_only_generic_tags(monkeypatch):
    monkeypatch.setattr("src.utils.bigquery.get_dataset", lambda: "trends_v2_dev")
    bq = _BqMock(
        tag_rows_by_topic={
            "finance_stokvel": [
                {"tag": "#fyp", "posts": 100, "engagement": 1},
                {"tag": "#viral", "posts": 90, "engagement": 1},
            ]
        },
        comments_by_tag={},
    )
    gemini = _MockClient({"mood": "ok"})
    out = compute_driving_hashtags(
        datetime.date(2026, 6, 7),
        {("za", "finance_stokvel"): {}},
        gemini_client=gemini,
        bq_client=bq,
        dataset="trends_v2_dev",
    )
    assert out == {}
    assert gemini.calls == 0


def test_compute_happy_path_top_3_with_mood(monkeypatch):
    monkeypatch.setattr("src.utils.bigquery.get_dataset", lambda: "trends_v2_dev")
    bq = _BqMock(
        tag_rows_by_topic={
            "finance_stokvel": [
                {"tag": "#stokvel", "posts": 50, "engagement": 1_000_000},
                {"tag": "#sidehustle", "posts": 30, "engagement": 500_000},
                {"tag": "#mzansi", "posts": 10, "engagement": 100_000},
                {"tag": "#fyp", "posts": 80, "engagement": 9_000_000},  # generic, dropped
            ]
        },
        comments_by_tag={
            "#stokvel": ["practical talk", "good advice"],
            "#sidehustle": ["ambitious", "hustle hard"],
            "#mzansi": ["proud", "south africa"],
        },
    )
    gemini = _MockClient({"mood": "practical"})
    out = compute_driving_hashtags(
        datetime.date(2026, 6, 7),
        {("za", "finance_stokvel"): {}},
        gemini_client=gemini,
        bq_client=bq,
        dataset="trends_v2_dev",
        top_n=3,
        min_posts=3,
    )
    key = ("za", "finance_stokvel")
    assert key in out
    tags = out[key]["driving_hashtags"]
    assert [t["tag"] for t in tags] == ["#stokvel", "#sidehustle", "#mzansi"]
    for t in tags:
        assert t["mood"] == "practical"
        assert "share_pct" in t
    # One Gemini call per surviving tag (3 tags here).
    assert gemini.calls == 3


def test_compute_drops_tag_when_mood_fails(monkeypatch):
    monkeypatch.setattr("src.utils.bigquery.get_dataset", lambda: "trends_v2_dev")
    bq = _BqMock(
        tag_rows_by_topic={"t": [{"tag": "#a", "posts": 10, "engagement": 1}]},
        comments_by_tag={"#a": []},  # empty corpus -> mood None -> tag dropped
    )
    out = compute_driving_hashtags(
        datetime.date(2026, 6, 7),
        {("za", "t"): {}},
        gemini_client=_MockClient({"mood": "ignored"}),
        bq_client=bq,
        dataset="trends_v2_dev",
    )
    # No surviving tag -> no entry for the topic.
    assert out == {}


# ----- tag_briefs_with_driving_hashtags --------------------------------


def test_tag_briefs_attaches_field_in_place():
    briefs = {("za", "t"): {}}
    payload = {
        ("za", "t"): {
            "driving_hashtags": [
                {"tag": "#stokvel", "posts": 50, "mood": "practical", "share_pct": 60.0}
            ]
        }
    }
    n = tag_briefs_with_driving_hashtags(briefs, payload)
    assert n == 1
    assert briefs[("za", "t")]["driving_hashtags"][0]["tag"] == "#stokvel"
    # Defensive copy: payload mutation must not leak.
    payload[("za", "t")]["driving_hashtags"][0]["tag"] = "MUTATED"
    assert briefs[("za", "t")]["driving_hashtags"][0]["tag"] == "#stokvel"


def test_tag_briefs_skips_missing_topic():
    briefs = {("za", "t"): {}, ("ng", "x"): {}}
    payload = {("za", "t"): {"driving_hashtags": [{"tag": "#a", "mood": "m"}]}}
    n = tag_briefs_with_driving_hashtags(briefs, payload)
    assert n == 1
    assert "driving_hashtags" not in briefs[("ng", "x")]


def test_default_stoplist_contains_known_generics():
    # Spot-check the stoplist surface; if a generic drops out of the set
    # someone has loosened the boundary and the slot will pollute.
    for s in ("fyp", "foryou", "viral", "tiktok", "instagram"):
        assert s in DEFAULT_STOPLIST


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
