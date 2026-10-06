"""Tests for src/ingestion/enrichment.py (Phase 1.7 ported from MVP)."""

from datetime import UTC

import pandas as pd
from src.ingestion.enrichment import (
    build_handle_to_tier_fallback,
    build_watchlist_lookup,
    detect_topic,
    extract_hashtags,
    extract_slang_terms,
    normalize_handle,
    relevance_score,
    score_slang,
    watchlist_score_for_tier,
)


def test_extract_hashtags_basic():
    assert extract_hashtags("#Amapiano and #Mzansi vibes") == ["amapiano", "mzansi"]


def test_extract_hashtags_empty_input():
    assert extract_hashtags("") == []
    assert extract_hashtags(None) == []


def test_relevance_score_saturates_without_hard_cap():
    markers = ["lagos", "naija", "9ja", "abuja"]
    text = "lagos naija 9ja abuja all in one post"
    # 4 word-boundary hits saturate via hits/(hits+1.5), below 1.0 (no cliff).
    assert relevance_score(text, markers) == round(4 / 5.5, 4)


def test_relevance_score_partial():
    markers = ["lagos", "naija", "9ja"]
    # one hit -> 1/(1+1.5) = 0.40
    assert round(relevance_score("lagos story", markers), 4) == round(1 / 2.5, 4)


def test_relevance_score_word_boundary_no_substring_false_positive():
    # "died" must not fire inside "studied"; "student" marker should still
    # hit the standalone word.
    assert relevance_score("she studied hard", ["died"]) == 0.0
    assert relevance_score("a student post", ["student"]) == round(1 / 2.5, 4)


def test_relevance_score_handles_int_markers():
    # ke.yaml previously had '- 254' as int; must not crash
    markers = [254, "kenya"]
    assert relevance_score("+254 is kenya's dialling code", markers) > 0


def test_relevance_score_empty():
    assert relevance_score("", ["x"]) == 0.0
    assert relevance_score("text", []) == 0.0


def test_detect_topic_first_match():
    groups = {
        "tech": ["ai", "crypto"],
        "news": ["election", "court"],
    }
    assert detect_topic("AI breakthrough in Johannesburg", groups) == "tech"
    assert detect_topic("Election court ruling", groups) == "news"


def test_detect_topic_other_when_none_match():
    assert detect_topic("random content", {"tech": ["ai"]}) == "other"


def test_detect_topic_empty_inputs():
    assert detect_topic("", {"tech": ["ai"]}) == "other"
    assert detect_topic("ai is cool", {}) == "other"


def test_normalize_handle_strips_and_lowers():
    assert normalize_handle("  @LasiZwe  ") == "lasizwe"
    assert normalize_handle("@@double") == "double"
    assert normalize_handle(None) == ""


def test_build_watchlist_lookup():
    cfg = {
        "watchlists": {
            "tiktok": {
                "tier_1": ["@lasizwe", "Boity"],
                "tier_2": ["foo"],
            },
            "instagram": {"tier_3": ["bar"]},
        }
    }
    lookup = build_watchlist_lookup(cfg)
    assert lookup[("tiktok", "lasizwe")] == "tier_1"
    assert lookup[("tiktok", "boity")] == "tier_1"
    assert lookup[("tiktok", "foo")] == "tier_2"
    assert lookup[("instagram", "bar")] == "tier_3"


def test_build_watchlist_lookup_empty_cfg():
    assert build_watchlist_lookup({}) == {}
    assert build_watchlist_lookup({"watchlists": {}}) == {}


def test_watchlist_score_for_tier():
    assert watchlist_score_for_tier("tier_1") == 1.0
    assert watchlist_score_for_tier("tier_2") == 0.7
    assert watchlist_score_for_tier("tier_3") == 0.4
    assert watchlist_score_for_tier("") == 0.0
    assert watchlist_score_for_tier(None) == 0.0


def test_build_handle_to_tier_fallback_promotes_highest_tier():
    """When a handle sits in tier_1 on one platform and tier_3 on another,
    the cross-platform fallback returns tier_1 (the higher signal)."""
    cfg = {
        "watchlists": {
            "tiktok": {"tier_3": ["@tyla"]},
            "instagram": {"tier_1": ["tyla"]},
        }
    }
    fallback = build_handle_to_tier_fallback(cfg)
    assert fallback["tyla"] == "tier_1"


