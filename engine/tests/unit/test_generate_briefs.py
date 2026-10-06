"""Unit tests for src/analysis/generate_briefs.py.

Mocks every external boundary (BigQuery client, GeminiClient,
insert_dataframe) so no live API call is made and no spend incurred.
Verifies orchestration logic: topic queue, sample/creator pull,
prompt assembly call, response shaping, BQ persistence, failure
isolation.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from src.analysis.gemini_client import BriefResponse
from src.analysis.generate_briefs import (
    GenerateBriefsReport,
    TopicBrief,
    _b24_sentiment_trajectory_for_market,
    _clamp_status_tag,
    _is_empty_brief,
    _to_topic_brief,
    generate_briefs,
)

# --- _clamp_status_tag (score-authoritative Key/Rising pill) ---


def test_clamp_status_tag_key_at_or_above_threshold():
    with patch(
        "src.analysis.generate_briefs.load_scoring",
        return_value={"thresholds": {"trending": 0.40}},
    ):
        assert _clamp_status_tag(0.40) == "Key"
        assert _clamp_status_tag(0.55) == "Key"


def test_clamp_status_tag_rising_below_threshold():
    with patch(
        "src.analysis.generate_briefs.load_scoring",
        return_value={"thresholds": {"trending": 0.40}},
    ):
        assert _clamp_status_tag(0.39) == "Rising"
        assert _clamp_status_tag(0.0) == "Rising"


def test_clamp_status_tag_falls_back_to_default_on_config_error():
    with patch(
        "src.analysis.generate_briefs.load_scoring",
        side_effect=RuntimeError("no config"),
    ):
        assert _clamp_status_tag(0.45) == "Key"  # fallback floor 0.45
        assert _clamp_status_tag(0.44) == "Rising"


def test_clamp_status_tag_uses_live_scoring_threshold():
    """No patch: exercises the real configs/scoring.yaml threshold (0.45) end to
    end, so this fails loudly if the Trending floor is retuned without review."""
    assert _clamp_status_tag(0.45) == "Key"
    assert _clamp_status_tag(0.44) == "Rising"


def test_to_topic_brief_promotes_to_key_over_model_rising():
    """A model that under-labels a trending-tier topic is corrected: the score,
    not the model's self-reported status_tag, decides the pill."""
    resp = _mock_brief_response({"status_tag": "Rising"})
    with patch(
        "src.analysis.generate_briefs.load_scoring",
        return_value={"thresholds": {"trending": 0.45}},
    ):
        brief = _to_topic_brief("za", "music_amapiano", 0.50, resp)
    assert brief.status_tag == "Key"


def test_to_topic_brief_demotes_to_rising_over_model_key():
    """A model that over-labels a below-floor topic is corrected to Rising."""
    resp = _mock_brief_response({"status_tag": "Key"})
    with patch(
        "src.analysis.generate_briefs.load_scoring",
        return_value={"thresholds": {"trending": 0.45}},
    ):
        brief = _to_topic_brief("ng", "film_nollywood", 0.20, resp)
    assert brief.status_tag == "Rising"


@pytest.fixture(autouse=True)
def _stub_existing_brief_keys():
    """Default: pretend trend_analysis is empty so tests exercise the live
    Vertex path. Tests that need to assert idempotency override this patch
    inside their own context.
    """
    with patch(
        "src.analysis.generate_briefs._existing_brief_keys",
        return_value=set(),
    ):
        yield


