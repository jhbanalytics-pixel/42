"""Unit tests for scripts/engine_pulse.py.

Focused on the pure functions: bucketing, verdict combination, flag-state
extraction, condition evaluation, action selection. BQ pulls are mocked
via dependency injection at build_pulse boundary so no live BQ needed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch


def _load_pulse_module():
    """Side-load scripts/engine_pulse.py without adding it to the package.

    Registers the module in sys.modules before exec_module() so dataclasses'
    `_is_type` lookup against `sys.modules[cls.__module__]` finds it. Without
    that registration, decorating a class with @dataclass under a side-loaded
    module raises AttributeError on Python 3.11+.
    """
    if "engine_pulse" in sys.modules:
        return sys.modules["engine_pulse"]
    root = Path(__file__).resolve().parent.parent.parent
    spec = importlib.util.spec_from_file_location(
        "engine_pulse",
        root / "scripts" / "engine_pulse.py",
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["engine_pulse"] = mod
    spec.loader.exec_module(mod)
    return mod


pulse = _load_pulse_module()


# ----------------------------------------------------------------------
# bucket_for


def test_bucket_clean_when_above_warn():
    assert pulse._bucket_for(85.0, warn=70, critical=40) == "CLEAN"
    assert pulse._bucket_for(70.0, warn=70, critical=40) == "CLEAN"


def test_bucket_warn_when_between_warn_and_critical():
    assert pulse._bucket_for(50.0, warn=70, critical=40) == "WARN"
    assert pulse._bucket_for(40.0, warn=70, critical=40) == "WARN"


def test_bucket_critical_when_below_critical():
    assert pulse._bucket_for(10.0, warn=70, critical=40) == "CRITICAL"


def test_bucket_fail_when_zero_and_critical_positive():
    assert pulse._bucket_for(0.0, warn=70, critical=40) == "FAIL"


def test_bucket_clean_when_zero_and_critical_zero():
    """Spotify pattern: critical_pct=0 means an expected-zero source.

    Should never report FAIL because the source isn't expected to fire.
    """
    assert pulse._bucket_for(0.0, warn=70, critical=0) == "CLEAN"


# ----------------------------------------------------------------------
# verdict_for


def _conn(name: str, bucket: str) -> pulse.ConnectorPulse:
    return pulse.ConnectorPulse(
        name=name,
        expected_per_day=100,
        actual_per_day=50,
        per_market_expected={},
        per_market_actual={},
        warn_pct=70,
        critical_pct=40,
        utilisation_pct=50.0,
        bucket=bucket,
    )


def test_verdict_clean_when_all_clean():
    connectors = [_conn("a", "CLEAN"), _conn("b", "CLEAN")]
    assert pulse._verdict_for(connectors, briefs_actual=24, briefs_expected=24) == "CLEAN"


def test_verdict_minor_when_one_or_two_warn():
    connectors = [_conn("a", "WARN"), _conn("b", "CLEAN")]
    assert pulse._verdict_for(connectors, briefs_actual=24, briefs_expected=24) == "MINOR"


def test_verdict_degraded_when_three_or_more_warn():
    connectors = [_conn("a", "WARN"), _conn("b", "WARN"), _conn("c", "WARN")]
    assert pulse._verdict_for(connectors, briefs_actual=24, briefs_expected=24) == "DEGRADED"


def test_verdict_degraded_when_any_critical():
    connectors = [_conn("a", "CRITICAL"), _conn("b", "CLEAN")]
    assert pulse._verdict_for(connectors, briefs_actual=24, briefs_expected=24) == "DEGRADED"


def test_verdict_fail_when_any_fail_bucket():
    connectors = [_conn("a", "FAIL"), _conn("b", "CLEAN")]
    assert pulse._verdict_for(connectors, briefs_actual=24, briefs_expected=24) == "FAIL"


def test_verdict_fail_when_briefs_below_75pct():
    """18 / 24 briefs (75%) is at the cutoff, below should FAIL."""
    connectors = [_conn("a", "CLEAN")]
    assert pulse._verdict_for(connectors, briefs_actual=17, briefs_expected=24) == "FAIL"


def test_verdict_degraded_when_briefs_below_90pct_not_75pct():
    """22 / 24 (91.7%) clean; 21 / 24 (87.5%) degrades."""
    connectors = [_conn("a", "CLEAN")]
    assert pulse._verdict_for(connectors, briefs_actual=21, briefs_expected=24) == "DEGRADED"


def test_verdict_degraded_when_daily_summary_tokens_below_minimum():
    """Tokens below the configured floor mean the Gemini fallback fired."""
    connectors = [_conn("a", "CLEAN")]
    verdict = pulse._verdict_for(
        connectors,
        briefs_actual=24,
        briefs_expected=24,
        daily_summary_tokens=42,
        daily_summary_min_tokens=100,
    )
    assert verdict == "DEGRADED"


def test_verdict_clean_when_daily_summary_tokens_above_minimum():
    """A real Gemini brief (tokens above floor) keeps the verdict CLEAN."""
    connectors = [_conn("a", "CLEAN")]
    verdict = pulse._verdict_for(
        connectors,
        briefs_actual=24,
        briefs_expected=24,
        daily_summary_tokens=500,
        daily_summary_min_tokens=100,
    )
    assert verdict == "CLEAN"


def test_verdict_unaffected_when_token_check_not_supplied():
    """Default call without token args behaves exactly as before."""
    connectors = [_conn("a", "CLEAN")]
    assert pulse._verdict_for(connectors, briefs_actual=24, briefs_expected=24) == "CLEAN"


# ----------------------------------------------------------------------
# flag_state_from_sources


def test_flag_state_brand24_mentions_false_when_any_false():
    sources = {
        "brand24": {
            "za": {"projects": [{"id": "1", "include_mentions": False}]},
            "ng": {"projects": [{"id": "2", "include_mentions": True}]},
            "ke": {"projects": [{"id": "3", "include_mentions": True}]},
        }
    }
    assert pulse._flag_state_from_sources(sources)["include_mentions_false"] is True


def test_flag_state_brand24_mentions_clean_when_all_true():
    sources = {
        "brand24": {
            "za": {"projects": [{"id": "1", "include_mentions": True}]},
            "ng": {"projects": [{"id": "2", "include_mentions": True}]},
            "ke": {"projects": [{"id": "3", "include_mentions": True}]},
        }
    }
    assert pulse._flag_state_from_sources(sources)["include_mentions_false"] is False


def test_flag_state_twitter_handles_false_when_any_market_false():
    """Wave 1 patch (28 May 2026): only twitter_handles is a real EnsembleData
    path. The legacy twitter_keywords / twitter_hashtags markers were dropped.
    """
    sources = {
        "ensembledata": {
            "za": {"twitter_handles_enabled": False},
            "ng": {"twitter_handles_enabled": True},
            "ke": {"twitter_handles_enabled": True},
        }
    }
    assert pulse._flag_state_from_sources(sources)["twitter_handles_enabled_false"] is True


def test_flag_state_gdelt_events_default_disabled():
    sources = {"gdelt": {}}
    assert pulse._flag_state_from_sources(sources)["gdelt_events_enabled_false"] is True


def test_flag_state_brand24_mentions_sentiment_false_default():
    sources = {"brand24": {"za": {"projects": [{"id": "1"}]}}}
    assert pulse._flag_state_from_sources(sources)["include_mentions_sentiment_false"] is True


def test_flag_state_brand24_mentions_sentiment_clean_when_all_true():
    sources = {
        "brand24": {
            "za": {"projects": [{"id": "1", "include_mentions_sentiment": True}]},
            "ng": {"projects": [{"id": "2", "include_mentions_sentiment": True}]},
            "ke": {"projects": [{"id": "3", "include_mentions_sentiment": True}]},
        }
    }
    assert pulse._flag_state_from_sources(sources)["include_mentions_sentiment_false"] is False


def test_flag_state_brand24_mentions_reach_false_default():
    sources = {"brand24": {"za": {"projects": [{"id": "1"}]}}}
    assert pulse._flag_state_from_sources(sources)["include_mentions_reach_false"] is True


def test_flag_state_brand24_daily_metrics_false_default():
    sources = {"brand24": {"za": {"projects": [{"id": "1"}]}}}
    assert pulse._flag_state_from_sources(sources)["include_daily_metrics_false"] is True


def test_flag_state_bq_top_terms_disabled_default():
    sources = {"bigquery_trends": {}}
    assert pulse._flag_state_from_sources(sources)["bq_top_terms_disabled"] is True


def test_flag_state_bq_top_terms_disabled_when_flag_true():
    sources = {"bigquery_trends": {"top_terms": {"enabled": True}}}
    assert pulse._flag_state_from_sources(sources)["bq_top_terms_disabled"] is False


def test_flag_state_playlist_items_disabled_default():
    sources = {"youtube_queries": {"za": {}, "ng": {}, "ke": {}}}
    assert pulse._flag_state_from_sources(sources)["playlist_items_disabled"] is True


def test_flag_state_playlist_items_disabled_when_any_market_false():
    sources = {
        "youtube_queries": {
            "za": {"playlist_items_enabled": True},
            "ng": {"playlist_items_enabled": False},
            "ke": {"playlist_items_enabled": True},
        }
    }
    assert pulse._flag_state_from_sources(sources)["playlist_items_disabled"] is True


def test_flag_state_playlist_items_clean_when_all_true():
    sources = {
        "youtube_queries": {
            "za": {"playlist_items_enabled": True},
            "ng": {"playlist_items_enabled": True},
            "ke": {"playlist_items_enabled": True},
        }
    }
    assert pulse._flag_state_from_sources(sources)["playlist_items_disabled"] is False


def test_flag_state_spotify_default_disabled():
    sources = {"spotify": {}}
    assert pulse._flag_state_from_sources(sources)["spotify_disabled_no_secrets"] is True


# ----------------------------------------------------------------------
# eval_condition


def test_condition_utilisation_below_threshold_matches():
    util = {"brand24": 70.0}
    flags = {"include_mentions_false": True}
    cond = "brand24_utilisation_below_90 AND include_mentions_false"
    assert pulse._eval_condition(cond, util, flags) is True


def test_condition_utilisation_above_threshold_does_not_match():
    util = {"brand24": 95.0}
    flags = {"include_mentions_false": True}
    cond = "brand24_utilisation_below_90 AND include_mentions_false"
    assert pulse._eval_condition(cond, util, flags) is False


def test_condition_unknown_flag_returns_false():
    util = {"brand24": 70.0}
    flags = {}  # missing the flag
    cond = "brand24_utilisation_below_90 AND something_unknown"
    assert pulse._eval_condition(cond, util, flags) is False


def test_condition_unknown_connector_returns_false_fail_closed():
    """A connector absent from util means we have no reading, not zero spend.

    Defaulting to 0.0 made a misspelled connector always satisfy
    '<connector>_utilisation_below_N', contradicting the fail-closed contract.
    An unknown connector must evaluate the term False.
    """
    util = {}
    flags = {"include_mentions_false": True}
    cond = "unknown_utilisation_below_90 AND include_mentions_false"
    assert pulse._eval_condition(cond, util, flags) is False


def test_condition_empty_string_returns_false():
    assert pulse._eval_condition("", {}, {}) is False


def test_condition_non_string_returns_false():
    assert pulse._eval_condition(None, {}, {}) is False  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# select_action


def test_select_action_returns_highest_priority_match():
    """High-priority rule wins when multiple match."""
    capacity = {
        "auto_suggest": [
            {
                "id": "low",
                "condition": "brand24_utilisation_below_90 AND include_mentions_false",
                "priority": "low",
                "action": "low priority {pct}",
            },
            {
                "id": "high",
                "condition": "brand24_utilisation_below_90 AND include_mentions_false",
                "priority": "high",
                "action": "high priority {pct}",
            },
        ]
    }
    util = {"brand24": 60.0}
    flags = {"include_mentions_false": True}
    action = pulse._select_action(capacity, util, flags)
    assert action.startswith("high priority")


def test_select_action_returns_no_action_when_none_match():
    capacity = {
        "auto_suggest": [
            {
                "condition": "brand24_utilisation_below_90 AND include_mentions_false",
                "priority": "high",
                "action": "would fire {pct}",
            }
        ]
    }
    util = {"brand24": 99.0}  # above threshold
    flags = {}
    action = pulse._select_action(capacity, util, flags)
    assert "no action" in action.lower()


def test_select_action_formats_pct_placeholder():
    capacity = {
        "auto_suggest": [
            {
                "condition": "brand24_utilisation_below_90 AND include_mentions_false",
                "priority": "high",
                "action": "Brand24 at {pct}%",
            }
        ]
    }
    util = {"brand24": 60.0}
    flags = {"include_mentions_false": True}
    action = pulse._select_action(capacity, util, flags)
    assert "%" in action
    assert "60" in action or "{pct}" not in action  # placeholder resolved


# ----------------------------------------------------------------------
# format_action


def test_format_action_uses_named_connector_util_not_average():
    """{pct} must reflect the matched rule's own connector, never a blend.

    Regression guard for the 29 May misfire: GDELT at 13% rendered as 154%
    because _format_action averaged every connector's util (brand24 756%,
    rss 126%, etc.). The placeholder now takes the connector parsed from the
    rule condition.
    """
    util = {"gdelt": 13.0, "brand24": 756.0, "rss": 126.0}
    out = pulse._format_action("GDELT at {pct}% of expected", util, connector="gdelt")
    assert out == "GDELT at 13% of expected"
    # No blended average leaks in
    assert "154" not in out
    assert "298" not in out  # mean of the three


def test_format_action_drops_placeholder_when_connector_unknown():
    """No reliable per-connector figure: strip the fragment, don't fake a number."""
    util = {"gdelt": 13.0}
    out = pulse._format_action("GDELT at {pct}% of expected", util, connector=None)
    assert "{pct}" not in out
    assert "%" not in out  # the " at {pct}% of expected" fragment is removed cleanly