def test_build_handle_to_tier_fallback_normalises_handles():
    cfg = {"watchlists": {"tiktok": {"tier_1": ["@LasiZwe", "  Boity  "]}}}
    fallback = build_handle_to_tier_fallback(cfg)
    assert fallback["lasizwe"] == "tier_1"
    assert fallback["boity"] == "tier_1"


def test_build_handle_to_tier_fallback_empty_cfg():
    assert build_handle_to_tier_fallback({}) == {}
    assert build_handle_to_tier_fallback({"watchlists": {}}) == {}


def test_normalize_handle_at_variants_collapse():
    """Regression: @Tyla, tyla, TYLA all collapse to the same normalised
    form so the watchlist join is case + @-prefix insensitive."""
    assert normalize_handle("@Tyla") == "tyla"
    assert normalize_handle("tyla") == "tyla"
    assert normalize_handle("TYLA") == "tyla"
    assert normalize_handle("  @@TYLA  ") == "tyla"


def test_enrich_dataframe_watchlist_cross_platform_fallback():
    """A creator handle listed under 'tiktok' in the yaml should match
    rows ingested on 'youtube' or 'reddit' via the cross-platform fallback.
    """
    from src.ingestion.enrichment import enrich_dataframe

    df = pd.DataFrame(
        [
            # YouTube row, handle lives in ZA tiktok watchlist (tyla, tier_1)
            {
                "title": "Tyla performs at MTV",
                "text": "amapiano hit Water trending",
                "author_handle": "tyla",
                "platform": "youtube",
                "query_group": "",
            },
            # Reddit row, handle lives in ZA tiktok watchlist (lasizwe, tier_1)
            {
                "title": "[r/southafrica]",
                "text": "lasizwe new skit dropped today",
                "author_handle": "@LasiZwe",
                "platform": "reddit",
                "query_group": "",
            },
            # Unknown handle, should score 0
            {
                "title": "Random news",
                "text": "noise",
                "author_handle": "random_unknown",
                "platform": "web",
                "query_group": "news",
            },
        ]
    )
    out = enrich_dataframe(df, "za")
    assert out.loc[0, "creator_watchlist_tier"] == "tier_1"
    assert out.loc[0, "creator_watchlist_score"] == 1.0
    assert out.loc[1, "creator_watchlist_tier"] == "tier_1"
    assert out.loc[1, "creator_watchlist_score"] == 1.0
    assert out.loc[2, "creator_watchlist_tier"] == ""
    assert out.loc[2, "creator_watchlist_score"] == 0.0


def test_enrich_dataframe_watchlist_strict_match_beats_fallback():
    """When a platform-specific entry exists, that tier wins over the
    cross-platform fallback. Guards the tier semantics so tier_3 on the
    actual platform does not get silently promoted to tier_1 from
    elsewhere."""
    from src.ingestion.enrichment import enrich_dataframe

    df = pd.DataFrame(
        [
            {
                "title": "post",
                "text": "content",
                "author_handle": "focalistic",
                "platform": "tiktok",  # focalistic is tier_3 in ZA tiktok
                "query_group": "",
            }
        ]
    )
    out = enrich_dataframe(df, "za")
    # Strict (tiktok, focalistic) -> tier_3 wins. focalistic also sits in
    # tier_3 on threads/instagram so even fallback would not promote, but
    # the assertion confirms the strict lookup happens first.
    assert out.loc[0, "creator_watchlist_tier"] == "tier_3"
    assert out.loc[0, "creator_watchlist_score"] == 0.4


def test_enrich_dataframe_watchlist_tier_weights_distinguish():
    """A tier_1 creator scores higher than a tier_2 creator. Sanity guard
    on the weight schedule (1.0 / 0.7 / 0.4)."""
    from src.ingestion.enrichment import enrich_dataframe

    df = pd.DataFrame(
        [
            {
                "title": "a",
                "text": "x",
                "author_handle": "tyla",  # ZA tiktok tier_1
                "platform": "tiktok",
                "query_group": "",
            },
            {
                "title": "b",
                "text": "y",
                "author_handle": "kabzadesmall",  # ZA tiktok tier_2
                "platform": "tiktok",
                "query_group": "",
            },
        ]
    )
    out = enrich_dataframe(df, "za")
    assert out.loc[0, "creator_watchlist_score"] > out.loc[1, "creator_watchlist_score"]
    assert out.loc[0, "creator_watchlist_score"] == 1.0
    assert out.loc[1, "creator_watchlist_score"] == 0.7