def _topics_df(rows: list[dict] | None = None) -> pd.DataFrame:
    rows = rows or [
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
    return pd.DataFrame(rows)


# Sample fixture that PASSES the sample-thinness gate added 25 May 2026
# in generate_briefs._sample_is_too_thin. The gate requires:
#   - At least 5 surviving rows
#   - At least 2000 total chars of title + text across rows
#
# 6 rows of ~360 chars each = 2160 chars, comfortably over both
# thresholds. Existing tests use this fixture wherever they mock
# _sample_rows_for_topic and need the brief generation step to proceed.
# Tests that specifically exercise the thin-sample skip path override
# locally with a smaller sample.
_FAT_SAMPLE: list[dict] = [
    {
        "title": "Sample row one with enough text to pass the density gate cleanly",
        "text": (
            "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
            "Real social-post body content goes here, long enough that "
            "the sample-thinness check sees adequate substance to brief. "
            "Repeated across six rows hits the 2000 char threshold. "
            "Additional sentences extend each body so the cumulative "
            "title plus text length lands safely above the threshold "
            "with comfortable margin for production-realism in tests."
        ),
        "platform": "tiktok",
        "url": "https://example.com/1",
    },
    {
        "title": "Sample row two with enough text to pass the density gate cleanly",
        "text": (
            "Second row content body, similar length, talking about the "
            "same trend, different creator. Brief writer needs at least "
            "this much material to anchor a useful description in the "
            "actual sample rather than fabricate. Production samples "
            "typically run 200 to 500 characters per row across 10 to "
            "25 rows; this 6-row fixture aims for the middle of that band."
        ),
        "platform": "instagram",
        "url": "https://example.com/2",
    },
    {
        "title": "Sample row three with enough text to pass the density gate cleanly",
        "text": (
            "Third row content, news article snippet about the topic. "
            "News provides context that social posts often lack. Mixing "
            "platform sources is how the brief writer gets cultural "
            "texture beyond what one platform alone shows. Long bodies "
            "across the fixture ensure the 2000-char floor is cleared."
        ),
        "platform": "news",
        "url": "https://example.com/3",
    },
    {
        "title": "Sample row four with enough text to pass the density gate cleanly",
        "text": (
            "Fourth row content, more user-generated. Pads the sample to "
            "the 5-row minimum that the thin-sample gate enforces. With "
            "fewer than 5 rows the gate skips the topic entirely without "
            "calling Vertex. Total combined title plus text across all "
            "six rows sits comfortably above the 2000 character floor."
        ),
        "platform": "threads",
        "url": "https://example.com/4",
    },
    {
        "title": "Sample row five with enough text to pass the density gate cleanly",
        "text": (
            "Fifth row, the count threshold cleared. Total text length "
            "still needs to exceed 2000 chars; this row plus row six "
            "should comfortably do that with their combined bodies. "
            "Extra clauses pad each row so the fixture has margin to "
            "spare even if the thresholds tighten slightly later."
        ),
        "platform": "tiktok",
        "url": "https://example.com/5",
    },
    {
        "title": "Sample row six with enough text to pass the density gate cleanly",
        "text": (
            "Sixth row, padding to ensure the 2000-char floor is cleared "
            "with margin. Real production samples typically run 10-25 "
            "rows with 200-500 chars each, so a 6-row fixture sits "
            "below typical production volume but above the gate floor. "
            "Six rows times approximately 400 chars each comes to 2400 "
            "characters of body content, plus six titles for buffer."
        ),
        "platform": "instagram",
        "url": "https://example.com/6",
    },
]


def _mock_brief_response(parsed: dict | None = None) -> BriefResponse:
    return BriefResponse(
        parsed=parsed
        or {
            "description_rationale": "Amapiano dominates SA TikTok this week",
            "activation_idea": "Lyria audio drop with Kabza",
            "visual_anchor": "Joburg rooftop, dancers mid-move, mzansi streetwear, golden hour.",
            "nano_banana_prompt": (
                "Joburg rooftop sundowner Reel, mzansi fashion, 'Created with Gemini' tag."
            ),
            "lyria_prompt": "Amapiano log drum + shaker, 113 BPM, 20 seconds.",
            "key_metrics": ["100 mentions today", "22 sources", "Velocity 0.07"],
            "platforms": ["TikTok", "Instagram Reels", "YouTube Shorts"],
            "sentiment_summary": "Positive 80%, neutral 15%, negative 5%",
            "status_tag": "Key",
        },
        raw_text='{"description_rationale": "..."}',
        prompt_tokens=4500,
        completion_tokens=1800,
        model="gemini-2.5-flash",
    )


def test_derive_risk_flags_keeps_model_findings():
    from src.analysis.generate_briefs import _derive_risk_flags

    out = _derive_risk_flags(["competitor overlap, check before live"], "music_amapiano", "")
    assert out == ["competitor overlap, check before live"]


def test_derive_risk_flags_backstops_political_topic():
    # The model returned [] for politics_maandamano on 30 May; the backstop must
    # supply a caution so the per-Kit risk line is not blank on a crisis topic.
    from src.analysis.generate_briefs import _derive_risk_flags

    out = _derive_risk_flags([], "politics_maandamano", "protests over the finance bill")
    assert out
    # maandamano marker note plus the political-category note both surface.
    joined = " ".join(out).lower()
    assert "maandamano" in joined
    assert "political content" in joined


def test_derive_risk_flags_backstops_on_text_marker():
    from src.analysis.generate_briefs import _derive_risk_flags

    out = _derive_risk_flags([], "politics_crises", "xenophobia and anti-immigrant tension")
    assert any("reputational risk" in f.lower() for f in out)


def test_derive_risk_flags_empty_for_safe_topic():
    from src.analysis.generate_briefs import _derive_risk_flags

    out = _derive_risk_flags([], "food_jollof", "party jollof recipe with a perfect bottompot")
    assert out == []


def test_derive_risk_flags_short_markers_are_word_bounded():
    # 'died' as a substring of 'studied' (and 'embodied', 'parodied') must not
    # trip the death marker; bare substring matching stamped a false flag onto
    # innocuous briefs and shipped it to stakeholders.
    from src.analysis.generate_briefs import _derive_risk_flags

    out = _derive_risk_flags(
        [], "music_amapiano", "fans studied the new release and embodied the vibe"
    )
    assert out == []


def test_derive_risk_flags_short_markers_fire_on_whole_word():
    # The same markers still fire when they appear as standalone words.
    from src.analysis.generate_briefs import _derive_risk_flags

    out = _derive_risk_flags([], "music_amapiano", "the artist died last night")
    assert len(out) == 1
    assert "tragedy" in out[0].lower()


def test_derive_risk_flags_unions_weak_model_flag_with_backstop():
    # A weak single-note model flag on a sensitive topic must NOT suppress the
    # backstop. The model flag is kept and the backstop note is added on top.
    from src.analysis.generate_briefs import _derive_risk_flags

    out = _derive_risk_flags(
        ["minor note"], "politics_maandamano", "protests over the finance bill"
    )
    assert "minor note" in out  # model flag preserved
    joined = " ".join(out).lower()
    assert "maandamano" in joined  # backstop note added (union)
    assert len(out) > 1


def test_derive_risk_flags_negative_tone_on_sensitive_topic_adds_note():
    # Strongly-negative tone on a sensitive-prefix topic adds a caution even
    # when no keyword marker fired and the model returned nothing.
    from src.analysis.generate_briefs import _derive_risk_flags

    out = _derive_risk_flags([], "protest_action", "rally downtown today", tone_avg=-4.0)
    assert any("vet before activating" in f.lower() for f in out)


def test_derive_risk_flags_negative_tone_ignored_on_neutral_topic():
    # The tone check is scoped to sensitive prefixes; a non-sensitive topic with
    # a negative tone reading does not get a caution from the tone path alone.
    from src.analysis.generate_briefs import _derive_risk_flags

    out = _derive_risk_flags([], "music_amapiano", "fans love the new track", tone_avg=-6.0)
    assert out == []


def test_to_topic_brief_maps_response_fields():
    """_to_topic_brief copies every Gemini field into the TopicBrief."""
    response = _mock_brief_response()
    brief = _to_topic_brief("za", "music_amapiano", 0.48, response)

    assert isinstance(brief, TopicBrief)
    assert brief.market == "za"
    assert brief.topic_group == "music_amapiano"
    assert brief.trend_score == pytest.approx(0.48)
    assert brief.description_rationale.startswith("Amapiano")
    assert brief.activation_idea == "Lyria audio drop with Kabza"
    assert brief.key_metrics == [
        "100 mentions today",
        "22 sources",
        "Velocity 0.07",
    ]
    assert brief.platforms[:3] == ["TikTok", "Instagram Reels", "YouTube Shorts"]
    assert brief.status_tag == "Key"
    assert brief.prompt_tokens == 4500
    assert brief.completion_tokens == 1800


def test_to_topic_brief_truncates_oversized_lists():
    """key_metrics caps at 3, platforms caps at 4."""
    response = _mock_brief_response(
        parsed={
            "description_rationale": "x",
            "activation_idea": "y",
            "key_metrics": ["a", "b", "c", "d", "e"],
            "platforms": ["TikTok", "Instagram", "YouTube", "Threads", "Twitter", "Facebook"],
            "sentiment_summary": "ok",
            "status_tag": "Rising",
        }
    )
    brief = _to_topic_brief("za", "x", 0.4, response)
    assert len(brief.key_metrics) == 3
    assert len(brief.platforms) == 4


def test_topic_brief_to_bq_row_carries_all_fields():
    """to_bq_row produces a dict ready for trend_analysis insert."""
    brief = TopicBrief(
        market="ng",
        topic_group="music_afrobeats",
        trend_score=0.46,
        description_rationale="afrobeats narrative",
        activation_idea="Nano Banana Reel",
        visual_anchor="Lagos street market, afrobeats group dance, kente accents.",
        nano_banana_prompt="Lagos-street afrobeats Reel, Created with Gemini tag.",
        lyria_prompt="Afrobeats with shaker, 105 BPM, 20 sec.",
        key_metrics=["123 mentions", "20 sources", "vel 0.07"],
        platforms=["TikTok", "Instagram"],
        sentiment_summary="positive 82%",
        status_tag="Key",
        top_creators=["@burnaboy | TikTok | 14 mentions"],
        social_refs=["https://tiktok.com/@x/video/9 | TikTok | Burna Boy clip"],
        platform_counts=["tiktok | 78 items", "youtube | 24 items"],
        prompt_tokens=5000,
        completion_tokens=2000,
        model="gemini-2.5-flash",
    )
    row = brief.to_bq_row(date(2026, 5, 5), cycle_id="cycle-1")
    assert row["market"] == "ng"
    assert row["query_group"] == "music_afrobeats"
    assert row["trend_synthesis"] == "afrobeats narrative"
    assert row["cultural_context"] == "Nano Banana Reel"
    assert row["campaign_angles"] == ["123 mentions", "20 sources", "vel 0.07"]
    assert row["platforms"] == ["TikTok", "Instagram"]
    assert row["sentiment_summary"] == "positive 82%"
    assert row["status_tag"] == "Key"
    assert row["nano_banana_prompt"].startswith("Lagos-street")
    assert row["lyria_prompt"].startswith("Afrobeats with shaker")
    assert row["top_creators"] == ["@burnaboy | TikTok | 14 mentions"]
    assert row["social_refs"][0].startswith("https://tiktok.com")
    assert row["platform_counts"] == ["tiktok | 78 items", "youtube | 24 items"]
    assert row["gemini_model"] == "gemini-2.5-flash"
    assert row["cycle_id"] == "cycle-1"
    assert "analysis_id" in row
    assert "analyzed_at" in row


def test_generate_briefs_orchestrates_per_topic_call():
    """generate_briefs iterates topics, calls Gemini per topic, persists.

    The five data-only helpers (_platform_counts_for_topic,
    _peak_engagement_for_topic, _signals_digest_for_topic, _prior_day_scores,
    _b24_sentiment_trajectory_for_market) run against the bq client. Left
    unmocked against a bare MagicMock they each silently degrade to empty /
    zero, so the wiring of peak reach, platform_counts, creators, and
    social_refs into the persisted row was never asserted. Patch all five
    with realistic returns and pin that they reach the brief and the row.
    """
    fake_bq = MagicMock()
    fake_gemini = MagicMock()
    fake_gemini.generate_brief.return_value = _mock_brief_response()

    creators = [
        {"author_handle_norm": "kabza", "platform": "tiktok", "mentions": 14},
    ]
    platform_count_rows = [
        {"platform": "tiktok", "count": 78},
        {"platform": "instagram", "count": 24},
    ]

    with (
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(),
        ),
        patch(
            "src.analysis.generate_briefs._sample_rows_for_topic",
            return_value=_FAT_SAMPLE,
        ),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=creators),
        patch(
            "src.analysis.generate_briefs._platform_counts_for_topic",
            return_value=platform_count_rows,
        ),
        patch(
            "src.analysis.generate_briefs._peak_engagement_for_topic",
            return_value=50000.0,
        ),
        patch(
            "src.analysis.generate_briefs._signals_digest_for_topic",
            return_value={
                "persons": ["Kabza De Small"],
                "orgs": [],
                "slang": ["amapiano"],
                "genz_score": 0.42,
            },
        ),
        patch(
            "src.analysis.generate_briefs._prior_day_scores",
            return_value={("za", "music_amapiano"): 0.40},
        ),
        patch(
            "src.analysis.generate_briefs._b24_sentiment_trajectory_for_market",
            return_value="improving (+12pp positive share)",
        ),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=2) as mock_insert,
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=fake_bq,
            dataset="trends_v2_dev",
        )

    assert isinstance(report, GenerateBriefsReport)
    assert len(report.briefs) == 2
    assert ("za", "music_amapiano") in report.briefs
    assert ("ng", "music_afrobeats") in report.briefs
    assert report.failures == []
    assert report.total_prompt_tokens == 9000  # 4500 * 2
    assert report.total_completion_tokens == 3600  # 1800 * 2
    assert fake_gemini.generate_brief.call_count == 2
    mock_insert.assert_called_once()

    # The derived helpers must reach the brief, not silently degrade to empty.
    brief = report.briefs[("za", "music_amapiano")]
    assert brief.platform_counts == ["tiktok | 78 items", "instagram | 24 items"]
    assert brief.top_creators == ["@kabza | tiktok | 14 mentions"]
    assert brief.social_refs  # social_refs derive from the sample rows
    assert brief.b24_sentiment_trajectory == "improving (+12pp positive share)"
    # The true topic peak (50k) flows into the deterministic key_metrics, which
    # persist as campaign_angles in the trend_analysis row.
    assert any("Peak post engagement 50k" in m for m in brief.key_metrics)
    row = brief.to_bq_row(date(2026, 5, 5))
    assert row["platform_counts"] == ["tiktok | 78 items", "instagram | 24 items"]
    assert row["top_creators"] == ["@kabza | tiktok | 14 mentions"]
    assert row["social_refs"] == brief.social_refs
    assert any("Peak post engagement 50k" in m for m in row["campaign_angles"])


def test_generate_briefs_isolates_per_topic_failure():
    """One topic crashing does not stop the others.

    Patches the module logger to a no-op MagicMock so the warning path
    does not trip a known Python 3.13.13 Windows native crash in the
    stdlib logging module that surfaces during pytest captures.
    Functional behaviour (failure recorded in report) is what we assert.
    """
    fake_gemini = MagicMock()
    # generate_briefs now retries once on transient Vertex exceptions (added
    # 21 May 2026 to recover the Monitoring-tier topics lost when Vertex
    # blipped on 18 May / 21 May). The test gives the retry an exception
    # too so the topic ultimately lands in report.failures as before. Net
    # call_count for the failing topic is 2 (initial + retry), so we mock
    # three side_effects total: one success + two transients.
    fake_gemini.generate_brief.side_effect = [
        _mock_brief_response(),
        RuntimeError("Vertex transient"),
        RuntimeError("Vertex transient"),
    ]
    # Patch the time.sleep so the 5 second retry backoff does not slow the
    # test suite. Function patched at module-level so generate_briefs.time
    # .sleep is a no-op during this test.

    with (
        patch("src.analysis.generate_briefs.logger", new=MagicMock()),
        patch("src.analysis.generate_briefs.time.sleep", new=MagicMock()),
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(),
        ),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=1),
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    assert len(report.briefs) == 1
    assert len(report.failures) == 1
    failed_market, failed_topic, failed_msg = report.failures[0]
    assert failed_market == "ng"
    assert failed_topic == "music_afrobeats"
    assert "Vertex transient" in failed_msg
    # Failed topic should have triggered exactly one retry (initial + 1), so
    # generate_brief is called 3 times total: 1 for the successful za topic,
    # 2 for the failing ng topic (initial + retry, both raise).
    assert fake_gemini.generate_brief.call_count == 3