def test_connector_from_condition_extracts_connector():
    assert pulse._connector_from_condition("gdelt_utilisation_below_75 AND x_false") == "gdelt"
    assert pulse._connector_from_condition("brand24_utilisation_below_90") == "brand24"
    assert pulse._connector_from_condition("include_mentions_false") is None


def test_format_action_passes_through_when_no_placeholder():
    util = {"a": 50.0}
    out = pulse._format_action("no placeholder here", util)
    assert out == "no placeholder here"


# ----------------------------------------------------------------------
# build_pulse end-to-end (mocked BQ)


def test_build_pulse_smoke_with_mocked_bq():
    """build_pulse wires capacity + BQ + sources into a sensible report."""
    fake_per_source = {
        "rss": {"za": 100, "ng": 100, "ke": 50},
        # ensembledata + reddit retired 23 Jul 2026: both declare expected 0
        # and report 0, so neither contributes to the verdict any more.
        "ensembledata": {"za": 0, "ng": 0, "ke": 0},
        "brand24": {"za": 95, "ng": 90, "ke": 85},
        "reddit": {"za": 0, "ng": 0, "ke": 0},
        "youtube": {"za": 140, "ng": 130, "ke": 130},
        "gdelt": {
            "za": 1100,
            "ng": 1300,
            "ke": 550,
        },  # near the recalibrated expected (live medians)
        "bigquery_trends": {"za": 100, "ng": 100, "ke": 0},
        # Apple Music added 28 May 2026 (commit 66abb7a). 50 per market = 150/day target.
        "apple_music": {"za": 50, "ng": 50, "ke": 50},
        # SocialCrawl replaced EnsembleData as the primary social vendor 23 Jul 2026.
        "socialcrawl": {"za": 1050, "ng": 1000, "ke": 950},
    }
    fake_briefs = {"za": 8, "ng": 8, "ke": 8}
    fake_tokens = 500

    with patch.object(
        pulse,
        "_bq_row_counts",
        return_value=(fake_per_source, fake_briefs, fake_tokens),
    ):
        result = pulse.build_pulse("2026-05-28")

    assert isinstance(result, pulse.EnginePulse)
    assert result.trend_date == "2026-05-28"
    assert result.briefs_actual == 24
    assert result.daily_summary_tokens == 500
    # Verdict should be CLEAN or MINOR (most connectors at expected; spotify 0/300
    # with critical_pct=0 = CLEAN).
    assert result.verdict in {"CLEAN", "MINOR"}
    # At least the 7 declared connectors are present (spotify has critical=0).
    names = {c.name for c in result.connectors}
    assert {
        "rss",
        "ensembledata",
        "brand24",
        "reddit",
        "youtube",
        "gdelt",
        "bigquery_trends",
    } <= names