def test_enrich_dataframe_watchlist_no_double_count():
    """Two distinct rows from the same creator each score 1.0 individually
    (the per-row score is binary on (creator -> tier); the aggregator
    averages across rows). Confirms we do not accidentally double-credit
    within a single row when the handle appears in multiple yaml tiers."""
    from src.ingestion.enrichment import enrich_dataframe

    df = pd.DataFrame(
        [
            {
                "title": "post 1",
                "text": "a",
                "author_handle": "tyla",
                "platform": "tiktok",
                "query_group": "",
            },
            {
                "title": "post 2",
                "text": "b",
                "author_handle": "tyla",
                "platform": "tiktok",
                "query_group": "",
            },
        ]
    )
    out = enrich_dataframe(df, "za")
    assert out.loc[0, "creator_watchlist_score"] == 1.0
    assert out.loc[1, "creator_watchlist_score"] == 1.0


def _za_slang_cfg():
    return {
        "high_signal": {"weight": 1.0, "terms": ["mzansi", "kasi", "loadshedding"]},
        "medium_signal": {"weight": 0.5, "terms": ["lekker", "howzit"]},
    }


def test_extract_slang_terms():
    cfg = _za_slang_cfg()
    assert extract_slang_terms("kasi vibes in mzansi", cfg) == ["kasi", "mzansi"]
    assert extract_slang_terms("howzit bra", cfg) == ["howzit"]


def test_score_slang_weighted():
    cfg = _za_slang_cfg()
    # One high-signal hit: 1.0 / 3 = 0.333
    assert round(score_slang("mzansi", cfg), 3) == 0.333
    # Two high + one medium = 2.5 / 3 capped at 1.0 is 0.833
    assert round(score_slang("mzansi kasi lekker", cfg), 3) == 0.833
    # Overload saturates at 1.0
    assert score_slang("mzansi kasi loadshedding lekker howzit", cfg) == 1.0


def test_score_slang_empty_cfg():
    assert score_slang("mzansi", {}) == 0.0
    assert score_slang("", _za_slang_cfg()) == 0.0


def _homonym_slang_cfg():
    return {
        "high_signal": {"weight": 1.0, "terms": ["up", "da", "veo", "gemini"]},
    }


def test_extract_slang_terms_no_substring_false_match():
    cfg = _homonym_slang_cfg()
    # "up" must not fire inside setup / opportunity
    assert extract_slang_terms("setup the opportunity", cfg) == []
    # "da" must not fire inside data / drama
    assert extract_slang_terms("the data has drama", cfg) == []
    # "veo" must not fire inside video
    assert extract_slang_terms("watch the video", cfg) == []
    # "gemini" must not fire inside geminish
    assert extract_slang_terms("that is geminish", cfg) == []


def test_extract_slang_terms_genuine_match_still_fires():
    cfg = _homonym_slang_cfg()
    # standalone tokens still match
    assert extract_slang_terms("look up", cfg) == ["up"]
    assert extract_slang_terms("gemini dropped today", cfg) == ["gemini"]


def test_extract_slang_terms_multiword_match():
    cfg = {"high_signal": {"weight": 1.0, "terms": ["soft life", "amapiano"]}}
    assert extract_slang_terms("living that soft life now", cfg) == ["soft life"]
    assert extract_slang_terms("amapiano all night", cfg) == ["amapiano"]
    # multi-word must not partial-match a longer compound
    assert extract_slang_terms("software lifeline", cfg) == []


def test_score_slang_no_substring_false_match():
    cfg = _homonym_slang_cfg()
    # none of the homonym terms should score on embedded substrings
    assert score_slang("setup the opportunity with data and video", cfg) == 0.0


def test_score_slang_genuine_marker_scores():
    cfg = {"high_signal": {"weight": 1.0, "terms": ["amapiano"]}}
    assert round(score_slang("amapiano", cfg), 3) == 0.333


