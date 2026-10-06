"""Every Vertex Gemini consumer writes its real tokens to the gemini_usage ledger.

``test_gemini_usage_ledger.py`` covers the ledger helpers themselves plus the two
stages that already used them. This file covers the four consumers wired in
afterwards, whose spend the cost watchdog previously read from three other
places: ``trend_analysis``, ``daily_summary`` and ``seed_insights`` off their own
token columns, and ``reconcile`` off a hardcoded per-call estimate.

The point of each test is the same: the tokens Vertex actually billed reach the
ledger under the right consumer name, market and model, including the tokens
spent on a retry that was thrown away. A retry bills whether or not its output
ships.

Every test injects a list-appending ``usage_sink``, so no test touches BigQuery
(the BQ-RPC SDK segfaults on the Windows runner, per the sibling suites).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from src.analysis import event_ledger as el
from src.analysis.gemini_client import BriefResponse
from src.analysis.generate_briefs import generate_briefs
from src.analysis.generate_daily_summary import generate_daily_summary
from src.analysis.generate_seed_intelligence import generate_seed_intelligence
from src.utils.gemini_usage import usage_rows

DATE = date(2026, 8, 24)


def _sink():
    """A usage sink that captures every flushed row batch."""
    captured: list[dict] = []

    def sink(rows):
        captured.extend(rows)
        return len(rows)

    return captured, sink


def _by_market(rows: list[dict]) -> dict:
    return {r["market"]: r for r in rows}


# ---------------------------------------------------------------------------
# trend_analysis (src/analysis/generate_briefs.py)
# ---------------------------------------------------------------------------


def _topics_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "market": "za",
                "topic_group": "music_amapiano",
                "trend_score": 0.48,
                "item_count": 100,
                "source_diversity": 22,
                "creator_spread": 70,
                "velocity_score": 0.07,
                "tone_score": 0.6,
                "search_velocity_score": 0.18,
            },
            {
                "market": "ng",
                "topic_group": "music_afrobeats",
                "trend_score": 0.46,
                "item_count": 123,
                "source_diversity": 20,
                "creator_spread": 85,
                "velocity_score": 0.07,
                "tone_score": 0.55,
                "search_velocity_score": 0.05,
            },
        ]
    )


_SAMPLE = [
    {
        "title": f"Sample row {i} with enough text to clear the sample-thinness gate",
        "text": (
            "Amapiano keeps taking over the timeline this week and the log drum "
            "is everywhere, from Joburg rooftops to Lagos clubs, with creators "
            "posting dance edits faster than the labels can clear them. "
        )
        * 2,
        "url": f"https://example.com/{i}",
        "platform": "tiktok",
        "author_handle_norm": "kabza",
        "engagement_total": 1000,
        "published_at": datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
    }
    for i in range(6)
]


def _brief_response(parsed: dict | None = None, *, prompt=4500, completion=1800) -> BriefResponse:
    return BriefResponse(
        parsed=parsed
        or {
            "description_rationale": "Amapiano dominates SA TikTok this week",
            "activation_idea": "Lyria audio drop with Kabza",
            "visual_anchor": "Joburg rooftop, dancers mid-move, mzansi streetwear, golden hour.",
            "nano_banana_prompt": "Joburg rooftop sundowner Reel, mzansi fashion.",
            "lyria_prompt": "Amapiano log drum + shaker, 113 BPM, 20 seconds.",
            "key_metrics": ["100 mentions today", "22 sources", "Velocity 0.07"],
            "platforms": ["TikTok", "Instagram Reels"],
            "sentiment_summary": "Positive 80%, neutral 15%, negative 5%",
            "status_tag": "Key",
        },
        raw_text='{"description_rationale": "..."}',
        prompt_tokens=prompt,
        completion_tokens=completion,
        model="gemini-3.5-flash",
    )


@pytest.fixture
def _briefs_boundaries():
    """Stub every BQ-touching helper generate_briefs calls around the Gemini call."""
    with (
        patch("src.analysis.generate_briefs._query_top_topics", return_value=_topics_df()),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs._platform_counts_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs._peak_engagement_for_topic", return_value=50000.0),
        patch("src.analysis.generate_briefs._signals_digest_for_topic", return_value={}),
        patch("src.analysis.generate_briefs._prior_day_scores", return_value={}),
        patch(
            "src.analysis.generate_briefs._b24_sentiment_trajectory_for_market",
            return_value="",
        ),
        patch("src.analysis.generate_briefs._existing_brief_keys", return_value=set()),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=2),
    ):
        yield


def test_trend_analysis_records_one_ledger_row_per_market(_briefs_boundaries):
    captured, sink = _sink()
    gemini = MagicMock()
    gemini.generate_brief.return_value = _brief_response()

    report = generate_briefs(
        trend_date=DATE,
        gemini_client=gemini,
        bq_client=MagicMock(),
        dataset="trends_v2_dev",
        usage_sink=sink,
    )

    assert len(report.briefs) == 2
    rows = _by_market(captured)
    assert set(rows) == {"za", "ng"}
    assert {r["consumer"] for r in captured} == {"trend_analysis"}
    assert {r["gemini_model"] for r in captured} == {"gemini-3.5-flash"}
    # One brief per market, one call each, real token counts off the response.
    assert rows["za"]["calls"] == 1
    assert (rows["za"]["prompt_tokens"], rows["za"]["completion_tokens"]) == (4500, 1800)


def test_trend_analysis_counts_the_empty_brief_retry(_briefs_boundaries):
    """The thrown-away first call still billed, so it still lands in the ledger."""
    captured, sink = _sink()
    empty = {
        "description_rationale": "",
        "activation_idea": "",
        "key_metrics": [],
        "platforms": [],
        "sentiment_summary": "",
        "status_tag": "Rising",
    }
    gemini = MagicMock()
    # za: empty then recovered. ng: good first time.
    gemini.generate_brief.side_effect = [
        _brief_response(empty, prompt=1000, completion=50),
        _brief_response(prompt=4500, completion=1800),
        _brief_response(prompt=4500, completion=1800),
    ]

    generate_briefs(
        trend_date=DATE,
        gemini_client=gemini,
        bq_client=MagicMock(),
        dataset="trends_v2_dev",
        usage_sink=sink,
    )

    rows = _by_market(captured)
    assert rows["za"]["calls"] == 2
    assert rows["za"]["prompt_tokens"] == 5500
    assert rows["za"]["completion_tokens"] == 1850


# ---------------------------------------------------------------------------
# daily_summary (src/analysis/generate_daily_summary.py)
# ---------------------------------------------------------------------------

_SUMMARY_PARSED = {
    "summary_text": "Today across SSA, music led with Amapiano in ZA and Afrobeats in NG.",
    "through_line": "Music remains the dominant cultural signal across markets.",
    "call_to_action": "Brief creative on Amapiano activations this week.",
    "key_topics": ["za/music_amapiano"],
    "rising_topics": ["ke/politics_maandamano"],
}


def _summary_response(parsed: dict, *, prompt=2000, completion=400) -> BriefResponse:
    return BriefResponse(
        parsed=parsed,
        raw_text='{"summary_text":"x"}',
        prompt_tokens=prompt,
        completion_tokens=completion,
        model="gemini-3.5-flash",
    )


def _summary_briefs(n: int = 22) -> dict:
    return {
        (["za", "ng", "ke"][i % 3], f"topic_{i}"): {
            "status_tag": "Key" if i < 4 else "Rising",
            "description_rationale": f"Description for topic {i}",
        }
        for i in range(n)
    }


def test_daily_summary_records_one_cross_market_row():
    """daily_summary has no scalar market (its table carries a markets ARRAY),
    so its ledger row carries market NULL rather than a made-up one."""
    captured, sink = _sink()
    gemini = MagicMock()
    gemini.generate_brief.return_value = _summary_response(_SUMMARY_PARSED)

    with (
        patch(
            "src.analysis.generate_daily_summary._existing_non_empty_summary_for",
            return_value=False,
        ),
        patch("src.analysis.generate_daily_summary.insert_dataframe"),
    ):
        result = generate_daily_summary(
            trend_date=DATE,
            briefs_by_topic=_summary_briefs(),
            gemini_client=gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
            usage_sink=sink,
        )

    assert result is not None
    assert len(captured) == 1
    row = captured[0]
    assert row["consumer"] == "daily_summary"
    assert row["market"] is None
    assert (row["calls"], row["prompt_tokens"], row["completion_tokens"]) == (1, 2000, 400)


def test_daily_summary_counts_both_attempts_on_retry():
    captured, sink = _sink()
    empty = {
        "summary_text": "",
        "through_line": "",
        "call_to_action": "",
        "key_topics": [],
        "rising_topics": [],
    }
    gemini = MagicMock()
    gemini.generate_brief.side_effect = [
        _summary_response(empty, prompt=2000, completion=400),
        _summary_response(_SUMMARY_PARSED, prompt=2100, completion=450),
    ]

    with (
        patch(
            "src.analysis.generate_daily_summary._existing_non_empty_summary_for",
            return_value=False,
        ),
        patch("src.analysis.generate_daily_summary.insert_dataframe"),
    ):
        generate_daily_summary(
            trend_date=DATE,
            briefs_by_topic=_summary_briefs(),
            gemini_client=gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
            usage_sink=sink,
        )

    assert len(captured) == 1
    assert captured[0]["calls"] == 2
    assert captured[0]["prompt_tokens"] == 4100
    assert captured[0]["completion_tokens"] == 850


# ---------------------------------------------------------------------------
# seed_insights (src/analysis/generate_seed_intelligence.py)
# ---------------------------------------------------------------------------


def test_seed_insights_records_one_row_for_one_call():
    """One Gemini call, one ledger row, whatever the seed count.

    The seed_insights TABLE stamps that one call's tokens onto every seed row
    it writes, which is why a SUM over that table priced a single call once per
    seed, measured 3.0x. The ledger records the call, not the fan-out.
    """
    captured, sink = _sink()
    seeds = [
        {
            "behaviour": f"behaviour {i}",
            "the_shift": "shift",
            "evidence": ["za/music_amapiano - proof"],
            "why_hidden": "hidden",
            "timing": "now",
            "markets": ["za"],
            "brand_opportunity": "opportunity",
            "activation": {"tool": "lyria", "angle": "angle", "prompt": "prompt"},
            "signal_strength": "strong",
            "seed_score": 0.6,
        }
        for i in range(6)
    ]
    gemini = MagicMock()
    gemini.generate_brief.return_value = BriefResponse(
        parsed={"seeds": seeds},
        raw_text="{}",
        prompt_tokens=30_000,
        completion_tokens=6_000,
        model="gemini-3.5-flash",
    )

    with (
        patch("src.analysis.generate_seed_intelligence.get_client", return_value=MagicMock()),
        patch(
            "src.analysis.generate_seed_intelligence.get_dataset",
            return_value="trends_v2_dev",
        ),
        patch("src.analysis.generate_seed_intelligence._existing", return_value=False),
        patch("src.analysis.generate_seed_intelligence.insert_dataframe"),
    ):
        out = generate_seed_intelligence(
            trend_date=DATE,
            briefs_by_topic={
                ("za", f"topic_{i}"): {"description_rationale": "x"} for i in range(20)
            },
            gemini_client=gemini,
            usage_sink=sink,
        )

    assert len(out) == 6
    assert len(captured) == 1
    row = captured[0]
    assert row["consumer"] == "seed_insights"
    assert row["market"] is None
    assert (row["calls"], row["prompt_tokens"], row["completion_tokens"]) == (1, 30_000, 6_000)


# ---------------------------------------------------------------------------
# reconcile (src/analysis/event_ledger.py)
# ---------------------------------------------------------------------------


class _FakeGemini:
    def __init__(self, items: list[dict[str, Any]], model: str = "gemini-2.5-flash") -> None:
        self._items = items
        self._model = model
        self.calls = 0

    def generate_brief(self, prompt, response_schema, **kwargs):
        self.calls += 1
        return BriefResponse(
            parsed=self._items,
            raw_text="",
            prompt_tokens=35_000,
            completion_tokens=20_000,
            model=self._model,
        )


def _factual_rows():
    published = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    return [
        {
            "source": "gdelt",
            "content_type": "gdelt_gkg",
            "title": "Eskom stage 4 loadshedding announced",
            "text": "",
            "query_term": "",
            "v2persons": "",
            "v2orgs": "Eskom,12",
            "v2locations": "",
            "v2tone": "0.0",
            "search_velocity_score": 0.0,
            "published_at": published,
        },
        {
            "source": "rss",
            "content_type": None,
            "title": "Eskom stage 4 loadshedding announced for the weekend",
            "text": "",
            "query_term": "",
            "v2persons": "",
            "v2orgs": "",
            "v2locations": "",
            "v2tone": "",
            "search_velocity_score": 0.0,
            "published_at": published,
        },
    ]


def test_reconcile_records_real_tokens_not_an_estimate(monkeypatch):
    """The reconcile shadow's spend is measured off its response, per market.

    It bills on gemini-2.5-flash, a different rate from the brief model, so the
    model column is what keeps it priced correctly.
    """
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: _factual_rows())
    tally: dict = {}
    gemini = _FakeGemini([])

    el.build_event_ledger(
        DATE, "za", client=gemini, bq_client=object(), dataset="ds", usage_tally=tally
    )
    el.build_event_ledger(
        DATE, "ng", client=gemini, bq_client=object(), dataset="ds", usage_tally=tally
    )

    assert gemini.calls == 2
    rows = _by_market(usage_rows(DATE, "reconcile", tally))
    assert set(rows) == {"za", "ng"}
    assert rows["za"]["gemini_model"] == "gemini-2.5-flash"
    assert (rows["za"]["calls"], rows["za"]["prompt_tokens"]) == (1, 35_000)


# ---------------------------------------------------------------------------
# persist=False must stay a promise
# ---------------------------------------------------------------------------


def test_persist_false_writes_no_ledger_row(_briefs_boundaries):
    """A dry run bills Vertex but must not touch BigQuery, ledger included.

    ``persist=False`` is an explicit "write nothing" contract. Flushing the
    usage ledger under it reaches BigQuery on a run the caller asked to keep
    local, which on this machine also means a live network call that stalls the
    suite. The spend is still real; the billing reconciliation is what surfaces
    it, not a write that breaks the flag.
    """
    captured, sink = _sink()
    gemini = MagicMock()
    gemini.generate_brief.return_value = _brief_response()

    report = generate_briefs(
        trend_date=DATE,
        gemini_client=gemini,
        bq_client=MagicMock(),
        dataset="trends_v2_dev",
        persist=False,
        usage_sink=sink,
    )

    assert len(report.briefs) == 2
    assert gemini.generate_brief.call_count == 2
    assert captured == []


def test_daily_summary_persist_false_writes_no_ledger_row():
    captured, sink = _sink()
    gemini = MagicMock()
    gemini.generate_brief.return_value = _summary_response(_SUMMARY_PARSED)

    with patch(
        "src.analysis.generate_daily_summary._existing_non_empty_summary_for",
        return_value=False,
    ):
        generate_daily_summary(
            trend_date=DATE,
            briefs_by_topic=_summary_briefs(),
            gemini_client=gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
            persist=False,
            usage_sink=sink,
        )

    assert gemini.generate_brief.call_count == 1
    assert captured == []


def test_seed_insights_persist_false_writes_no_ledger_row():
    captured, sink = _sink()
    gemini = MagicMock()
    gemini.generate_brief.return_value = BriefResponse(
        parsed={"seeds": [{"behaviour": "b", "markets": ["za"], "seed_score": 0.6}]},
        raw_text="{}",
        prompt_tokens=30_000,
        completion_tokens=6_000,
        model="gemini-3.5-flash",
    )

    with (
        patch("src.analysis.generate_seed_intelligence.get_client", return_value=MagicMock()),
        patch(
            "src.analysis.generate_seed_intelligence.get_dataset",
            return_value="trends_v2_dev",
        ),
    ):
        generate_seed_intelligence(
            trend_date=DATE,
            briefs_by_topic={
                ("za", f"topic_{i}"): {"description_rationale": "x"} for i in range(20)
            },
            gemini_client=gemini,
            persist=False,
            usage_sink=sink,
        )

    assert gemini.generate_brief.call_count == 1
    assert captured == []