def test_bq_row_counts_query_includes_apple_music_column():
    """Day-1 guard: SQL must SELECT apple_music_rows so first-fire 28 May reports correctly.

    Without this, engine_pulse reports apple_music actual=0 and buckets FAIL,
    producing a false DEGRADED morning-check verdict.
    """
    import inspect

    src = inspect.getsource(pulse._bq_row_counts)
    assert "apple_music_rows" in src, "missing apple_music_rows in pipeline_runs SELECT"
    assert "apple_music" in src, "apple_music key missing from per_source dict"


def test_bq_row_counts_query_dedupes_to_latest_run_per_market():
    """A killed run plus a rerun must not double-count ingestion.

    The SQL has to collapse to one run per market per day (latest by
    started_at) so a same-day rerun does not inflate row counts.
    """
    import inspect

    src = inspect.getsource(pulse._bq_row_counts)
    assert "ROW_NUMBER" in src or "QUALIFY" in src, (
        "SQL must dedupe to the latest run per market, not SUM all runs for the date"
    )
    assert "PARTITION BY" in src, "latest-run dedup needs a per-market partition"


# ----------------------------------------------------------------------
# render_text


def test_render_text_contains_all_blocks():
    p = pulse.EnginePulse(trend_date="2026-05-28")
    p.connectors = [_conn("rss", "CLEAN"), _conn("brand24", "WARN")]
    p.briefs_actual = 24
    p.briefs_expected = 24
    p.daily_summary_tokens = 500
    p.verdict = "MINOR"
    p.recommended_action = "Flip include_mentions: true"

    text = pulse.render_text(p)
    assert "ENGINE PULSE 2026-05-28" in text
    assert "VERDICT: MINOR" in text
    assert "PER-CONNECTOR UTILISATION" in text
    assert "rss" in text
    assert "brand24" in text
    assert "PHASE 2  briefs=24 / 24" in text
    assert "RECOMMENDED ACTION" in text
    assert "Flip include_mentions: true" in text