def test_enrich_dataframe_happy_path():
    """Integration: real ZA config, synthetic rows."""
    from src.ingestion.enrichment import enrich_dataframe

    df = pd.DataFrame(
        [
            {
                "title": "Eskom announces load shedding in Mzansi",
                "text": "kasi residents say it's lekker nothing new",
                "author_handle": "@lasizwe",
                "platform": "tiktok",
                "query_group": "",
            },
            {
                "title": "Plain news",
                "text": "nothing remarkable",
                "author_handle": "",
                "platform": "web",
                "query_group": "news",
            },
        ]
    )
    out = enrich_dataframe(df, "za")
    assert len(out) == 2
    assert out.loc[0, "slang_score"] > 0
    assert out.loc[0, "regional_score"] > 0
    assert out.loc[0, "creator_watchlist_tier"] == "tier_1"
    assert out.loc[0, "creator_watchlist_score"] == 1.0
    assert out.loc[1, "slang_score"] == 0.0
    assert out.loc[1, "creator_watchlist_score"] == 0.0
    assert out.loc[1, "query_group"] == "news"  # preserved when already set


def test_engagement_weight_for_known_and_default():
    from src.ingestion.enrichment import engagement_weight_for

    weights = {"default": 1.0, "video/10": 0.2, "video/25": 1.0}
    assert engagement_weight_for("video/10", weights) == 0.2  # Music dampened
    assert engagement_weight_for("video/25", weights) == 1.0  # News full weight
    assert engagement_weight_for("article", weights) == 1.0  # unknown -> default
    assert engagement_weight_for("", weights) == 1.0


def test_engagement_per_day_divides_by_age():
    from datetime import datetime, timedelta, timezone

    from src.ingestion.enrichment import engagement_per_day

    now = datetime(2026, 4, 22, tzinfo=UTC)
    fresh = now - timedelta(days=1)
    old = now - timedelta(days=100)
    # Fresh: divide by 1 day
    assert engagement_per_day(10000, fresh, now=now) == 10000.0
    # Old: divide by 100 days
    assert engagement_per_day(10000, old, now=now) == 100.0
    # None published_at keeps raw
    assert engagement_per_day(500, None, now=now) == 500.0
    # Zero / negative engagement returns 0
    assert engagement_per_day(0, fresh, now=now) == 0.0


def test_enrich_applies_engagement_damping():
    """Music video with huge views gets scaled down vs a fresh vlog."""
    from datetime import datetime, timedelta, timezone

    from src.ingestion.enrichment import enrich_dataframe

    now = datetime.now(UTC)
    df = pd.DataFrame(
        [
            {
                "title": "Amapiano hit",
                "text": "",
                "author_handle": "@artist",
                "platform": "youtube",
                "query_group": "music",
                "content_type": "video/10",  # Music: weight 0.2
                "engagement_total": 1_000_000,
                "published_at": now - timedelta(days=365),
            },
            {
                "title": "Vlog about Joburg nightlife",
                "text": "",
                "author_handle": "@vlogger",
                "platform": "youtube",
                "query_group": "vlog",
                "content_type": "video/22",  # Vlog: 1.0
                "engagement_total": 50_000,
                "published_at": now - timedelta(days=1),
            },
        ]
    )
    out = enrich_dataframe(df, "za")
    # Music: 1M / 365 days = 2,739/day. x 0.2 weight = ~548
    music = out.loc[0, "engagement_weighted"]
    assert music < 1000, f"expected music dampened, got {music}"
    # Vlog: 50k / 1 day = 50k. Capped to 50k (per-day-cap). x 1.0 = 50k
    vlog = out.loc[1, "engagement_weighted"]
    assert vlog > music * 10, f"vlog should dominate music after damping: vlog={vlog} music={music}"


def test_parse_v2tone_happy():
    """avg_tone -10 -> (0.45,...); polarity 20 -> (..., 0.2)."""
    from src.ingestion.enrichment import _parse_v2tone

    tone, polarity = _parse_v2tone("-10,2,12,20,5,1,100")
    assert round(tone, 3) == 0.45
    assert round(polarity, 3) == 0.2


def test_parse_v2tone_saturates():
    """Out-of-band tone values clamp to 0..1."""
    from src.ingestion.enrichment import _parse_v2tone

    # tone 200 would be 1.5 without clamp
    tone, polarity = _parse_v2tone("200,0,0,150,0,0,0")
    assert tone == 1.0
    assert polarity == 1.0
    tone, polarity = _parse_v2tone("-500,0,0,-50,0,0,0")
    assert tone == 0.0
    assert polarity == 0.0


def test_parse_v2tone_garbage_returns_nan():
    """Missing / empty / non-numeric returns NaN so aggregation excludes."""
    import math

    from src.ingestion.enrichment import _parse_v2tone

    for bad in ("", None, "not-a-tone", "1,2,3", 42):
        tone, polarity = _parse_v2tone(bad)
        assert math.isnan(tone)
        assert math.isnan(polarity)