def test_generate_briefs_recovers_on_transient_retry():
    """The transient-retry recovery path: first attempt raises, the retry
    succeeds, so the brief lands and nothing is recorded as a failure.

    The both-attempts-fail case is covered above; this pins the other branch
    so a regression that drops the retry (or stops re-attempting) is caught.
    """
    fake_gemini = MagicMock()
    fake_gemini.generate_brief.side_effect = [
        RuntimeError("Vertex transient"),
        _mock_brief_response(),
    ]
    single_topic = _topics_df(
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
            }
        ]
    )

    with (
        patch("src.analysis.generate_briefs.logger", new=MagicMock()),
        patch("src.analysis.generate_briefs.time.sleep", new=MagicMock()),
        patch("src.analysis.generate_briefs._query_top_topics", return_value=single_topic),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs._platform_counts_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs._peak_engagement_for_topic", return_value=0.0),
        patch(
            "src.analysis.generate_briefs._signals_digest_for_topic",
            return_value={"persons": [], "orgs": [], "slang": [], "genz_score": 0.0},
        ),
        patch("src.analysis.generate_briefs._prior_day_scores", return_value={}),
        patch(
            "src.analysis.generate_briefs._b24_sentiment_trajectory_for_market",
            return_value="",
        ),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=1),
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    # Initial attempt raised, the retry returned a real brief: 2 calls, the
    # brief lands, and no failure is recorded.
    assert fake_gemini.generate_brief.call_count == 2
    assert ("za", "music_amapiano") in report.briefs
    assert report.failures == []


def _empty_brief_response() -> BriefResponse:
    """Vertex returned empty parsed dict but raw_text is JSON-shaped.

    Simulates today's recoverable failure mode where the SDK's parser
    rejected the schema validation but the raw text is itself parseable
    JSON (or at least started with '{'). The retry guard treats this as
    worth retrying because a stricter prompt may produce a clean payload.
    """
    return BriefResponse(
        parsed={},
        raw_text='{"description_rationale": ""}',
        prompt_tokens=1700,
        completion_tokens=300,
        model="gemini-2.5-flash",
    )


def _topic_brief(
    *,
    description_rationale: str = "real text",
    activation_idea: str = "real idea",
    key_metrics: list[str] | None = None,
    visual_anchor: str = "anchor",
    nano_banana_prompt: str = "nano prompt",
    lyria_prompt: str = "lyria prompt",
) -> TopicBrief:
    """Helper: build a TopicBrief with optional narrative fields filled in.

    Brief v2 kit fields default filled so _is_empty_brief checks narrative + kit.
    """
    return TopicBrief(
        market="za",
        topic_group="x",
        trend_score=0.4,
        description_rationale=description_rationale,
        activation_idea=activation_idea,
        visual_anchor=visual_anchor,
        nano_banana_prompt=nano_banana_prompt,
        lyria_prompt=lyria_prompt,
        key_metrics=key_metrics if key_metrics is not None else ["a"],
        platforms=[],
        sentiment_summary="",
        status_tag="Rising",
        top_creators=[],
        social_refs=[],
        platform_counts=[],
        prompt_tokens=0,
        completion_tokens=0,
        model="gemini-2.5-flash",
    )


def test_is_empty_brief_flags_missing_narrative_fields():
    """Brief is empty when description, activation, or key_metrics blank."""
    assert _is_empty_brief(_topic_brief()) is False
    assert _is_empty_brief(_topic_brief(description_rationale="")) is True
    assert _is_empty_brief(_topic_brief(description_rationale="   ")) is True
    assert _is_empty_brief(_topic_brief(activation_idea="")) is True
    assert _is_empty_brief(_topic_brief(key_metrics=[])) is True
    assert _is_empty_brief(_topic_brief(visual_anchor="")) is True
    assert _is_empty_brief(_topic_brief(nano_banana_prompt="")) is True
    assert _is_empty_brief(_topic_brief(lyria_prompt="")) is True


def test_format_creators_drops_unrenderable_handles():
    from src.analysis.generate_briefs import _format_creators_for_brief

    creators = [
        {"author_handle_norm": "realcreator", "platform": "tiktok", "mentions": 3},
        {"author_handle_norm": "uc" + "a" * 22, "platform": "youtube", "mentions": 2},
        {"author_handle_norm": "123456789", "platform": "instagram", "mentions": 1},
    ]
    out = _format_creators_for_brief(creators)
    assert len(out) == 1
    assert out[0].startswith("@realcreator")


def test_generate_briefs_retries_empty_brief_then_persists_recovered():
    """Empty first response triggers a retry; recovered brief persists.

    Models the recoverable failure mode: Vertex returned an empty parsed
    payload on the first attempt, but the retry with stricter prompt
    produces a valid brief. The valid brief should land in BQ; the call
    count should be exactly 2 for that one topic.
    """
    fake_gemini = MagicMock()
    fake_gemini.generate_brief.side_effect = [
        _empty_brief_response(),
        _mock_brief_response(),
    ]

    one_topic = [
        {
            "market": "za",
            "topic_group": "music_amapiano",
            "trend_score": 0.48,
            "item_count": 100,
            "source_diversity": 22,
            "creator_spread": 70,
            "velocity_score": 0.07,
            "tone_score": 0.6,
        }
    ]

    with (
        patch("src.analysis.generate_briefs.logger", new=MagicMock()),
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(rows=one_topic),
        ),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=1) as mock_insert,
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    assert fake_gemini.generate_brief.call_count == 2  # first + retry
    assert len(report.briefs) == 1
    assert ("za", "music_amapiano") in report.briefs
    assert report.skipped_empty == []
    # Tokens from BOTH attempts roll into spend (we paid for both calls).
    assert report.total_prompt_tokens == 1700 + 4500
    assert report.total_completion_tokens == 300 + 1800
    mock_insert.assert_called_once()


def test_generate_briefs_skips_retry_when_raw_text_is_prose_refusal():
    """Cost-leak guard: empty parsed + prose raw_text means safety filter
    refused, so retry is skipped and spend is bounded to one call. Empty
    raw_text DOES retry (see test_generate_briefs_retries_when_raw_text_empty
    below); the prose case is the only stop-signal because it indicates a
    deterministic refusal that a second call will not flip.
    """
    fake_gemini = MagicMock()
    refusal = BriefResponse(
        parsed={},
        raw_text="I cannot answer that.",
        prompt_tokens=1700,
        completion_tokens=70,
        model="gemini-2.5-flash",
    )
    fake_gemini.generate_brief.return_value = refusal

    one_topic = [
        {
            "market": "ng",
            "topic_group": "culture_owambe",
            "trend_score": 0.30,
            "item_count": 60,
            "source_diversity": 4,
            "creator_spread": 25,
            "velocity_score": 0.05,
            "tone_score": 0.5,
        }
    ]

    with (
        patch("src.analysis.generate_briefs.logger", new=MagicMock()),
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(rows=one_topic),
        ),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=0),
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    # Only ONE call total; retry skipped because raw_text is prose-refusal.
    assert fake_gemini.generate_brief.call_count == 1
    assert report.briefs == {}
    assert report.skipped_empty == [("ng", "culture_owambe")]


def test_generate_briefs_retries_when_raw_text_empty():
    """Transient-recovery: empty raw_text (Vertex 200 with empty body) now
    triggers a retry. 27 May 2026 ng/politics_tinubu hit this case on the
    autonomous cron and was silently skipped because the old behaviour
    treated empty raw_text as deterministic. A Phase 2 Briefs Only backfill
    60 minutes later landed the brief cleanly on first try, confirming the
    failure was transient. Retry on first attempt costs ~$0.0065 and
    recovers the class.
    """
    fake_gemini = MagicMock()
    empty_response = BriefResponse(
        parsed={},
        raw_text="",  # transient Vertex empty body
        prompt_tokens=1700,
        completion_tokens=0,
        model="gemini-2.5-flash",
    )
    recovered_response = _mock_brief_response()
    fake_gemini.generate_brief.side_effect = [empty_response, recovered_response]

    one_topic = [
        {
            "market": "ng",
            "topic_group": "politics_tinubu",
            "trend_score": 0.263,
            "item_count": 38,
            "source_diversity": 4,
            "creator_spread": 12,
            "velocity_score": 0.0,
            "tone_score": -0.2,
        }
    ]

    with (
        patch("src.analysis.generate_briefs.logger", new=MagicMock()),
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(rows=one_topic),
        ),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=0) as mock_insert,
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 27),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    assert fake_gemini.generate_brief.call_count == 2  # first + retry
    assert len(report.briefs) == 1
    assert ("ng", "politics_tinubu") in report.briefs
    assert report.skipped_empty == []
    # Tokens from BOTH attempts roll into spend.
    assert report.total_prompt_tokens == 1700 + 4500
    assert report.total_completion_tokens == 0 + 1800
    mock_insert.assert_called_once()


def test_generate_briefs_skips_persist_when_retry_also_empty():
    """If the retry is also empty, the brief is skipped, not persisted.

    Tracks the topic in report.skipped_empty so the email layer can
    surface the count to operators.
    """
    fake_gemini = MagicMock()
    fake_gemini.generate_brief.side_effect = [
        _empty_brief_response(),
        _empty_brief_response(),
    ]

    one_topic = [
        {
            "market": "ng",
            "topic_group": "culture_owambe",
            "trend_score": 0.30,
            "item_count": 60,
            "source_diversity": 4,
            "creator_spread": 25,
            "velocity_score": 0.05,
            "tone_score": 0.5,
        }
    ]

    with (
        patch("src.analysis.generate_briefs.logger", new=MagicMock()),
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(rows=one_topic),
        ),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=0) as mock_insert,
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    assert fake_gemini.generate_brief.call_count == 2
    assert report.briefs == {}
    assert report.skipped_empty == [("ng", "culture_owambe")]
    # Tokens still count (we paid for both useless calls).
    assert report.total_prompt_tokens == 3400  # 1700 * 2
    # No persist call when nothing to write.
    mock_insert.assert_not_called()