# ----------------------------------------------------------------------
# Capacity YAML round-trip


def test_capacity_yaml_loads_cleanly():
    capacity = pulse._load_capacity()
    assert isinstance(capacity, dict)
    assert "connectors" in capacity
    assert "auto_suggest" in capacity
    # All seven core connectors must be declared.
    for required in (
        "rss",
        "ensembledata",
        "brand24",
        "reddit",
        "youtube",
        "gdelt",
        "bigquery_trends",
    ):
        assert required in capacity["connectors"], f"missing {required} in engine_capacity.yaml"


def test_capacity_yaml_auto_suggest_rules_have_required_fields():
    capacity = pulse._load_capacity()
    rules = capacity.get("auto_suggest") or []
    assert rules, "auto_suggest list is empty"
    for rule in rules:
        assert "condition" in rule
        assert "priority" in rule
        assert "action" in rule
        assert rule["priority"] in {"high", "medium", "low"}


def test_capacity_yaml_has_no_unfireable_auto_suggest_rules():
    """Every shipped rule must have a condition _eval_condition can satisfy.

    A rule whose condition is neither a `<connector>_utilisation_below_N` term
    nor a known flag / quota gate can never match, so it is dead config. The
    two escalation rules (unclassified_rate_above_60,
    github_actions_minutes_above_1800) were exactly that and were removed; this
    guard stops a non-firing rule from creeping back in.
    """
    # The universe of flag names the evaluator understands: the source-derived
    # flags plus the quota gates. Built from the real functions so the test
    # tracks the evaluator instead of a hand-maintained copy.
    known_flags = set(pulse._flag_state_from_sources({}))
    known_flags |= set(pulse._quota_flags(None))

    capacity = pulse._load_capacity()
    for rule in capacity.get("auto_suggest") or []:
        cond = rule.get("condition", "")
        terms = [t.strip() for t in cond.split("AND")]
        fireable = any(("_utilisation_below_" in t) or (t in known_flags) for t in terms)
        assert fireable, f"rule {rule.get('id')!r} has an unfireable condition: {cond!r}"


def test_capacity_yaml_dropped_dead_escalation_rules():
    capacity = pulse._load_capacity()
    ids = {r.get("id") for r in capacity.get("auto_suggest") or []}
    assert "m4_embeddings_carla_escalation" not in ids
    assert "cloud_run_migration_escalation" not in ids


# ----------------------------------------------------------------------
# Wave 3 EnsembleData dark surfaces (13 new markers)


WAVE3_FLAG_TO_MARKER = [
    # YouTube via EnsembleData (4)
    ("yt_shorts_enabled", "yt_shorts_disabled"),
    ("yt_video_comments_enabled", "yt_video_comments_disabled"),
    ("yt_channel_videos_enabled", "yt_channel_videos_disabled"),
    ("yt_keyword_search_enabled", "yt_keyword_search_disabled"),
    # Instagram (3)
    ("ig_post_comments_enabled", "ig_post_comments_disabled"),
    ("ig_user_reels_enabled", "ig_user_reels_disabled"),
    ("ig_user_tagged_posts_enabled", "ig_user_tagged_posts_disabled"),
    # Threads (1)
    ("threads_post_replies_enabled", "threads_post_replies_disabled"),
    # TikTok (3)
    ("tt_music_posts_enabled", "tt_music_posts_disabled"),
    ("tt_post_comment_replies_enabled", "tt_post_comment_replies_disabled"),
    ("tt_post_info_enabled", "tt_post_info_disabled"),
]