def test_enrich_adds_tone_cols():
    """V2Tone column flows through enrichment into tone_avg / tone_polarity."""
    import math

    from src.ingestion.enrichment import enrich_dataframe

    df = pd.DataFrame(
        [
            {
                "title": "News",
                "text": "Lagos protest",
                "author_handle": "",
                "platform": "news",
                "query_group": "news",
                "content_type": "gdelt_gkg",
                "v2tone": "-10,2,12,20,5,1,100",
            },
            {
                "title": "Other",
                "text": "random",
                "author_handle": "",
                "platform": "web",
                "query_group": "other",
                "content_type": "article",
                "v2tone": "",
            },
        ]
    )
    out = enrich_dataframe(df, "ng")
    assert round(out.loc[0, "tone_avg"], 3) == 0.45
    assert round(out.loc[0, "tone_polarity"], 3) == 0.2
    # Row 1 has empty v2tone -> NaN
    assert math.isnan(out.loc[1, "tone_avg"])
    assert math.isnan(out.loc[1, "tone_polarity"])


def test_enrich_dataframe_adds_topic_groups_per_row():
    """enrich_dataframe must set topic_groups list on every row."""
    import pandas as pd
    from src.ingestion.enrichment import enrich_dataframe

    df_in = pd.DataFrame(
        [
            {
                "source": "rss",
                "platform": "web",
                "market": "za",
                "content_type": "article",
                "query_group": "news",
                "query_term": "",
                "author_name": "",
                "author_handle": "",
                "title": "Amapiano dance challenge takes over",
                "text": "New amapiano track with dance challenge",
                "url": "https://example.com/1",
                "published_at": "",
                "views": 0,
                "likes": 0,
                "comments": 0,
                "shares": 0,
                "v2tone": "",
                "v2persons": "",
                "v2orgs": "",
                "v2locations": "",
            }
        ]
    )
    df_out = enrich_dataframe(df_in, "za")
    assert "topic_groups" in df_out.columns
    topics = df_out.iloc[0]["topic_groups"]
    assert isinstance(topics, list)
    assert "music_amapiano" in topics


def test_enrich_dataframe_empty_text_produces_empty_topic_groups():
    """Rows with no matching keywords get topic_groups = []."""
    import pandas as pd
    from src.ingestion.enrichment import enrich_dataframe

    df_in = pd.DataFrame(
        [
            {
                "source": "rss",
                "platform": "web",
                "market": "za",
                "content_type": "article",
                "query_group": "news",
                "query_term": "",
                "author_name": "",
                "author_handle": "",
                "title": "Pelle glow e capelli perfetti",
                "text": "",
                "url": "https://example.com/1",
                "published_at": "",
                "views": 0,
                "likes": 0,
                "comments": 0,
                "shares": 0,
                "v2tone": "",
                "v2persons": "",
                "v2orgs": "",
                "v2locations": "",
            }
        ]
    )
    df_out = enrich_dataframe(df_in, "za")
    assert df_out.iloc[0]["topic_groups"] == []


# --- SEARCHVEL revive (SEARCH_VELOCITY_ENABLED) ----------------------------


def _searchvel_rows() -> pd.DataFrame:
    """One bigquery_trends row carrying a real search_velocity_score plus one
    non-trends (RSS) row that arrives without the column populated."""
    return pd.DataFrame(
        [
            {
                "source": "Google Trends",
                "platform": "google_search",
                "market": "za",
                "content_type": "search_term",
                "query_group": "search_intent",
                "author_handle": "",
                "title": "load shedding stage 6",
                "text": "",
                "search_velocity_score": 0.45,
            },
            {
                "source": "rss",
                "platform": "web",
                "market": "za",
                "content_type": "article",
                "query_group": "news",
                "author_handle": "",
                "title": "Plain news",
                "text": "nothing remarkable",
                # No search_velocity_score: NaN once the frame is built.
            },
        ]
    )


def test_enrich_search_velocity_flag_off_zeros_incoming(monkeypatch):
    """Flag OFF (default): a row carrying a nonzero search_velocity_score is
    still hard-zeroed, preserving today's byte-identical behavior."""
    monkeypatch.delenv("SEARCH_VELOCITY_ENABLED", raising=False)
    from src.ingestion.enrichment import enrich_dataframe

    out = enrich_dataframe(_searchvel_rows(), "za")
    assert out.loc[0, "search_velocity_score"] == 0.0
    assert out.loc[1, "search_velocity_score"] == 0.0