def test_generate_briefs_skips_topics_already_briefed():
    """If trend_analysis already has rows for (trend_date, market, topic), skip Vertex.

    Idempotency guard against double-spend on cron retries and ad-hoc
    backfills via the scripts/ops/run_phase2_briefs.py Cloud Run job.
    """
    fake_gemini = MagicMock()
    fake_gemini.generate_brief.return_value = _mock_brief_response()

    with (
        patch("src.analysis.generate_briefs.logger", new=MagicMock()),
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(),
        ),
        patch(
            "src.analysis.generate_briefs._existing_brief_keys",
            return_value={("za", "music_amapiano")},
        ),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=1) as mock_insert,
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    # Only the un-briefed topic (ng/music_afrobeats) should hit Vertex
    assert fake_gemini.generate_brief.call_count == 1
    assert ("ng", "music_afrobeats") in report.briefs
    assert ("za", "music_amapiano") not in report.briefs
    assert report.skipped_existing == [("za", "music_amapiano")]
    mock_insert.assert_called_once()


def test_generate_briefs_force_true_overrides_idempotency():
    """force=True bypasses the existing-key check and re-generates everything."""
    fake_gemini = MagicMock()
    fake_gemini.generate_brief.return_value = _mock_brief_response()

    with (
        patch("src.analysis.generate_briefs.logger", new=MagicMock()),
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(),
        ),
        patch(
            "src.analysis.generate_briefs._existing_brief_keys",
            return_value={("za", "music_amapiano"), ("ng", "music_afrobeats")},
        ) as mock_existing,
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=2),
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
            force=True,
        )

    # force=True should not even call _existing_brief_keys
    mock_existing.assert_not_called()
    assert fake_gemini.generate_brief.call_count == 2
    assert len(report.briefs) == 2
    assert report.skipped_existing == []


def test_generate_briefs_persist_false_skips_bq_write():
    """persist=False short-circuits the BQ insert."""
    fake_gemini = MagicMock()
    fake_gemini.generate_brief.return_value = _mock_brief_response()

    with (
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(
                rows=[
                    {
                        "market": "za",
                        "topic_group": "music_amapiano",
                        "trend_score": 0.48,
                        "item_count": 100,
                        "source_diversity": 22,
                        "creator_spread": 70,
                        "velocity_score": 0.07,
                        "tone_score": 0.6,
                    }
                ]
            ),
        ),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=1) as mock_insert,
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
            persist=False,
        )

    assert len(report.briefs) == 1
    mock_insert.assert_not_called()


def test_persist_failure_surfaces_on_report():
    """When insert_dataframe raises, the failure is surfaced on the report so
    the orchestrator can flag the email + log a warning. Previous behavior
    swallowed silently, letting briefs ship in-memory while BQ stayed empty.
    Open audit finding 5 May 2026."""
    fake_gemini = MagicMock()
    fake_gemini.generate_brief.return_value = _mock_brief_response()

    one_topic = [
        {
            "market": "za",
            "topic_group": "music_amapiano",
            "trend_score": 0.48,
            "item_count": 100,
            "source_diversity": 22,
            "creator_spread": 70,
            "velocity_score": 0.07,
            "tone_score": 0.6,
        }
    ]

    def _boom(*_args, **_kwargs):
        raise RuntimeError("BadRequest: schema mismatch on column visual_anchor")

    with (
        patch("src.analysis.generate_briefs.logger", new=MagicMock()),
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(rows=one_topic),
        ),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", side_effect=_boom),
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    # Report carries the persistence failure so the orchestrator can act.
    assert report.persist_attempted is True
    assert report.persist_succeeded is False
    assert report.persist_row_count == 1
    assert report.persist_error is not None
    assert "schema mismatch" in report.persist_error
    # Briefs are still in-memory (so the email path can still ship cards).
    assert len(report.briefs) == 1


def test_persist_success_marks_report_succeeded():
    """Happy-path BQ insert sets persist_succeeded True with no error."""
    fake_gemini = MagicMock()
    fake_gemini.generate_brief.return_value = _mock_brief_response()

    one_topic = [
        {
            "market": "za",
            "topic_group": "music_amapiano",
            "trend_score": 0.48,
            "item_count": 100,
            "source_diversity": 22,
            "creator_spread": 70,
            "velocity_score": 0.07,
            "tone_score": 0.6,
        }
    ]

    with (
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(rows=one_topic),
        ),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=1),
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    assert report.persist_attempted is True
    assert report.persist_succeeded is True
    assert report.persist_error is None
    assert report.persist_row_count == 1


def _many_topics(n: int) -> list[dict]:
    """n distinct (market, topic_group) rows so the brief loop produces n
    briefs, enough to span more than one PERSIST_CHUNK_SIZE chunk."""
    return [
        {
            "market": "za",
            "topic_group": f"music_topic_{i:02d}",
            "trend_score": 0.48,
            "item_count": 100,
            "source_diversity": 22,
            "creator_spread": 70,
            "velocity_score": 0.07,
            "tone_score": 0.6,
            "search_velocity_score": 0.18,
        }
        for i in range(n)
    ]


def test_generate_briefs_persists_in_chunks():
    """With more than PERSIST_CHUNK_SIZE briefs, persistence flushes per chunk
    DURING the loop, so insert_dataframe is called more than once and the
    summed row counts equal the total briefs persisted."""
    from src.analysis.generate_briefs import PERSIST_CHUNK_SIZE

    total = 10
    assert total > PERSIST_CHUNK_SIZE

    fake_gemini = MagicMock()
    fake_gemini.generate_brief.return_value = _mock_brief_response()

    with (
        patch("src.analysis.generate_briefs.logger", new=MagicMock()),
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(rows=_many_topics(total)),
        ),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=1) as mock_insert,
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
            top_n_per_market=total,
        )

    assert len(report.briefs) == total
    assert mock_insert.call_count > 1
    persisted = sum(len(call.args[0]) for call in mock_insert.call_args_list)
    assert persisted == total
    assert report.persist_succeeded is True
    assert report.persist_row_count == total


def test_generate_briefs_partial_persist_failure_keeps_earlier_chunks():
    """First chunk inserts, second chunk raises. The first chunk reached BQ
    (so a rerun skips those topics via _existing_brief_keys), the report
    flags the failure, and persist_succeeded is False."""
    from src.analysis.generate_briefs import PERSIST_CHUNK_SIZE

    total = 10
    assert total > PERSIST_CHUNK_SIZE

    fake_gemini = MagicMock()
    fake_gemini.generate_brief.return_value = _mock_brief_response()

    calls: list[int] = []

    def _first_ok_then_boom(df, _table):
        calls.append(len(df))
        if len(calls) == 1:
            return 1
        raise RuntimeError("BadRequest: schema mismatch on column visual_anchor")

    with (
        patch("src.analysis.generate_briefs.logger", new=MagicMock()),
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(rows=_many_topics(total)),
        ),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch(
            "src.analysis.generate_briefs.insert_dataframe",
            side_effect=_first_ok_then_boom,
        ) as mock_insert,
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
            top_n_per_market=total,
        )

    # First chunk reached BQ: a rerun's _existing_brief_keys would skip those.
    assert mock_insert.call_count >= 2
    assert calls[0] == PERSIST_CHUNK_SIZE
    assert report.persist_attempted is True
    assert report.persist_succeeded is False
    assert report.persist_error is not None
    assert "schema mismatch" in report.persist_error


def test_geo_blocklist_drops_contaminated_sample_rows():
    """Sample rows whose text references a foreign-collision geo for the
    topic (Sapa, Vietnam for ng/economy_sapa_hustle; Ankara, Turkey for
    ng/fashion_ankara_asoebi) must be filtered out before they reach
    the brief prompt. Thapelo flagged 7 May 2026: top creators on
    economy_sapa_hustle included @vietnamexpress.diary."""
    from src.analysis.generate_briefs import _row_matches_geo_blocklist

    # economy_sapa_hustle: Vietnam content blocked
    contaminated = {
        "title": "Sailing Sapa, Vietnam adventure",
        "text": "Best place ever in Vietnam",
        "url": "https://tiktok.com/@user/video/1?hashtag=sapavietnam",
    }
    assert _row_matches_geo_blocklist(contaminated, "economy_sapa_hustle") is True

    # economy_sapa_hustle: legitimate NG content passes
    legit = {
        "title": "Sapa Lagos hustle",
        "text": "Naija boys hustling sapa life",
        "url": "https://twitter.com/lagos_naija",
    }
    assert _row_matches_geo_blocklist(legit, "economy_sapa_hustle") is False

    # fashion_ankara_asoebi: Turkey content blocked
    turkey = {
        "title": "Ankara, Turkey city tour",
        "text": "Visit Türkiye capital",
        "url": "https://tiktok.com/v/123",
    }
    assert _row_matches_geo_blocklist(turkey, "fashion_ankara_asoebi") is True

    # fashion_ankara_asoebi: legitimate NG fabric content passes
    fabric = {
        "title": "Asoebi Ankara dress",
        "text": "Lagos owambe fashion",
        "url": "https://instagram.com/aso_ebi",
    }
    assert _row_matches_geo_blocklist(fabric, "fashion_ankara_asoebi") is False

    # Untracked topic returns False (no filter applied)
    untracked = {"title": "x", "text": "vietnam", "url": ""}
    assert _row_matches_geo_blocklist(untracked, "music_amapiano") is False