def _single_market_off_sources(flag: str) -> dict:
    """Minimal sources_yaml: flag explicitly false on ZA, true on NG + KE."""
    return {
        "ensembledata": {
            "za": {flag: False},
            "ng": {flag: True},
            "ke": {flag: True},
        }
    }


def _all_markets_on_sources(flag: str) -> dict:
    return {
        "ensembledata": {
            "za": {flag: True},
            "ng": {flag: True},
            "ke": {flag: True},
        }
    }


def test_wave3_markers_fire_when_any_market_false():
    """13 Wave 3 dark surfaces. Each marker fires when ANY market has flag false."""
    for flag, marker in WAVE3_FLAG_TO_MARKER:
        state = pulse._flag_state_from_sources(_single_market_off_sources(flag))
        assert state[marker] is True, f"{marker} should fire when {flag} false on ZA"


def test_wave3_markers_clean_when_all_markets_true():
    for flag, marker in WAVE3_FLAG_TO_MARKER:
        state = pulse._flag_state_from_sources(_all_markets_on_sources(flag))
        assert state[marker] is False, f"{marker} should be clean when {flag} true everywhere"


def test_wave3_markers_default_true_when_flag_absent():
    """Missing flag treated as disabled (the dark-surface default)."""
    state = pulse._flag_state_from_sources({"ensembledata": {"za": {}, "ng": {}, "ke": {}}})
    for _flag, marker in WAVE3_FLAG_TO_MARKER:
        assert state[marker] is True, f"{marker} should default True when flag absent"


# ----------------------------------------------------------------------
# Customer endpoint integration (free quota dashboard)


def test_compute_ensembledata_quota_state_returns_none_when_token_none():
    assert pulse._compute_ensembledata_quota_state(None) is None
    assert pulse._compute_ensembledata_quota_state("") is None


def test_compute_ensembledata_quota_state_happy_path(monkeypatch):
    """Mocks the customer_units module to validate the dict shape."""
    fake_today = {"tiktok": 800, "instagram": 200, "youtube": 50, "total": 1050}
    fake_history = [
        {"date": "2026-05-14", "total": 1100},
        {"date": "2026-05-15", "total": 900},
        {"date": "2026-05-16", "total": 1000},
    ]

    import types

    fake_mod = types.ModuleType("src.ingestion.connectors.customer_units")
    fake_mod.fetch_used_units = lambda token, d: fake_today
    fake_mod.fetch_units_history = lambda token, days: fake_history
    monkeypatch.setitem(sys.modules, "src.ingestion.connectors.customer_units", fake_mod)

    result = pulse._compute_ensembledata_quota_state("fake-token", "2026-05-28")
    assert result is not None
    assert result["today_total"] == 1050
    assert result["cap"] == pulse.ENSEMBLEDATA_DAILY_CAP
    assert result["headroom_today"] == pulse.ENSEMBLEDATA_DAILY_CAP - 1050
    assert result["today_used"] == {"tiktok": 800, "instagram": 200, "youtube": 50}
    assert result["rolling_avg_per_day"] == 1000.0


def test_compute_ensembledata_quota_state_returns_none_when_both_fetches_empty(monkeypatch):
    """A vendor outage returns empty from both fetches. That is unknown, not zero.

    Synthesising today_used=0 / full headroom from empty responses is
    indistinguishable from genuine zero spend and would silently mask an
    outage. Return None (unknown) instead.
    """
    import types

    fake_mod = types.ModuleType("src.ingestion.connectors.customer_units")
    fake_mod.fetch_used_units = lambda token, d: {}
    fake_mod.fetch_units_history = lambda token, days: []
    monkeypatch.setitem(sys.modules, "src.ingestion.connectors.customer_units", fake_mod)

    assert pulse._compute_ensembledata_quota_state("fake-token", "2026-05-28") is None


def test_compute_ensembledata_quota_state_returns_snapshot_when_only_history(monkeypatch):
    """History alone is a real reading, so a snapshot is still produced."""
    import types

    fake_mod = types.ModuleType("src.ingestion.connectors.customer_units")
    fake_mod.fetch_used_units = lambda token, d: {}
    fake_mod.fetch_units_history = lambda token, days: [{"date": "2026-05-14", "total": 1100}]
    monkeypatch.setitem(sys.modules, "src.ingestion.connectors.customer_units", fake_mod)

    result = pulse._compute_ensembledata_quota_state("fake-token", "2026-05-28")
    assert result is not None
    assert result["rolling_avg_per_day"] == 1100.0


def test_compute_ensembledata_quota_state_returns_none_on_exception(monkeypatch):
    """Defensive: never crash the watchdog on a Customer endpoint blip."""
    import types

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated network failure")

    fake_mod = types.ModuleType("src.ingestion.connectors.customer_units")
    fake_mod.fetch_used_units = _boom
    fake_mod.fetch_units_history = _boom
    monkeypatch.setitem(sys.modules, "src.ingestion.connectors.customer_units", fake_mod)

    assert pulse._compute_ensembledata_quota_state("fake-token") is None


def test_quota_flags_none_when_quota_none():
    flags = pulse._quota_flags(None)
    assert flags["ensembledata_quota_headroom_below_500"] is False
    assert flags["ensembledata_quota_headroom_below_1500"] is False