def test_enrich_search_velocity_flag_on_preserves_trends_row(monkeypatch):
    """Flag ON: a bigquery_trends row keeps its real search_velocity_score
    while a non-trends row (no incoming value) stays 0.0."""
    monkeypatch.setenv("SEARCH_VELOCITY_ENABLED", "true")
    from src.ingestion.enrichment import enrich_dataframe

    out = enrich_dataframe(_searchvel_rows(), "za")
    assert out.loc[0, "search_velocity_score"] == 0.45
    assert out.loc[1, "search_velocity_score"] == 0.0


def test_enrich_search_velocity_flag_on_missing_column_stays_zero(monkeypatch):
    """Flag ON but the frame has no search_velocity_score column at all (the
    common case when no bigquery_trends rows are present): every row is 0.0,
    no KeyError. KE behaves this way naturally (zero Google Trends coverage)."""
    monkeypatch.setenv("SEARCH_VELOCITY_ENABLED", "true")
    from src.ingestion.enrichment import enrich_dataframe

    df = pd.DataFrame(
        [
            {
                "title": "Sheng news",
                "text": "mtaani hustle",
                "author_handle": "",
                "platform": "web",
                "query_group": "news",
            }
        ]
    )
    out = enrich_dataframe(df, "ke")
    assert out.loc[0, "search_velocity_score"] == 0.0


def test_search_velocity_on_helper_truthy_set(monkeypatch):
    """_search_velocity_on mirrors the _language_guard_on truthy set."""
    from src.ingestion.enrichment import _search_velocity_on

    monkeypatch.delenv("SEARCH_VELOCITY_ENABLED", raising=False)
    assert _search_velocity_on() is False
    for val in ("1", "true", "TRUE", "Yes", "  yes  "):
        monkeypatch.setenv("SEARCH_VELOCITY_ENABLED", val)
        assert _search_velocity_on() is True
    for val in ("0", "false", "off", ""):
        monkeypatch.setenv("SEARCH_VELOCITY_ENABLED", val)
        assert _search_velocity_on() is False


# --- Embedding rescue residual: skip non-conversational rows ---------------


class _RecordingClassifier:
    """Stub embedding classifier that records the batch it is handed and
    classifies nothing, so a test can assert WHICH rows entered the embed batch.
    """

    def __init__(self):
        self.seen: list[str] = []

    def classify_batch(self, texts, _market):
        self.seen = list(texts)
        return [[] for _ in texts]


def test_embedding_rescue_skips_gdelt_and_aggregate_rows(monkeypatch):
    """gdelt_gkg (machine theme codes) and platform=='aggregate' (Brand24
    rollup) rows are excluded from the embed batch, matching the keyword layer's
    should_skip_content_type. A normal conversational unclassified row still
    enters the batch.
    """
    import src.ingestion.enrichment as enr

    recorder = _RecordingClassifier()
    monkeypatch.setattr(enr, "embedding_is_enabled", lambda: True)
    monkeypatch.setattr(enr, "_get_embedding_classifier", lambda: recorder)

    df = pd.DataFrame(
        [
            {
                "title": "GKG codes",
                "text": "WB_1234 TAX_FNCACT ECON_STOCKMARKET",
                "author_handle": "",
                "platform": "news",
                "query_group": "",
                "content_type": "gdelt_gkg",
            },
            {
                "title": "rollup",
                "text": "brand24 daily metric blob qwxzpf",
                "author_handle": "",
                "platform": "aggregate",
                "query_group": "",
                "content_type": "brand24_daily_metric",
            },
            {
                "title": "random chatter",
                "text": "qwxzpf zzblarg nothing topical here at all",
                "author_handle": "",
                "platform": "tiktok",
                "query_group": "",
                "content_type": "post",
            },
        ]
    )
    enr.enrich_dataframe(df, "za")

    # Only the conversational row reached the embed batch.
    assert any("qwxzpf zzblarg" in t for t in recorder.seen)
    assert not any("WB_1234" in t for t in recorder.seen)
    assert not any("daily metric blob" in t for t in recorder.seen)
    assert len(recorder.seen) == 1