def test_format_social_refs_belt_and_braces_geo_filter():
    """Even when sample rows pass _sample_rows_for_topic's filter, the
    social_refs formatter must re-apply the topic blocklist so any row
    whose URL / title carries a foreign-collision marker is dropped
    before it reaches the email card. Caught 7 May 2026: sample-level
    filter let through 5 Turkey/Vietnam tourism URLs because their text
    bodies were generic enough to clear the original keyword list."""
    from src.analysis.generate_briefs import _format_social_refs_for_brief

    samples = [
        # Vietnam Sapa contamination
        {
            "url": "https://tiktok.com/v/sapahotels1",
            "platform": "tiktok",
            "title": "Top 7 Sapa hotels with train view",
            "text": "",
        },
        # Bahasa Malay sapa contamination
        {
            "url": "https://threads.net/sapamau",
            "platform": "threads",
            "title": "Sapa mau group wasap?",
            "text": "",
        },
        # Legitimate NG content
        {
            "url": "https://tiktok.com/v/lagos_naija",
            "platform": "tiktok",
            "title": "Naija boys hustling sapa life in Lagos",
            "text": "",
        },
    ]

    refs = _format_social_refs_for_brief(samples, topic_group="economy_sapa_hustle")

    # Only the Lagos NG ref survived.
    assert len(refs) == 1
    assert "lagos_naija" in refs[0]

    # Without topic_group (default), no filter applies (back-compat).
    refs_no_topic = _format_social_refs_for_brief(samples)
    assert len(refs_no_topic) == 3


def test_geo_blocklist_normalizes_unicode_apostrophes():
    """Source posts use Unicode apostrophes (U+2019) instead of ASCII;
    blocklist must match either. Caught 7 May 2026 round-3 audit:
    Turkish caption 'Ankara'da ne var ki?' bypassed the filter
    because U+2019 didn't match ASCII apostrophe in the blocklist."""
    from src.analysis.generate_briefs import _row_matches_geo_blocklist

    # Unicode apostrophe (right single quotation mark, U+2019)
    contaminated = {
        "title": "Ankara’da ne var ki?",  # noqa: RUF001
        "text": "",
        "url": "",
    }
    assert _row_matches_geo_blocklist(contaminated, "fashion_ankara_asoebi") is True

    # Right single quotation mark on possessive form
    contaminated2 = {
        "title": "Ankara’nın En Büyük Pavyonu",  # noqa: RUF001
        "text": "",
        "url": "",
    }
    assert _row_matches_geo_blocklist(contaminated2, "fashion_ankara_asoebi") is True


def test_geo_blocklist_drops_san_antonio_pets_alive_collision():
    """SAPA = San Antonio Pets Alive! (Texas USA shelter) collides with
    NG slang 'sapa'. English Latin script, doesn't trigger script
    backstop, requires keyword. Caught 7 May 2026 round-5."""
    from src.analysis.generate_briefs import _row_matches_geo_blocklist

    sapa_us = {
        "title": "Come on by any San Antonio Pets Alive! location this August",
        "text": "all adoption fees are waived",
        "url": "",
    }
    assert _row_matches_geo_blocklist(sapa_us, "economy_sapa_hustle") is True


def test_geo_blocklist_drops_non_ssa_script_content():
    """Layer-2 script-block backstop: rows containing 5+ Devanagari /
    Cyrillic / CJK / Thai / Hangul characters drop regardless of
    keyword match. Catches the contamination class without per-
    language keyword curation. Caught 7 May 2026 round-4: Hindi
    Samajwadi Party post leaked into ng/economy_sapa_hustle."""
    from src.analysis.generate_briefs import _row_matches_geo_blocklist

    # Devanagari (Hindi) content
    hindi = {
        "title": "योगी सरकार में खाद बीज",
        "text": "#yogi #kisan #upelection",
        "url": "https://instagram.com/p/x",
    }
    assert _row_matches_geo_blocklist(hindi, "economy_sapa_hustle") is True

    # Cyrillic content
    cyrillic = {
        "title": "Русский текст здесь",
        "text": "",
        "url": "",
    }
    assert _row_matches_geo_blocklist(cyrillic, "economy_sapa_hustle") is True

    # CJK content
    cjk = {
        "title": "中文内容这里",
        "text": "",
        "url": "",
    }
    assert _row_matches_geo_blocklist(cjk, "economy_sapa_hustle") is True

    # Latin-only content with sapa keyword (legitimate NG slang) passes
    legit = {
        "title": "Sapa life Lagos",
        "text": "Naija boys hustling",
        "url": "",
    }
    assert _row_matches_geo_blocklist(legit, "economy_sapa_hustle") is False

    # Untracked topic: no script filter applies (back-compat)
    untracked = {"title": "中文 here", "text": "", "url": ""}
    assert _row_matches_geo_blocklist(untracked, "music_amapiano") is False


def test_geo_blocklist_drops_turkish_latin_density():
    """Layer-3 foreign-Latin density backstop: Turkish content with
    3+ distinctive characters (s-cedilla, g-breve, dotless-i and caps)
    drops regardless of keyword match. Added 21 May 2026 to catch the
    Ankara content that survived round 7 keyword list. Examples drawn
    from real 16-21 May 2026 fashion_ankara_asoebi survivors."""
    from src.analysis.generate_briefs import (
        _has_foreign_latin_density,
        _row_matches_geo_blocklist,
    )

    # Real Ankara real-estate caption with 4+ Turkish-distinctive chars.
    turkish = {
        "title": "PORTFÖY NO: 529 Ankara Keçiören",
        "text": "5+1 225mt emlakdanışmanlığı lansmanfırsatı",  # noqa: RUF001
        "url": "",
    }
    assert _has_foreign_latin_density(" ".join(turkish.values())) == "turkish"
    assert _row_matches_geo_blocklist(turkish, "fashion_ankara_asoebi") is True

    # Turkish without enough density (<3 distinctive chars) still passes
    # this layer, falls through to keyword check.
    sparse = {"title": "Ankara çay", "text": "", "url": ""}
    assert _has_foreign_latin_density(" ".join(sparse.values())) is None

    # SSA Latin content with French / Portuguese accents not in the
    # distinctive Turkish set: must NOT trigger.
    ssa = {
        "title": "Lagos café déjà vu",
        "text": "à la carte naija boys",
        "url": "",
    }
    assert _has_foreign_latin_density(" ".join(ssa.values())) is None
    assert _row_matches_geo_blocklist(ssa, "fashion_ankara_asoebi") is False


def test_geo_blocklist_drops_vietnamese_latin_density():
    """Layer-3 backstop catches Vietnamese Sapa tour content using
    d-stroke / a-breve / o-horn / u-horn density. Examples drawn from
    real 16 May 2026 economy_sapa_hustle survivors that bypassed round
    8 keyword list."""
    from src.analysis.generate_briefs import (
        _has_foreign_latin_density,
        _row_matches_geo_blocklist,
    )

    # Real Vietnamese Sapa tour caption with multiple distinctive chars.
    vietnamese = {
        "title": "Tour Sa Pa 3N2Đ giá rẻ Hà Nội",
        "text": "Cảnh đẹp đáng để thử mùa lúa chín",
        "url": "",
    }
    assert _has_foreign_latin_density(" ".join(vietnamese.values())) == "vietnamese"
    assert _row_matches_geo_blocklist(vietnamese, "economy_sapa_hustle") is True

    # Vietnamese sentence with only tone marks (combining diacritics) and
    # no distinctive base chars: not caught by THIS layer; would need
    # the keyword list or a different rule.
    sparse_vi = {"title": "Mùa thu Hà Nội", "text": "", "url": ""}
    assert _has_foreign_latin_density(" ".join(sparse_vi.values())) is None

    # Legitimate NG sapa content with English Latin only passes.
    naija = {
        "title": "Sapa life Lagos",
        "text": "Naija boys hustling, side hustle weekend",
        "url": "",
    }
    assert _has_foreign_latin_density(" ".join(naija.values())) is None
    assert _row_matches_geo_blocklist(naija, "economy_sapa_hustle") is False


def test_geo_blocklist_catches_latin_transliterations():
    """Turkish creators on Latin keyboards transliterate diacritics
    away ('keşfet' -> 'kesfet'). Blocklist must catch both."""
    from src.analysis.generate_briefs import _row_matches_geo_blocklist

    transliterated = {
        "title": "Var iste #fyp #kesfet #ankara",
        "text": "",
        "url": "",
    }
    assert _row_matches_geo_blocklist(transliterated, "fashion_ankara_asoebi") is True


def test_creator_geo_blocklist_drops_foreign_handles():
    """Creator handles obviously referencing a foreign-collision geo for
    the topic must NOT show up as 'top creators to brief'. Thapelo
    flagged @vietnamexpress.diary on ng/economy_sapa_hustle 7 May 2026."""
    from src.analysis.generate_briefs import _creator_matches_geo_blocklist

    assert _creator_matches_geo_blocklist("vietnamexpress.diary", "economy_sapa_hustle") is True
    assert _creator_matches_geo_blocklist("@vietnamexpress.diary", "economy_sapa_hustle") is True
    assert _creator_matches_geo_blocklist("naija_hustler", "economy_sapa_hustle") is False
    assert _creator_matches_geo_blocklist("ankaraturkey_user", "fashion_ankara_asoebi") is True
    assert _creator_matches_geo_blocklist("istanbul_fashion", "fashion_ankara_asoebi") is True
    assert _creator_matches_geo_blocklist("ankara_dress_lagos", "fashion_ankara_asoebi") is False
    # Untracked topic: no filter applied
    assert _creator_matches_geo_blocklist("vietnamexpress.diary", "music_amapiano") is False


def test_retry_brief_token_counts_summed_into_persisted_row():
    """The TopicBrief that lands in trend_analysis should carry the SUM of
    first-call + retry-call tokens, not just the retry's. Mirrors the
    daily_summary retry-spend-ledger pattern. Without this, BQ rows for
    retried topics under-count tokens vs the report's total_prompt_tokens.
    """
    fake_gemini = MagicMock()
    fake_gemini.generate_brief.side_effect = [
        _empty_brief_response(),  # first call: empty, prompt=1700, completion=300
        _mock_brief_response(),  # retry: full, prompt=4500, completion=1800
    ]

    one_topic = [
        {
            "market": "za",
            "topic_group": "music_amapiano",
            "trend_score": 0.48,
            "item_count": 100,
            "source_diversity": 22,
            "creator_spread": 70,
            "velocity_score": 0.07,
            "tone_score": 0.6,
        }
    ]

    with (
        patch("src.analysis.generate_briefs.logger", new=MagicMock()),
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(rows=one_topic),
        ),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=1),
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    # The persisted brief must carry tokens from BOTH calls.
    persisted = report.briefs[("za", "music_amapiano")]
    assert persisted.prompt_tokens == 1700 + 4500
    assert persisted.completion_tokens == 300 + 1800
    # Report total agrees (sum of per-call tokens already).
    assert report.total_prompt_tokens == persisted.prompt_tokens
    assert report.total_completion_tokens == persisted.completion_tokens