def test_quota_flags_below_1500_only_when_headroom_between_500_and_1500():
    flags = pulse._quota_flags({"headroom_today": 1000})
    assert flags["ensembledata_quota_headroom_below_500"] is False
    assert flags["ensembledata_quota_headroom_below_1500"] is True


def test_quota_flags_below_500_implies_below_1500():
    flags = pulse._quota_flags({"headroom_today": 200})
    assert flags["ensembledata_quota_headroom_below_500"] is True
    assert flags["ensembledata_quota_headroom_below_1500"] is True


def test_quota_flags_clean_when_headroom_high():
    flags = pulse._quota_flags({"headroom_today": 3000})
    assert flags["ensembledata_quota_headroom_below_500"] is False
    assert flags["ensembledata_quota_headroom_below_1500"] is False


# ----------------------------------------------------------------------
# CONNECTOR-ZERO check (the 3-Jun bad-key signature)
#
# A connector is hard-flagged when it is ENABLED in sources.yaml AND has a
# nonzero capacity floor AND is not on the may-be-zero allow-list AND returned
# exactly 0 rows. Distinct from the utilisation buckets (which WARN on
# under-volume); this is the hard "an enabled high-volume connector flat-lined
# on a success run" alarm.


def _zero_conn(
    name: str,
    actual: int,
    *,
    expected: int = 1700,
    critical: int = 35,
) -> pulse.ConnectorPulse:
    """ConnectorPulse builder with controllable actual / expected / critical.

    The shared `_conn` helper hardcodes actual=50, which cannot exercise the
    zero path, so the connector-zero tests use this one.
    """
    util = (actual / expected * 100.0) if expected else 0.0
    return pulse.ConnectorPulse(
        name=name,
        expected_per_day=expected,
        actual_per_day=actual,
        per_market_expected={},
        per_market_actual={},
        warn_pct=65,
        critical_pct=critical,
        utilisation_pct=util,
        bucket="FAIL" if actual == 0 else "CLEAN",
    )


# All three SSA markets carry a non-empty brand24 projects list, so brand24 is
# ENABLED. reddit + gdelt enabled via their real master switches.
_ENABLED_SOURCES = {
    "reddit": {"enabled": True},
    "gdelt": {"active": True},
    "brand24": {"za": {"projects": [{"id": "1"}]}},
    "ensemble_budget": {"enabled": True},
    "ensembledata": {"za": {}, "ng": {}, "ke": {}},
}


def test_connector_zero_flags_enabled_high_volume_connector_at_zero():
    """reddit enabled + nonzero floor + 0 rows -> hard-flagged."""
    connectors = [
        _zero_conn("reddit", 0, expected=1700, critical=35),
        _zero_conn("brand24", 90, expected=275, critical=40),
    ]
    flagged = pulse.check_connector_zero(connectors, _ENABLED_SOURCES, may_be_zero={"gdelt"})
    assert flagged == ["reddit"]


def test_connector_zero_does_not_flag_gdelt_on_allow_list():
    """gdelt is legitimately low-or-zero some days; the allow-list exempts it."""
    connectors = [
        _zero_conn("gdelt", 0, expected=550, critical=30),
        _zero_conn("reddit", 1500, expected=1700, critical=35),
    ]
    flagged = pulse.check_connector_zero(connectors, _ENABLED_SOURCES, may_be_zero={"gdelt"})
    assert flagged == []


def test_connector_zero_clean_when_all_connectors_healthy():
    connectors = [
        _zero_conn("reddit", 1500, expected=1700, critical=35),
        _zero_conn("brand24", 250, expected=275, critical=40),
        _zero_conn("ensembledata", 2400, expected=2600, critical=30),
    ]
    flagged = pulse.check_connector_zero(connectors, _ENABLED_SOURCES, may_be_zero={"gdelt"})
    assert flagged == []


def test_connector_zero_does_not_flag_disabled_connector_at_zero():
    """A connector that is DISABLED in sources.yaml is meant to sit at 0."""
    sources = {
        "reddit": {"enabled": False},  # disabled: zero is expected
        "brand24": {"za": {"projects": [{"id": "1"}]}},
    }
    connectors = [
        _zero_conn("reddit", 0, expected=1700, critical=35),
        _zero_conn("brand24", 250, expected=275, critical=40),
    ]
    flagged = pulse.check_connector_zero(connectors, sources, may_be_zero={"gdelt"})
    assert flagged == []


def test_connector_zero_does_not_flag_expected_zero_connector():
    """Spotify-style: expected_per_day=0 / critical=0 is not a flat-line."""
    connectors = [_zero_conn("spotify", 0, expected=0, critical=0)]
    # spotify is also disabled, but the expected-zero guard alone must exempt it.
    flagged = pulse.check_connector_zero(
        connectors, {"spotify": {"enabled": True}}, may_be_zero=set()
    )
    assert flagged == []


# ----------------------------------------------------------------------
# _connector_enabled (mirrors each connector's real master switch)


def test_connector_enabled_rss_youtube_always_on():
    assert pulse._connector_enabled("rss", {}) is True
    assert pulse._connector_enabled("youtube", {}) is True


def test_connector_enabled_active_default_true():
    """bigquery_trends / gdelt / apple_music default active=True when absent."""
    for name in ("bigquery_trends", "gdelt", "apple_music"):
        assert pulse._connector_enabled(name, {}) is True
        assert pulse._connector_enabled(name, {name: {"active": False}}) is False


