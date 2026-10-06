"""Unit tests for the gemini_usage token ledger.

Two Phase-2 stages bill Vertex every day and persisted no table of their own, so
the cost watchdog could not see them: the comment-sentiment room-card producer
(one call per briefed topic) and the driving-hashtag producer (one call per top
tag per briefed topic). Both now fold real token counts into the shared
gemini_usage ledger.

Every test injects a list-appending ``usage_sink``, so no test touches BigQuery
(the BQ-RPC SDK segfaults on the Windows runner, per the sibling suites). The
gemini + bq boundaries are plain stubs, mirroring test_comment_sentiment.py and
test_driving_hashtags.py.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.analysis.comment_sentiment import compute_comment_sentiment
from src.scoring.driving_hashtags import compute_driving_hashtags
from src.utils.gemini_usage import (
    GEMINI_USAGE_TABLE,
    USAGE_CONSUMERS,
    persist_gemini_usage,
    record_usage,
    usage_rows,
)

DATE = datetime.date(2026, 8, 19)

_READ = {
    "comment_sentiment": "love the sound, resent the gatekeeping",
    "comment_themes": ["ticket prices", "lineup snubs"],
}


# --- stubs -----------------------------------------------------------------


def _resp(parsed, prompt=1200, completion=300, model="gemini-3.5-flash"):
    """A BriefResponse-shaped stub carrying usage metadata."""
    return SimpleNamespace(
        parsed=parsed,
        prompt_tokens=prompt,
        completion_tokens=completion,
        model=model,
    )


class _Gemini:
    """Returns the same parsed payload + token counts on every call."""

    def __init__(self, parsed, prompt=1200, completion=300, model="gemini-3.5-flash"):
        self._args = (parsed, prompt, completion, model)
        self.calls = 0

    def generate_brief(self, *_a, **_k):
        self.calls += 1
        return _resp(*self._args)


class _NoUsageGemini:
    """A model response with NO usage metadata at all (the stub/failure shape)."""

    def generate_brief(self, *_a, **_k):
        return SimpleNamespace(parsed=dict(_READ))


class _SentimentBq:
    project = "ogilvy-trends-v2"

    def __init__(self, bodies):
        self._bodies = bodies

    def query(self, _sql, **_k):
        rows = [
            {"content_type": "reddit_comment", "text": b, "engagement_total": 1}
            for b in self._bodies
        ]
        return SimpleNamespace(result=lambda: rows)


class _HashtagBq:
    project = "ogilvy-trends-v2"

    def __init__(self, tags, comments):
        self._tags = tags
        self._comments = comments

    def query(self, _sql, *, job_config):
        params = {p.name: p.value for p in job_config.query_parameters}
        if params.get("tag_regex") is None:
            rows = list(self._tags)
        else:
            rows = [{"text": c} for c in self._comments]
        return SimpleNamespace(result=lambda: rows)


def _sink():
    """A usage sink that captures every flushed row batch."""
    captured: list[dict] = []

    def sink(rows):
        captured.extend(rows)
        return len(rows)

    return captured, sink


# --- the canonical consumer list ------------------------------------------


def test_usage_consumers_covers_every_vertex_gemini_caller():
    """Every stage that fires a Vertex Gemini call must name itself here.

    The watchdog imports this same tuple, so a consumer added to one side and
    forgotten on the other is the exact failure the ledger exists to prevent:
    a stage that bills and is never counted.
    """
    assert set(USAGE_CONSUMERS) == {
        "trend_analysis",
        "daily_summary",
        "seed_insights",
        "comment_sentiment",
        "driving_hashtags",
        "reconcile",
        "dynamic_signal_summary",
        "open_question_answer",
    }


# --- record_usage (pure) ---------------------------------------------------


def test_record_usage_counts_a_real_call():
    tally: dict = {}
    assert record_usage(tally, "za", _resp({}, prompt=1000, completion=250)) is True
    assert record_usage(tally, "za", _resp({}, prompt=500, completion=100)) is True
    assert tally == {
        ("za", "gemini-3.5-flash"): {
            "calls": 2,
            "prompt_tokens": 1500,
            "completion_tokens": 350,
        }
    }


def test_record_usage_splits_by_market_and_model():
    tally: dict = {}
    record_usage(tally, "za", _resp({}, model="gemini-3.5-flash"))
    record_usage(tally, "ng", _resp({}, model="gemini-3.5-flash"))
    record_usage(tally, "za", _resp({}, model="gemini-2.5-flash"))
    assert len(tally) == 3
    assert tally[("za", "gemini-2.5-flash")]["calls"] == 1


def test_record_usage_rejects_zero_token_response():
    # A failed or empty model response carries no usage metadata. Counting it
    # would write a bogus zero-token row that inflates the calls column the
    # watchdog reads while adding no spend.
    tally: dict = {}
    assert record_usage(tally, "za", SimpleNamespace(parsed={})) is False
    assert record_usage(tally, "za", _resp({}, prompt=0, completion=0)) is False
    assert record_usage(tally, "za", _resp({}, prompt=None, completion=None)) is False
    assert tally == {}


# --- usage_rows (pure) ----------------------------------------------------


def test_usage_rows_shape_and_aggregation():
    tally: dict = {}
    for _ in range(3):
        record_usage(tally, "za", _resp({}, prompt=100, completion=10))
    record_usage(tally, "ng", _resp({}, prompt=70, completion=7))
    rows = usage_rows(DATE, "comment_sentiment", tally)
    assert [r["market"] for r in rows] == ["ng", "za"]
    za = next(r for r in rows if r["market"] == "za")
    assert za["consumer"] == "comment_sentiment"
    assert za["trend_date"] == DATE
    assert za["gemini_model"] == "gemini-3.5-flash"
    assert (za["calls"], za["prompt_tokens"], za["completion_tokens"]) == (3, 300, 30)
    assert isinstance(za["recorded_at"], datetime.datetime)
    assert za["usage_id"] != next(r for r in rows if r["market"] == "ng")["usage_id"]


def test_usage_rows_empty_tally_is_no_rows():
    assert usage_rows(DATE, "driving_hashtags", {}) == []


# --- persist_gemini_usage -------------------------------------------------


def test_persist_empty_rows_never_touches_bigquery(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("insert_dataframe must not be called for an empty batch")

    monkeypatch.setattr("src.utils.bigquery.insert_dataframe", boom)
    assert persist_gemini_usage([]) == 0


def test_persist_writes_to_the_ledger_table(monkeypatch):
    seen: dict = {}

    def fake_insert(df, table_name, **_k):
        seen["table"] = table_name
        seen["rows"] = df.to_dict("records")
        return len(df)

    monkeypatch.setattr("src.utils.bigquery.insert_dataframe", fake_insert)
    tally: dict = {}
    record_usage(tally, "ke", _resp({}, prompt=900, completion=120))
    assert persist_gemini_usage(usage_rows(DATE, "comment_sentiment", tally)) == 1
    assert seen["table"] == GEMINI_USAGE_TABLE
    assert seen["rows"][0]["prompt_tokens"] == 900


def test_persist_is_non_fatal_on_write_failure(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("403 table not found")

    monkeypatch.setattr("src.utils.bigquery.insert_dataframe", boom)
    tally: dict = {}
    record_usage(tally, "za", _resp({}))
    # Cost accounting must never break the stage whose output already shipped.
    assert persist_gemini_usage(usage_rows(DATE, "comment_sentiment", tally)) == 0


# --- comment_sentiment producer ------------------------------------------


def test_comment_sentiment_records_usage_on_success():
    captured, sink = _sink()
    gemini = _Gemini(dict(_READ), prompt=2000, completion=400)
    out = compute_comment_sentiment(
        DATE,
        {("za", "music_amapiano"): {}, ("ng", "music_afrobeats"): {}},
        gemini_client=gemini,
        bq_client=_SentimentBq([f"the room is talking {i}" for i in range(8)]),
        dataset="trends_v2_dev",
        usage_sink=sink,
    )
    assert len(out) == 2
    assert gemini.calls == 2
    # One row per market, each carrying that market's real token counts.
    assert {r["market"] for r in captured} == {"za", "ng"}
    assert all(r["consumer"] == "comment_sentiment" for r in captured)
    assert sum(r["calls"] for r in captured) == 2
    assert sum(r["prompt_tokens"] for r in captured) == 4000
    assert sum(r["completion_tokens"] for r in captured) == 800


def test_comment_sentiment_failed_call_writes_no_row():
    captured, sink = _sink()

    class _Raising:
        def generate_brief(self, *_a, **_k):
            raise RuntimeError("transient 503")

    out = compute_comment_sentiment(
        DATE,
        {("za", "music_amapiano"): {}},
        gemini_client=_Raising(),
        bq_client=_SentimentBq([f"body {i}" for i in range(8)]),
        dataset="trends_v2_dev",
        usage_sink=sink,
    )
    assert out == {}
    assert captured == []


def test_comment_sentiment_response_without_usage_writes_no_row():
    # The model answered, so the card renders, but the SDK returned no usage
    # metadata. A zero-token row would be a bogus call count, so none is written.
    captured, sink = _sink()
    out = compute_comment_sentiment(
        DATE,
        {("za", "music_amapiano"): {}},
        gemini_client=_NoUsageGemini(),
        bq_client=_SentimentBq([f"body {i}" for i in range(8)]),
        dataset="trends_v2_dev",
        usage_sink=sink,
    )
    assert ("za", "music_amapiano") in out
    assert captured == []


def test_comment_sentiment_flushes_usage_even_when_the_pass_dies():
    # The tokens already left the account, so a pass that dies mid-way must
    # still record what it spent. KeyboardInterrupt is not an Exception, so it
    # bypasses both non-fatal guards and exercises the finally-flush.
    captured, sink = _sink()

    class _DiesOnSecondCall:
        def __init__(self):
            self.calls = 0

        def generate_brief(self, *_a, **_k):
            self.calls += 1
            if self.calls > 1:
                raise KeyboardInterrupt("job cancelled")
            return _resp(dict(_READ), prompt=1500, completion=250)

    with pytest.raises(KeyboardInterrupt):
        compute_comment_sentiment(
            DATE,
            {("za", "music_amapiano"): {}, ("ng", "music_afrobeats"): {}},
            gemini_client=_DiesOnSecondCall(),
            bq_client=_SentimentBq([f"body {i}" for i in range(8)]),
            dataset="trends_v2_dev",
            usage_sink=sink,
        )
    assert [r["market"] for r in captured] == ["za"]
    assert captured[0]["prompt_tokens"] == 1500


# --- driving_hashtags producer -------------------------------------------


def test_driving_hashtags_records_one_row_per_market_summing_every_tag_call():
    captured, sink = _sink()
    gemini = _Gemini({"mood": "practical and supportive"}, prompt=800, completion=40)
    tags = [
        {"tag": "#stokvel", "posts": 40, "engagement": 90},
        {"tag": "#amapiano", "posts": 30, "engagement": 80},
    ]
    out = compute_driving_hashtags(
        DATE,
        {("za", "finance_stokvel"): {}},
        gemini_client=gemini,
        bq_client=_HashtagBq(tags, ["a post using the tag"]),
        dataset="trends_v2_dev",
        usage_sink=sink,
    )
    assert ("za", "finance_stokvel") in out
    # Per-tag stage: two tags means two Gemini calls folded into ONE ledger row.
    assert gemini.calls == 2
    assert len(captured) == 1
    row = captured[0]
    assert row["consumer"] == "driving_hashtags"
    assert (row["market"], row["calls"]) == ("za", 2)
    assert (row["prompt_tokens"], row["completion_tokens"]) == (1600, 80)


def test_driving_hashtags_failed_call_writes_no_row():
    captured, sink = _sink()

    class _Raising:
        def generate_brief(self, *_a, **_k):
            raise RuntimeError("transient 503")

    out = compute_driving_hashtags(
        DATE,
        {("za", "finance_stokvel"): {}},
        gemini_client=_Raising(),
        bq_client=_HashtagBq([{"tag": "#stokvel", "posts": 40, "engagement": 90}], ["post"]),
        dataset="trends_v2_dev",
        usage_sink=sink,
    )
    assert out == {}
    assert captured == []


def test_driving_hashtags_no_tags_means_no_calls_and_no_row():
    captured, sink = _sink()
    gemini = _Gemini({"mood": "ok"})
    out = compute_driving_hashtags(
        DATE,
        {("za", "finance_stokvel"): {}},
        gemini_client=gemini,
        bq_client=_HashtagBq([], []),
        dataset="trends_v2_dev",
        usage_sink=sink,
    )
    assert out == {}
    assert gemini.calls == 0
    assert captured == []