def test_sample_is_too_thin_unit():
    """Unit check on the gate's threshold logic. Caught 14-25 May 2026
    noise-floor brief failures where Vertex returned empty over thin
    samples; gate skips the topic before the Vertex call now."""
    from src.analysis.generate_briefs import (
        _MIN_SAMPLE_ROWS,
        _MIN_SAMPLE_TEXT_CHARS,
        _MIN_SAMPLE_TEXT_CHARS_GEO_BLOCKLIST,
        _sample_is_too_thin,
    )

    # Empty sample is thin (zero rows).
    is_thin, rows, chars = _sample_is_too_thin([])
    assert is_thin is True
    assert rows == 0
    assert chars == 0

    # Too few rows (4 of 5 required).
    short_sample = [{"title": "long title " * 30, "text": "long body " * 30, "url": ""}] * 4
    is_thin, rows, chars = _sample_is_too_thin(short_sample)
    assert is_thin is True
    assert rows == 4

    # Enough rows but too little text.
    sparse_sample = [{"title": "a", "text": "b", "url": "https://example.com"}] * 10
    is_thin, rows, chars = _sample_is_too_thin(sparse_sample)
    assert is_thin is True
    assert rows == 10
    assert chars < _MIN_SAMPLE_TEXT_CHARS

    # The geo-blocklist topics get a looser 500-char floor because the geo
    # filter strips a large share of their rows. A 5-row ~600-char sample
    # clears the 500 floor (NOT thin for economy_sapa_hustle) but falls under
    # the 1000 floor (IS thin for music_amapiano). This pins the topic_group
    # parameter, which the prior cases never exercised.
    body = "x" * 120  # 120 chars per row; 5 rows = 600 chars total
    blocklist_sample = [{"title": "", "text": body, "url": "https://example.com"}] * 5
    is_thin, rows, chars = _sample_is_too_thin(blocklist_sample, "economy_sapa_hustle")
    assert rows == 5
    assert _MIN_SAMPLE_TEXT_CHARS_GEO_BLOCKLIST <= chars < _MIN_SAMPLE_TEXT_CHARS
    assert is_thin is False  # 600 chars clears the 500 blocklist floor
    is_thin, _, _ = _sample_is_too_thin(blocklist_sample, "music_amapiano")
    assert is_thin is True  # but 600 chars is under the 1000 default floor

    # Fat sample (the test fixture) passes the gate.
    is_thin, rows, chars = _sample_is_too_thin(_FAT_SAMPLE)
    assert is_thin is False
    assert rows >= _MIN_SAMPLE_ROWS
    assert chars >= _MIN_SAMPLE_TEXT_CHARS


def test_generate_briefs_skips_thin_sample_topic():
    """Integration check: a topic whose sample fails the thinness gate
    is recorded in report.skipped_thin_sample, the Vertex call is never
    made for it, and the other (fat) topic still gets briefed."""
    fake_gemini = MagicMock()
    fake_gemini.generate_brief.return_value = _mock_brief_response()

    # Custom sample function: za topic gets fat sample, ng topic gets
    # thin (empty) sample. The ng topic should be skipped via the gate.
    def _per_topic_sample(*args, **kwargs):
        # generate_briefs calls _sample_rows_for_topic with positional
        # args (client, dataset, market, topic_group, trend_date, n).
        market = args[2] if len(args) >= 3 else kwargs.get("market")
        if market == "ng":
            return []
        return _FAT_SAMPLE

    with (
        patch("src.analysis.generate_briefs.logger", new=MagicMock()),
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(),
        ),
        patch(
            "src.analysis.generate_briefs._sample_rows_for_topic",
            side_effect=_per_topic_sample,
        ),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=1),
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    # ng got skipped via thin-sample gate; za was briefed normally.
    assert ("za", "music_amapiano") in report.briefs
    assert ("ng", "music_afrobeats") not in report.briefs
    # The thin-sample skip is recorded with (market, topic, rows, chars).
    assert len(report.skipped_thin_sample) == 1
    skip = report.skipped_thin_sample[0]
    assert skip[0] == "ng"
    assert skip[1] == "music_afrobeats"
    assert skip[2] == 0  # row_count
    assert skip[3] == 0  # total_chars
    # Vertex was called exactly once (for za), not twice (no ng call).
    assert fake_gemini.generate_brief.call_count == 1


# ---------------------------------------------------------------------------
# Workstream E: Brand24 sentiment trajectory
# ---------------------------------------------------------------------------


class _FakeSentRow:
    def __init__(self, positive, total):
        self.positive = positive
        self.total = total


def _traj_client(rows):
    """A MagicMock bq client whose query().result() yields the given rows."""
    client = MagicMock()
    client.project = "ogilvy-trends-v2"
    client.query.return_value.result.return_value = iter(rows)
    return client


def test_b24_sentiment_trajectory_improving():
    # older half positive-share ~0.3, recent half ~0.7 -> improving
    rows = [
        _FakeSentRow(30, 100),
        _FakeSentRow(35, 100),
        _FakeSentRow(70, 100),
        _FakeSentRow(75, 100),
    ]
    label = _b24_sentiment_trajectory_for_market(
        _traj_client(rows), "trends_v2_dev", "ng", date(2026, 5, 29)
    )
    assert label.startswith("improving")


def test_b24_sentiment_trajectory_declining():
    rows = [
        _FakeSentRow(80, 100),
        _FakeSentRow(75, 100),
        _FakeSentRow(30, 100),
        _FakeSentRow(25, 100),
    ]
    label = _b24_sentiment_trajectory_for_market(
        _traj_client(rows), "trends_v2_dev", "ng", date(2026, 5, 29)
    )
    assert label.startswith("declining")


def test_b24_sentiment_trajectory_stable():
    rows = [_FakeSentRow(50, 100), _FakeSentRow(52, 100), _FakeSentRow(51, 100)]
    label = _b24_sentiment_trajectory_for_market(
        _traj_client(rows), "trends_v2_dev", "ng", date(2026, 5, 29)
    )
    assert label == "stable"


def test_b24_sentiment_trajectory_empty_when_dark():
    """No rows (surface dark) -> empty string, graceful no-op."""
    label = _b24_sentiment_trajectory_for_market(
        _traj_client([]), "trends_v2_dev", "ng", date(2026, 5, 29)
    )
    assert label == ""


def test_to_bq_row_includes_b24_sentiment_trajectory():
    brief = _topic_brief()
    brief.b24_sentiment_trajectory = "improving (+12pp positive share)"
    row = brief.to_bq_row(date(2026, 5, 29))
    assert row["b24_sentiment_trajectory"] == "improving (+12pp positive share)"


# ---------------------------------------------------------------------------
# PULSE v2: headline / risk_flags + display assembly
# ---------------------------------------------------------------------------


def test_to_topic_brief_reads_headline_and_risk_flags():
    """_to_topic_brief now reads parsed headline + risk_flags onto the brief.

    Both were previously dropped: headline did not exist as a field and
    risk_flags was hardcoded to [] in to_bq_row. The v2 mailer + the
    brand-safety persistence both depend on these landing.
    """
    response = _mock_brief_response(
        parsed={
            "description_rationale": "x",
            "activation_idea": "y",
            "key_metrics": ["a", "b", "c"],
            "platforms": ["TikTok"],
            "sentiment_summary": "ok",
            "status_tag": "Key",
            "headline": "Amapiano goes mainstream",
            "risk_flags": ["alcohol reference", "political figure"],
        }
    )
    brief = _to_topic_brief("za", "music_amapiano", 0.48, response)
    assert brief.headline == "Amapiano goes mainstream"
    assert brief.risk_flags == ["alcohol reference", "political figure"]
    # headline + risk_flags now flow through to the persisted row. headline was
    # generated and rendered in-memory but never stored, so the preview and the
    # dashboard fell back to the topic slug; it is now first-class in BQ.
    row = brief.to_bq_row(date(2026, 5, 29))
    assert row["risk_flags"] == ["alcohol reference", "political figure"]
    assert row["headline"] == "Amapiano goes mainstream"


def test_to_topic_brief_headline_risk_flags_default_empty():
    """Absent headline / risk_flags default to empty, never None."""
    response = _mock_brief_response(
        parsed={
            "description_rationale": "x",
            "activation_idea": "y",
            "key_metrics": ["a"],
            "platforms": ["TikTok"],
            "sentiment_summary": "ok",
            "status_tag": "Rising",
        }
    )
    brief = _to_topic_brief("za", "x", 0.4, response)
    assert brief.headline == ""
    assert brief.risk_flags == []
    assert brief.to_bq_row(date(2026, 5, 29))["risk_flags"] == []


def test_build_display_produces_aligned_dict():
    """_build_display maps onto the display_layer and returns the exact key
    set the PULSE v2 renderers read: state / phase / window / in_market_pct
    / channels / confidence / search.
    """
    from src.analysis.generate_briefs import _build_display

    display = _build_display(
        market="za",
        topic_group="music_amapiano",
        trend_score=0.55,
        velocity=0.35,
        prior_score=0.50,
        platform_count_rows=[
            {"platform": "tiktok", "count": 40},
            {"platform": "threads", "count": 20},
        ],
        sample_rows=[
            {"title": "Amapiano set in Soweto", "text": "kasi vibes", "hashtags": ""},
        ],
        search_velocity=None,
    )
    assert set(display.keys()) == {
        "state",
        "phase",
        "window",
        "in_market_pct",
        "channels",
        "confidence",
        "search",
    }
    # state is itself a dict with a badge + direction (display_layer.trend_state)
    assert "badge" in display["state"]
    assert "direction" in display["state"]
    # channels weights derive from the per-platform counts
    assert display["channels"][0][0] == "tiktok"
    # confidence reflects the 2-channel count, honestly (never "confirmed")
    assert "confirmed" not in display["confidence"].lower()
    assert "2 channels" in display["confidence"]
    # search_velocity None renders "no lift", never invented
    assert display["search"] == "no lift"
    # prior_score below current by >0.03 -> accelerating
    assert display["state"]["direction"] == "up"