def test_connector_enabled_reddit_spotify_default_false():
    """reddit / spotify require an explicit enabled: true."""
    for name in ("reddit", "spotify"):
        assert pulse._connector_enabled(name, {}) is False
        assert pulse._connector_enabled(name, {name: {"enabled": True}}) is True


def test_connector_enabled_brand24_needs_projects():
    assert pulse._connector_enabled("brand24", {"brand24": {"za": {"projects": []}}}) is False
    assert (
        pulse._connector_enabled("brand24", {"brand24": {"ng": {"projects": [{"id": "1"}]}}})
        is True
    )


def test_connector_enabled_ensembledata_gated_by_budget_and_terms():
    on = {"ensemble_budget": {"enabled": True}, "ensembledata": {"za": {}}}
    assert pulse._connector_enabled("ensembledata", on) is True
    budget_off = {"ensemble_budget": {"enabled": False}, "ensembledata": {"za": {}}}
    assert pulse._connector_enabled("ensembledata", budget_off) is False
    no_terms = {"ensemble_budget": {"enabled": True}}
    assert pulse._connector_enabled("ensembledata", no_terms) is False


def test_connector_enabled_unknown_connector_defaults_true():
    """A newly declared capacity floor must not be silently exempt from the check."""
    assert pulse._connector_enabled("brand_new_connector", {}) is True


# ----------------------------------------------------------------------
# may-be-zero allow-list loader


def test_load_may_be_zero_reads_config():
    capacity = {"connector_zero_alert": {"may_be_zero": ["gdelt", "foo"]}}
    assert pulse._load_may_be_zero(capacity) == {"gdelt", "foo"}


def test_load_may_be_zero_falls_back_to_gdelt_when_absent():
    """If the config block is missing, GDELT must still be exempt by default."""
    assert pulse._load_may_be_zero({}) == {"gdelt"}
    assert pulse._load_may_be_zero({"connector_zero_alert": {}}) == {"gdelt"}


def test_capacity_yaml_exempts_no_connector_from_zero():
    """The live engine_capacity.yaml de-listed gdelt (2026-06-16): an explicit
    empty may_be_zero means no connector is exempt, so a gdelt zero is now a real
    bad-key signal, not the old default exemption."""
    capacity = pulse._load_capacity()
    assert "gdelt" not in pulse._load_may_be_zero(capacity)
    assert pulse._load_may_be_zero(capacity) == set()


# ----------------------------------------------------------------------
# bucket may_be_zero refinement (GDELT-zero is WARN, not FAIL)


def test_bucket_warn_when_zero_but_may_be_zero():
    """An allow-listed connector at 0 degrades to WARN, not FAIL.

    Fixes the pre-existing false positive where a legitimately-zero GDELT
    buckets FAIL and false-FAILs the whole verdict.
    """
    assert pulse._bucket_for(0.0, warn=60, critical=30, may_be_zero=True) == "WARN"
    # Default (not allow-listed) keeps the hard FAIL.
    assert pulse._bucket_for(0.0, warn=60, critical=30, may_be_zero=False) == "FAIL"


# ----------------------------------------------------------------------
# verdict + render wiring for connector-zero alerts


def test_verdict_fail_when_connector_zero_alert_fires():
    """A connector-zero alert forces FAIL even if every bucket is CLEAN."""
    connectors = [_conn("a", "CLEAN")]
    assert pulse._verdict_for(connectors, 24, 24, connector_zero_alerts=["reddit"]) == "FAIL"


def test_verdict_unchanged_when_no_connector_zero_alert():
    connectors = [_conn("a", "CLEAN")]
    assert pulse._verdict_for(connectors, 24, 24, connector_zero_alerts=[]) == "CLEAN"


def test_render_text_connector_zero_clean_line():
    p = pulse.EnginePulse(trend_date="2026-06-09")
    p.connectors = [_conn("rss", "CLEAN")]
    text = pulse.render_text(p)
    assert "CONNECTOR-ZERO" in text
    assert "clean" in text


def test_render_text_connector_zero_fail_line():
    p = pulse.EnginePulse(trend_date="2026-06-09")
    p.connectors = [_conn("reddit", "FAIL")]
    p.connector_zero_alerts = ["reddit"]
    p.verdict = "FAIL"
    text = pulse.render_text(p)
    assert "CONNECTOR-ZERO" in text
    assert "FAIL: reddit" in text
    assert "bad-key" in text


def test_to_dict_includes_connector_zero_alerts():
    p = pulse.EnginePulse(trend_date="2026-06-09")
    p.connector_zero_alerts = ["reddit", "brand24"]
    assert p.to_dict()["connector_zero_alerts"] == ["reddit", "brand24"]


# ----------------------------------------------------------------------
# build_pulse end-to-end: an enabled high-volume connector at 0 -> FAIL