def test_build_display_no_prior_is_new_on_board():
    """A topic with no prior-day score renders as New on the board."""
    from src.analysis.generate_briefs import _build_display

    display = _build_display(
        market="ng",
        topic_group="music_afrobeats",
        trend_score=0.46,
        velocity=0.07,
        prior_score=None,
        platform_count_rows=[],
        sample_rows=[],
        search_velocity=None,
    )
    assert display["state"]["direction"] == "new"
    # no platform counts -> no channels, single-source confidence
    assert display["channels"] == []
    assert display["confidence"] == "single-source"


def test_generate_briefs_attaches_aligned_display_per_brief():
    """The orchestrator attaches a display dict to every brief, so the email
    path can build a displays list positionally aligned to the briefs list.
    """
    fake_bq = MagicMock()
    fake_gemini = MagicMock()
    fake_gemini.generate_brief.return_value = _mock_brief_response()

    with (
        patch(
            "src.analysis.generate_briefs._query_top_topics",
            return_value=_topics_df(),
        ),
        patch("src.analysis.generate_briefs._sample_rows_for_topic", return_value=_FAT_SAMPLE),
        patch("src.analysis.generate_briefs._top_creators_for_topic", return_value=[]),
        patch("src.analysis.generate_briefs.insert_dataframe", return_value=2),
    ):
        report = generate_briefs(
            trend_date=date(2026, 5, 5),
            gemini_client=fake_gemini,
            bq_client=fake_bq,
            dataset="trends_v2_dev",
        )

    assert len(report.briefs) == 2
    # Every brief carries a non-empty display with the renderer key set.
    expected_keys = {
        "state",
        "phase",
        "window",
        "in_market_pct",
        "channels",
        "confidence",
        "search",
    }
    for brief in report.briefs.values():
        assert isinstance(brief.display, dict)
        assert set(brief.display.keys()) == expected_keys

    # Search-as-signal must survive the query->display seam. The fixture
    # carries search_velocity_score, so za (0.18) reads "rising" and ng
    # (0.05) reads "flat". A missing SELECT column would default both to
    # "no lift" and silently kill the SEARCH chip.
    assert report.briefs[("za", "music_amapiano")].display["search"] == "rising"
    assert report.briefs[("ng", "music_afrobeats")].display["search"] == "flat"

    # Build the aligned briefs + displays lists the way the email path will:
    # same order, same length.
    briefs_list = list(report.briefs.values())
    displays_list = [b.display for b in briefs_list]
    assert len(briefs_list) == len(displays_list)
    assert displays_list[0] is briefs_list[0].display


def test_query_top_topics_selects_search_velocity_score():
    """Regression guard: search_velocity_score must be in the SELECT.

    The read site in _build_display defaults a missing column to None,
    which search_read maps to "no lift", which suppresses the SEARCH chip
    on every card. The column dropping out of the SELECT silently kills
    the search-as-signal differentiator in the live cron. Tests that mock
    _query_top_topics wholesale cannot catch that, so pin the SQL here.
    """
    from src.analysis.generate_briefs import _query_top_topics

    captured: dict[str, str] = {}
    fake_client = MagicMock()
    fake_client.project = "proj"

    def _capture(sql, job_config=None):
        captured["sql"] = sql
        result = MagicMock()
        result.to_dataframe.return_value = _topics_df()
        return result

    fake_client.query.side_effect = _capture
    _query_top_topics(fake_client, "trends_v2_dev", date(2026, 5, 5), top_n_per_market=8)

    assert "search_velocity_score" in captured["sql"]


# ---------------------------------------------------------------------------
# Prompt grounding: engagement columns + derived-signal digest query
# ---------------------------------------------------------------------------


def _capture_sql_client(rows: list | None = None) -> tuple[MagicMock, dict[str, str]]:
    """A bq client whose query() records the SQL and yields rows on result()."""
    captured: dict[str, str] = {}
    client = MagicMock()
    client.project = "proj"

    def _capture(sql, job_config=None):
        captured["sql"] = sql
        result = MagicMock()
        result.result.return_value = iter(rows or [])
        return result

    client.query.side_effect = _capture
    return client, captured


def test_sample_rows_select_includes_engagement_columns():
    """Regression guard: the sample SELECT must pull engagement magnitudes.

    _sample_rows_for_topic ORDER BYs on engagement but historically only
    SELECTed title/text/platform/url, so every number was discarded and
    the brief prompt was structurally blind to reach. Pin the columns in
    the SQL so they cannot silently drop out again.
    """
    from src.analysis.generate_briefs import _sample_rows_for_topic

    client, captured = _capture_sql_client()
    _sample_rows_for_topic(client, "trends_v2_dev", "za", "music_amapiano", date(2026, 5, 5), 10)

    sql = captured["sql"]
    assert "engagement_total" in sql
    assert "views" in sql
    assert "likes" in sql
    assert "comments" in sql
    assert "shares" in sql


class _FakeSampleRow:
    """Stand-in for a BQ Row carrying the engagement columns."""

    def __init__(self, **kwargs):
        self.title = kwargs.get("title", "")
        self.text = kwargs.get("text", "")
        self.platform = kwargs.get("platform", "")
        self.url = kwargs.get("url", "")
        self.engagement_total = kwargs.get("engagement_total", 0.0)
        self.views = kwargs.get("views", 0.0)
        self.likes = kwargs.get("likes", 0.0)
        self.comments = kwargs.get("comments", 0.0)
        self.shares = kwargs.get("shares", 0.0)


def test_sample_rows_carry_engagement_into_dicts():
    """The per-row dicts returned must carry the engagement numbers through."""
    from src.analysis.generate_briefs import _sample_rows_for_topic

    rows = [
        _FakeSampleRow(
            title="Amapiano set",
            text="kasi vibes",
            platform="tiktok",
            url="https://x/1",
            engagement_total=1_340_000.0,
            views=1_300_000.0,
            likes=40_000.0,
            comments=1_200.0,
            shares=2_500.0,
        )
    ]
    client, _ = _capture_sql_client(rows)
    out = _sample_rows_for_topic(
        client, "trends_v2_dev", "za", "music_amapiano", date(2026, 5, 5), 10
    )
    assert len(out) == 1
    assert out[0]["engagement_total"] == 1_340_000.0
    assert out[0]["views"] == 1_300_000.0
    assert out[0]["likes"] == 40_000.0
    assert out[0]["comments"] == 1_200.0
    assert out[0]["shares"] == 2_500.0


def test_sample_rows_geo_blocklist_overfetch_and_drops():
    """The geo-blocklist path: 3x over-fetch, row-level contamination drop,
    hard-foreign-language drop, and the cap at n.

    Both existing SQL-guard tests use music_amapiano (not in the blocklist),
    so this whole branch (fetch_n = n*3, _row_matches_geo_blocklist drop,
    _row_is_hard_foreign_language drop, early break at n) was unpinned.
    """
    from unittest.mock import patch

    from src.analysis.generate_briefs import _sample_rows_for_topic

    # A clean row, a geo-contaminated row (matches the economy_sapa_hustle
    # blocklist via 'sapa vietnam' / 'fansipan'), a row we flag hard-foreign,
    # then two more clean rows so the survivors exceed n=2 and the cap fires.
    rows = [
        _FakeSampleRow(title="Sapa hustle", text="kasi grind energy", url="https://x/1"),
        _FakeSampleRow(
            title="Sapa Vietnam trek",
            text="Fansipan mountain tour in Sapa Vietnam",
            url="https://x/2",
        ),
        _FakeSampleRow(
            title="foreign row", text="this row is flagged hard-foreign", url="https://x/3"
        ),
        _FakeSampleRow(title="Sapa side hustle two", text="japa money moves", url="https://x/4"),
        _FakeSampleRow(title="Sapa side hustle three", text="naija sapa season", url="https://x/5"),
    ]

    captured: dict[str, int] = {}

    def _capture(sql, job_config=None):
        for p in job_config.query_parameters:
            if p.name == "lim":
                captured["lim"] = p.value
        result = MagicMock()
        result.result.return_value = iter(rows)
        return result

    client = MagicMock()
    client.project = "proj"
    client.query.side_effect = _capture

    def _fake_hard_foreign(row):
        return "flagged hard-foreign" in str(row.get("text") or "")

    with patch(
        "src.analysis.generate_briefs._row_is_hard_foreign_language",
        side_effect=_fake_hard_foreign,
    ):
        out = _sample_rows_for_topic(
            client, "trends_v2_dev", "ng", "economy_sapa_hustle", date(2026, 5, 5), 2
        )

    # 3x over-fetch: n=2 -> lim=6.
    assert captured["lim"] == 6
    # The contaminated row and the hard-foreign row are dropped; the output is
    # capped at n=2 from the surviving clean rows.
    assert len(out) == 2
    urls = {r["url"] for r in out}
    assert "https://x/2" not in urls  # geo-contaminated dropped
    assert "https://x/3" not in urls  # hard-foreign dropped


def test_signals_digest_query_includes_content_aggregates_only():
    """The per-topic digest SELECT aggregates slang and entities.

    Grounds description_rationale + sentiment_summary + the generative
    prompts in the local signals the engine already matched, and lets the
    model disambiguate foreign-collision topics (sapa-the-hustle vs
    Sapa-Vietnam). Pin the aggregates in the SQL so they stay wired.
    """
    from src.analysis.generate_briefs import _signals_digest_for_topic

    client, captured = _capture_sql_client()
    _signals_digest_for_topic(
        client, "trends_v2_dev", "ng", "economy_sapa_hustle", date(2026, 5, 5)
    )

    sql = captured["sql"]
    assert "v2persons" in sql
    assert "v2orgs" in sql
    assert "slang_terms" in sql
    assert "genz_score" not in sql
    assert "STRING_AGG" in sql.upper()
    # v2locations is intentionally NOT aggregated: the GKG location column is
    # a FIPS hash, not a clean name, so it produced garbage tokens.
    assert "v2locations" not in sql


class _FakeDigestRow:
    def __init__(self, persons, orgs, locations, slang, genz):
        self.persons = persons
        self.orgs = orgs
        self.locations = locations
        self.slang = slang
        self.genz_score = genz


def test_signals_digest_parses_and_caps_lists():
    """The digest result is parsed into capped, de-duplicated name lists.

    GDELT entity strings arrive as 'Name,offset;Name,offset' aggregates;
    the parser must strip offsets, split on the delimiters, de-dup, and
    cap each list.
    """
    from src.analysis.generate_briefs import _signals_digest_for_topic

    row = _FakeDigestRow(
        persons="Bola Tinubu,12;Peter Obi,40;Bola Tinubu,90",
        orgs="CBN,3;EFCC,7",
        locations="Lagos,1;Abuja,5",
        slang="sapa,japa,sapa",
        genz=0.42,
    )
    client, _ = _capture_sql_client([row])
    # Non-collision topic: entities are surfaced (collision topics withhold
    # them, covered separately).
    digest = _signals_digest_for_topic(
        client, "trends_v2_dev", "ng", "politics_tinubu", date(2026, 5, 5)
    )

    # Offsets stripped, duplicates collapsed.
    assert "Bola Tinubu" in digest["persons"]
    assert "Peter Obi" in digest["persons"]
    assert digest["persons"].count("Bola Tinubu") == 1
    assert "12" not in " ".join(digest["persons"])
    assert digest["orgs"] == ["CBN", "EFCC"]
    # locations are no longer parsed or returned.
    assert "locations" not in digest
    # Slang de-duplicated.
    assert sorted(digest["slang"]) == ["japa", "sapa"]
    assert "genz_score" not in digest
    # Each list is capped at 5 entries.
    for key in ("persons", "orgs", "slang"):
        assert len(digest[key]) <= 5


def test_signals_digest_empty_when_no_rows():
    """No rows return empty content signals without a crash."""
    from src.analysis.generate_briefs import _signals_digest_for_topic

    client, _ = _capture_sql_client([])
    digest = _signals_digest_for_topic(
        client, "trends_v2_dev", "za", "music_amapiano", date(2026, 5, 5)
    )
    assert digest["persons"] == []
    assert digest["orgs"] == []
    assert digest["slang"] == []
    assert "genz_score" not in digest
    assert "locations" not in digest


def test_signals_digest_withholds_entities_for_geo_collision_topic():
    """Geo-collision topics surface slang only, never named entities.

    The SQL aggregate pulls in the exact foreign names (Sapa -> Lao Cai,
    Fansipan) the sample path strips row-by-row, and the SYSTEM_INSTRUCTION
    forbids naming the foreign meaning. So for topics in _TOPIC_GEO_BLOCKLIST
    the persons/orgs lists must come back empty even when the columns are
        populated; only the matched local slang passes through.
    """
    from src.analysis.generate_briefs import _signals_digest_for_topic

    row = _FakeDigestRow(
        persons="Lao Cai,3;Fansipan,9",
        orgs="Sapa Tourism Board,2",
        locations="Lao Cai,1",
        slang="sapa,hustle",
        genz=0.5,
    )
    client, _ = _capture_sql_client([row])
    digest = _signals_digest_for_topic(
        client, "trends_v2_dev", "ng", "economy_sapa_hustle", date(2026, 5, 5)
    )

    # Foreign entities must not leak through for a collision topic.
    assert digest["persons"] == []
    assert digest["orgs"] == []
    assert "Lao Cai" not in str(digest)
    assert "Fansipan" not in str(digest)
    # The local slang still surfaces as the disambiguator.
    assert sorted(digest["slang"]) == ["hustle", "sapa"]
    assert "genz_score" not in digest


def test_to_bq_row_persists_render_payload_with_display():
    """The display bundle persists at insert time inside render_payload, so a
    send that re-reads briefs from BigQuery (resend, preview, recovery day)
    carries the same Seen-on channels and state badge the live email
    rendered. Regression for the 10 Jun recovery resend, which reached the
    exec list with empty Seen-on strips because display lived only in
    memory."""
    import json as _json

    brief = TopicBrief(
        market="ke",
        topic_group="sports_football",
        trend_score=0.31,
        description_rationale="football read",
        activation_idea="activation",
        visual_anchor="anchor",
        nano_banana_prompt="prompt",
        lyria_prompt="lyria",
        key_metrics=["m1"],
        platforms=["TikTok"],
        sentiment_summary="positive",
        status_tag="Rising",
        top_creators=[],
        social_refs=[],
        platform_counts=["tiktok | 10 items"],
        prompt_tokens=1,
        completion_tokens=1,
        model="gemini-3.5-flash",
    )
    brief.display = {
        "state": {"badge": "New on the board", "direction": "new"},
        "channels": [("tiktok", 4)],
    }
    row = brief.to_bq_row(date(2026, 6, 10))
    payload = _json.loads(row["render_payload"])
    assert payload["display"]["state"]["badge"] == "New on the board"
    assert payload["display"]["channels"]


def test_persist_render_payloads_updates_one_row_per_brief(monkeypatch):
    """persist_render_payloads issues one batched MERGE and serializes render_payload."""
    import json as _json

    from src.analysis import generate_briefs as gb

    captured: list[tuple[str, dict]] = []

    class _FakeJob:
        # persist_render_payloads reports what BigQuery actually changed rather
        # than the brief count, so the fake job has to carry the DML row count.
        num_dml_affected_rows = 2

        def result(self):
            return None

    class _FakeClient:
        project = "proj"

        def query(self, sql, job_config=None):
            params = {
                p.name: p.values if hasattr(p, "values") else p.value
                for p in job_config.query_parameters
            }
            captured.append((sql, params))
            return _FakeJob()

    monkeypatch.setattr("src.utils.bigquery.get_client", lambda: _FakeClient())
    monkeypatch.setattr("src.utils.bigquery.get_dataset", lambda: "ds")

    briefs = {
        ("ng", "music_afrobeats"): {
            "display": {"phase": "Peaking"},
            "comment_sentiment": "warm",
            "comment_opening": "room for a brand that platforms the underground",
            "comment_themes": ["t1"],
            "driving_hashtags": [{"tag": "#x", "share_pct": 40.0, "mood": "hype"}],
        },
        ("ke", "sports_football"): {"display": {"phase": "Emerging"}},
    }
    updated = gb.persist_render_payloads(date(2026, 6, 10), briefs)
    assert updated == 2
    assert len(captured) == 1
    sql, params = captured[0]
    assert "MERGE" in sql
    assert "UNION ALL" in sql
    assert "render_payload" in sql
    assert "MAX(analyzed_at)" in sql
    payload = _json.loads(params["payload_0"])
    assert payload["display"]["phase"] == "Peaking"
    assert payload["comment_sentiment"] == "warm"
    assert payload["comment_opening"] == "room for a brand that platforms the underground"
    assert payload["driving_hashtags"][0]["tag"] == "#x"
    payload2 = _json.loads(params["payload_1"])
    assert payload2["display"]["phase"] == "Emerging"
    assert payload2["comment_sentiment"] == ""


def test_persist_render_payloads_on_clause_carries_no_subquery(monkeypatch):
    """The MERGE join predicate must stay free of subqueries.

    BigQuery rejects a MERGE whose ON clause contains a subquery over a table:
    "Unsupported subquery with table in join predicate". An earlier form
    correlated a SELECT MAX(analyzed_at) against the source alias inside ON, so
    the statement raised on every pipeline run for at least four consecutive days
    while the caller logged it as non-fatal. The enrichment silently never reached
    trend_analysis and render_payload kept only its display key. The latest-row
    lookup belongs in USING, where a subquery is legal.
    """
    from src.analysis import generate_briefs as gb

    captured: list[str] = []

    class _FakeJob:
        num_dml_affected_rows = 1

        def result(self):
            return None

    class _FakeClient:
        project = "proj"

        def query(self, sql, job_config=None):
            captured.append(sql)
            return _FakeJob()

    monkeypatch.setattr("src.utils.bigquery.get_client", lambda: _FakeClient())
    monkeypatch.setattr("src.utils.bigquery.get_dataset", lambda: "ds")

    gb.persist_render_payloads(date(2026, 6, 10), {("ng", "music_afrobeats"): {"display": {}}})

    sql = captured[0]
    start = sql.index(" ON T.trend_date")
    on_clause = sql[start : sql.index("WHEN MATCHED", start)]
    assert "SELECT" not in on_clause.upper(), (
        f"MERGE ON clause contains a subquery, BigQuery will reject it: {on_clause}"
    )
    # The max lookup must still happen, just relocated into the source.
    assert "MAX(analyzed_at)" in sql[: sql.index(" ON T.trend_date")]


def test_prefetch_brief_aggregates_batches_counts_and_peaks(monkeypatch):
    from src.analysis import generate_briefs as gb

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def __iter__(self):
            return iter(self._rows)

    class _Row:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _FakeClient:
        project = "proj"

        def query(self, sql, job_config=None):
            if "MAX(CAST(engagement_total" in sql:
                rows = [_Row(market="ng", topic_group="music_afrobeats", peak=9000.0)]
            else:
                rows = [_Row(market="ng", topic_group="music_afrobeats", platform="tiktok", n=12)]

            class _Job:
                def result(self_inner):
                    return rows

            return _Job()

    counts, peaks = gb._prefetch_brief_aggregates(
        _FakeClient(),
        "ds",
        date(2026, 6, 10),
        [("ng", "music_afrobeats")],
    )
    assert counts[("ng", "music_afrobeats")][0]["count"] == 12
    assert peaks[("ng", "music_afrobeats")] == 9000.0