def test_build_pulse_flags_connector_zero_end_to_end():
    """reddit enabled with 0 rows on an otherwise-healthy run -> verdict FAIL.

    This is the 3-Jun hole: a bad vendor key makes one connector return 0 while
    pipeline_runs.status stays 'success'. Since the 2026-06-16 gdelt de-list,
    gdelt at 0 in the same run also fires the alert (no connector is exempt).
    """
    fake_per_source = {
        "rss": {"za": 130, "ng": 130, "ke": 50},
        "ensembledata": {"za": 880, "ng": 880, "ke": 840},
        "brand24": {"za": 95, "ng": 95, "ke": 85},
        "reddit": {"za": 0, "ng": 0, "ke": 0},  # retired 23 Jul 2026, expected is 0
        "socialcrawl": {"za": 0, "ng": 0, "ke": 0},  # dead-key flat-line
        "youtube": {"za": 140, "ng": 140, "ke": 140},
        "gdelt": {"za": 0, "ng": 0, "ke": 0},  # de-listed: a zero is now a real alert
        "bigquery_trends": {"za": 100, "ng": 100, "ke": 0},
        "apple_music": {"za": 50, "ng": 50, "ke": 50},
    }
    fake_briefs = {"za": 8, "ng": 8, "ke": 8}

    with patch.object(pulse, "_bq_row_counts", return_value=(fake_per_source, fake_briefs, 500)):
        result = pulse.build_pulse("2026-06-09")

    assert set(result.connector_zero_alerts) == {"socialcrawl", "gdelt"}
    assert result.verdict == "FAIL"
    # gdelt at 0 now buckets FAIL (no longer allow-listed).
    gdelt = next(c for c in result.connectors if c.name == "gdelt")
    assert gdelt.bucket == "FAIL"


# ---------------------------------------------------------------------------
# CROSS-MARKET-ZERO check (15-Jun Reddit->ensemble starvation regression)
# ---------------------------------------------------------------------------


def _market_conn(name, per_market_actual, per_market_expected):
    """ConnectorPulse with explicit per-market actual/expected for the
    cross-market-zero check."""
    actual = sum(per_market_actual.values())
    expected = sum(per_market_expected.values())
    return pulse.ConnectorPulse(
        name=name,
        expected_per_day=expected,
        actual_per_day=actual,
        per_market_expected=per_market_expected,
        per_market_actual=per_market_actual,
        warn_pct=60,
        critical_pct=30,
        utilisation_pct=(actual / expected * 100.0) if expected else 0.0,
        bucket="CLEAN",
    )


def test_cross_market_zero_flags_starved_markets():
    """ensemble healthy for ZA but zero for NG+KE (all three expected non-zero)
    is the Reddit-starvation signature and must FAIL even though the aggregate
    looks fine."""
    connectors = [
        _market_conn(
            "ensembledata",
            {"za": 880, "ng": 0, "ke": 0},
            {"za": 880, "ng": 880, "ke": 840},
        )
    ]
    flagged = pulse.check_cross_market_zero(connectors, _ENABLED_SOURCES, may_be_zero={"gdelt"})
    assert len(flagged) == 1
    assert "ensembledata" in flagged[0]
    assert "ng" in flagged[0]
    assert "ke" in flagged[0]


def test_cross_market_zero_clean_when_all_markets_produce():
    connectors = [
        _market_conn(
            "ensembledata",
            {"za": 880, "ng": 870, "ke": 800},
            {"za": 880, "ng": 880, "ke": 840},
        )
    ]
    assert pulse.check_cross_market_zero(connectors, _ENABLED_SOURCES, may_be_zero={"gdelt"}) == []


def test_cross_market_zero_ignores_market_expected_zero():
    """A market legitimately expected to produce 0 (e.g. KE bigquery_trends) at
    0 while siblings produce is NOT a starvation flag."""
    connectors = [
        _market_conn(
            "bigquery_trends",
            {"za": 40, "ng": 40, "ke": 0},
            {"za": 40, "ng": 40, "ke": 0},
        )
    ]
    assert pulse.check_cross_market_zero(connectors, _ENABLED_SOURCES, may_be_zero=set()) == []


def test_cross_market_zero_does_not_flag_all_zero():
    """All markets zero is the CONNECTOR-ZERO check's job, not this one (needs a
    surviving sibling to be a per-market starvation)."""
    connectors = [
        _market_conn("ensembledata", {"za": 0, "ng": 0, "ke": 0}, {"za": 880, "ng": 880, "ke": 840})
    ]
    assert pulse.check_cross_market_zero(connectors, _ENABLED_SOURCES, may_be_zero=set()) == []


def test_cross_market_zero_fails_the_verdict():
    """A cross-market-zero alert drives the overall verdict to FAIL."""
    verdict = pulse._verdict_for(
        [],
        briefs_actual=24,
        briefs_expected=24,
        connector_zero_alerts=[],
        cross_market_zero_alerts=["ensembledata (zero: ng, ke; live: za)"],
    )
    assert verdict == "FAIL"


# ----------------------------------------------------------------------
# Retired vendor: an explicit enabled: false wins over the project blocks


_BRAND24_GATES = (
    "include_mentions_false",
    "include_ai_insights_false",
    "include_mentions_sentiment_false",
    "include_mentions_reach_false",
    "include_daily_metrics_false",
)


def test_connector_enabled_brand24_retired_switch_wins_over_projects():
    retired = {"brand24": {"enabled": False, "za": {"projects": [{"id": "1"}]}}}
    assert pulse._connector_enabled("brand24", retired) is False


def test_flag_state_retired_brand24_raises_no_suggestion_gate():
    retired = {"brand24": {"enabled": False, "za": {"projects": [{"id": "1"}]}}}
    state = pulse._flag_state_from_sources(retired)
    assert {gate: state[gate] for gate in _BRAND24_GATES} == dict.fromkeys(_BRAND24_GATES, False)


def test_shipped_sources_config_retires_brand24_for_health():
    import yaml

    sources_path = Path(__file__).resolve().parents[2] / "configs" / "sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    assert sources["brand24"]["enabled"] is False
    assert pulse._connector_enabled("brand24", sources) is False
    state = pulse._flag_state_from_sources(sources)
    assert not any(state[gate] for gate in _BRAND24_GATES)
