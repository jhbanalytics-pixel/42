"""Unit tests for seed_candidates ranker (V3 Track C)."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from src.analysis import seed_candidates as sc


def _sg_row(**kwargs):
    base = {
        "market": "za",
        "term": "newterm",
        "term_type": "hashtag",
        "platform": "tiktok",
        "trend_date": date(2026, 7, 7),
        "event_date": date(2026, 7, 5),
        "row_count": 10,
        "avg_genz_score": 0.6,
        "slang_row_share": 0.4,
        "topic_groups": ["music_amapiano"],
        "near_topics": ["music_amapiano"],
        "sample_row_ids": ["r1", "r2", "r3", "r4", "r5"],
    }
    base.update(kwargs)
    return base


def test_make_candidate_id_deterministic():
    d = date(2026, 7, 7)
    a = sc.make_candidate_id("za", "foo", "keyword", d)
    b = sc.make_candidate_id("za", "foo", "keyword", d)
    c = sc.make_candidate_id("za", "bar", "keyword", d)
    assert a == b
    assert a != c


def test_load_config_terms_includes_keywords_dual_block():
    terms = sc.load_config_terms("za")
    assert "amapiano" in terms
    assert "mzansi" in terms


def test_aggregate_weighted_mean_and_row_dedup():
    rows = [
        _sg_row(term_type="hashtag", sample_row_ids=["r1", "r2"], row_count=5, avg_genz_score=0.8),
        _sg_row(term_type="token", sample_row_ids=["r2", "r3"], row_count=5, avg_genz_score=0.4),
    ]
    agg = sc.aggregate_terms(rows, "za")
    t = agg["newterm"]
    assert t["frequency"] == 3
    assert abs(t["avg_genz_score"] - 0.6) < 0.01


def test_aggregate_topic_union_dedupes():
    rows = [
        _sg_row(topic_groups=["music_amapiano"], near_topics=["music_amapiano"]),
        _sg_row(topic_groups=["music_amapiano"], near_topics=["genz_lifestyle"]),
    ]
    agg = sc.aggregate_terms(rows, "za")["newterm"]
    assert agg["topic_groups"] == {"music_amapiano"}
    assert agg["near_topics"] == {"music_amapiano", "genz_lifestyle"}


def test_compute_c2_is_invariant_to_hot_topic_prior():
    hot = {"music_amapiano", "genz_lifestyle", "fashion_beauty"}
    base = {"newterm": 5.0}
    only_topic = {
        "term": "newterm",
        "frequency": 10,
        "topic_groups": {"music_amapiano"},
        "near_topics": set(),
        "platform_counts": {"tiktok": 10},
        "avg_genz_score": 0.5,
        "slang_row_share": 0.5,
    }
    only_near = {
        "term": "newterm",
        "frequency": 10,
        "topic_groups": set(),
        "near_topics": {"music_amapiano"},
        "platform_counts": {"tiktok": 10},
        "avg_genz_score": 0.5,
        "slang_row_share": 0.5,
    }
    score_topic_cold, fit_topic_cold = sc.compute_c2_score(only_topic, set(), base)
    score_topic_hot, fit_topic_hot = sc.compute_c2_score(only_topic, hot, base)
    score_near_cold, fit_near_cold = sc.compute_c2_score(only_near, set(), base)
    score_near_hot, fit_near_hot = sc.compute_c2_score(only_near, hot, base)
    assert score_topic_hot == score_topic_cold
    assert fit_topic_hot == fit_topic_cold
    assert score_near_hot == score_near_cold
    assert fit_near_hot == fit_near_cold
    assert fit_topic_hot["co_occur"] == 0.0
    assert fit_near_hot["co_occur"] == 0.0


def test_config_term_never_eligible_fast_lane():
    config = sc.load_config_terms("za")
    term_agg = {
        "term": "amapiano",
        "frequency": 100,
        "first_seen_event_date": date.today(),
        "topic_groups": set(),
        "near_topics": set(),
    }
    assert not sc.is_eligible(
        term_agg,
        config,
        set(),
        "fast",
        cap_remaining=5,
        trend_date=date.today(),
    )


def test_rejection_memory_blocks():
    term_agg = {
        "term": "brandnew",
        "frequency": 10,
        "first_seen_event_date": date.today(),
        "topic_groups": set(),
        "near_topics": set(),
    }
    assert not sc.is_eligible(
        term_agg,
        set(),
        {"brandnew"},
        "weekly",
        cap_remaining=5,
        trend_date=date.today(),
    )


@pytest.mark.parametrize(
    "term",
    [
        "weekend",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "rocking",
        "awesome",
        "wonderful",
        "incredible",
        "fantastic",
        "july",
        "december",
        "talented",
        "team",
        "favorite",
        "travel",
        "form",
        "power",
        "proud",
        "crazy",
        "easier",
        "artists",
        "cheap",
        "movies",
        "nation",
        "gameplay",
        "flag",
        "small",
        "custom",
        "genre",
        "cinematic",
        "beach",
    ],
)
def test_generic_calendar_and_reaction_terms_not_eligible(term):
    # These are ubiquitous English words with no SSA cultural or slang
    # specificity. They naturally co-occur with every hot topic simply by
    # being common, which let them outscore real slang in the 6 Jul run
    # (graph_score 0.79 on "weekend"/"friday" vs genz and slang both 0.0).
    # Siblings like "today", "amazing", "beautiful" were already stoplisted;
    # this closes the same gap for weekday names and their nearest neighbours.
    term_agg = {
        "term": term,
        "frequency": 50,
        "first_seen_event_date": date.today(),
        "topic_groups": {"music_amapiano", "sports_rugby"},
        "near_topics": set(),
        "market": "za",
    }
    assert not sc.is_eligible(
        term_agg,
        set(),
        set(),
        "weekly",
        cap_remaining=5,
        trend_date=date.today(),
    )


@pytest.mark.parametrize(
    "term",
    ["ucgnt5exzvbnvu7f6insehyg", "8230", "misspeters0724728587", "wb_696_public_sector_management"],
)
def test_low_quality_term_not_eligible(term):
    # Backstop: junk already persisted in seed_graph must not become a candidate
    # even before the graph is rebuilt with the extraction-time gate.
    term_agg = {
        "term": term,
        "frequency": 50,
        "first_seen_event_date": date.today(),
        "topic_groups": {"music_amapiano"},
        "near_topics": set(),
        "market": "za",
    }
    assert not sc.is_eligible(
        term_agg, set(), set(), "weekly", cap_remaining=5, trend_date=date.today()
    )


@pytest.mark.parametrize("term", ["zero", "ideas", "greater", "incredibly", "destination"])
def test_generic_english_no_signal_not_eligible(term):
    # Common English word, no genz or slang signal: noise on day one.
    term_agg = {
        "term": term,
        "frequency": 50,
        "first_seen_event_date": date.today(),
        "topic_groups": {"music_amapiano"},
        "near_topics": set(),
        "market": "za",
        "avg_genz_score": 0.0,
        "slang_row_share": 0.0,
    }
    assert not sc.is_eligible(
        term_agg, set(), set(), "weekly", cap_remaining=5, trend_date=date.today()
    )


def test_generic_english_dropped_even_with_post_signal():
    # genz/slang are post properties, not term properties: a generic word in
    # slang-heavy posts inherits a high slang share, so it is NOT an escape
    # hatch. Common English is dropped outright on day one.
    term_agg = {
        "term": "greater",
        "frequency": 50,
        "first_seen_event_date": date.today(),
        "topic_groups": {"music_amapiano"},
        "near_topics": set(),
        "market": "za",
        "avg_genz_score": 0.2,
        "slang_row_share": 0.5,
    }
    assert not sc.is_eligible(
        term_agg, set(), set(), "weekly", cap_remaining=5, trend_date=date.today()
    )


def test_ssa_vernacular_not_in_common_english_list():
    common = sc._common_english()
    assert common  # the bundled file loaded
    for real in ("mtoto", "nonini", "calabar", "naijahustle", "amapiano", "kasi", "sapa"):
        assert real not in common


def test_safety_auto_reject_studied_not_died():
    safe = sc.check_safety({"term": "studied", "topic_groups": []})
    assert not any("died" in f for f in safe)
    unsafe = sc.check_safety({"term": "he died yesterday", "topic_groups": []})
    assert any("died" in f for f in unsafe)


def test_safety_risk_signal_on_topic():
    flags = sc.check_safety({"term": "vote", "topic_groups": ["politics_election"]})
    assert any("politics" in f for f in flags)


def test_monday_weekly_runs_before_fast():
    monday = date(2026, 7, 6)
    with (
        patch.object(sc, "weekly_lane_already_ran", return_value=False),
        patch.object(sc, "run_lane", side_effect=[3, 1]) as run_lane,
    ):
        n = sc.run_seed_candidates_stage(monday)
    assert n == 4
    assert run_lane.call_args_list[0] == ((monday, "weekly"),)
    assert run_lane.call_args_list[1] == ((monday, "fast"),)


def test_non_monday_skips_weekly_runs_fast_only():
    tuesday = date(2026, 7, 7)
    with patch.object(sc, "run_lane", return_value=2) as run_lane:
        n = sc.run_seed_candidates_stage(tuesday)
    assert n == 2
    run_lane.assert_called_once_with(tuesday, "fast")


def test_weekly_skip_when_pending_weekly_exists():
    monday = date(2026, 7, 6)
    with (
        patch.object(sc, "weekly_lane_already_ran", return_value=True),
        patch.object(sc, "run_lane", return_value=1) as run_lane,
    ):
        n = sc.run_seed_candidates_stage(monday)
    assert n == 1
    run_lane.assert_called_once_with(monday, "fast")


def test_double_fire_idempotency_filters_existing_ids():
    monday = date(2026, 7, 6)
    rows = [_sg_row()]
    agg = sc.aggregate_terms(rows, "za")
    term_agg = agg["newterm"]
    score, seed_fit = sc.compute_c2_score(term_agg, {"music_amapiano"}, {})
    built = sc.build_candidate_rows(
        term_agg,
        lane="fast",
        proposed_date=monday,
        score=score,
        seed_fit=seed_fit,
        safety_flags=[],
    )
    cid = built["candidate_id"]
    mock_client = MagicMock()
    with (
        patch.object(sc, "fetch_seed_graph_window", return_value=rows),
        patch.object(sc, "fetch_distinctiveness_baseline", return_value={"za": {}}),
        patch.object(sc, "fetch_week_proposed_counts", return_value={"za": 0, "ng": 0, "ke": 0}),
        patch.object(sc, "fetch_first_seen_map", return_value={}),
        patch.object(sc, "fetch_rejection_memory", return_value=set()),
        patch.object(sc, "load_config_terms", return_value=set()),
        patch.object(sc, "fetch_existing_candidate_ids", return_value={cid}),
        patch.object(sc, "persist_candidates") as persist,
        patch.object(sc, "get_client", return_value=mock_client),
        patch.object(sc, "get_dataset", return_value="trends"),
    ):
        n = sc.run_lane(monday, "fast")
    assert n == 0
    persist.assert_not_called()


def test_cap_split_weekly_respects_limit():
    monday = date(2026, 7, 6)
    rows = [_sg_row(term=f"term{i}", sample_row_ids=[f"r{i}"] * 6) for i in range(20)]
    with (
        patch.object(sc, "fetch_seed_graph_window", return_value=rows),
        patch.object(
            sc, "fetch_distinctiveness_baseline", return_value={"za": {}, "ng": {}, "ke": {}}
        ),
        patch.object(sc, "fetch_week_proposed_counts", return_value={"za": 0, "ng": 0, "ke": 0}),
        patch.object(sc, "fetch_first_seen_map", return_value={}),
        patch.object(sc, "fetch_rejection_memory", return_value=set()),
        patch.object(sc, "load_config_terms", return_value=set()),
        patch.object(sc, "fetch_existing_candidate_ids", return_value=set()),
        patch.object(sc, "persist_candidates", side_effect=lambda r: len(r)) as persist,
        patch.object(sc, "get_client", return_value=MagicMock()),
        patch.object(sc, "get_dataset", return_value="trends"),
    ):
        n = sc.run_lane(monday, "weekly")
    assert n <= sc.WEEKLY_CAP * 3
    for call in persist.call_args_list:
        assert len(call.args[0]) <= sc.WEEKLY_CAP


def test_novelty_window_weekly_vs_fast():
    today = date(2026, 7, 7)
    old_weekly = today - timedelta(days=10)
    term_agg = {
        "term": "newterm",
        "frequency": 10,
        "first_seen_event_date": old_weekly,
        "topic_groups": set(),
        "near_topics": set(),
    }
    assert not sc.is_eligible(term_agg, set(), set(), "weekly", cap_remaining=5, trend_date=today)
    assert sc.is_eligible(term_agg, set(), set(), "fast", cap_remaining=5, trend_date=today)
    old_fast = today - timedelta(days=15)
    term_agg["first_seen_event_date"] = old_fast
    assert not sc.is_eligible(term_agg, set(), set(), "fast", cap_remaining=5, trend_date=today)


def test_slang_share_only_counts_for_slang_terms():
    base_agg = {
        "term": "newterm",
        "frequency": 10,
        "topic_groups": set(),
        "near_topics": set(),
        "platform_counts": {"tiktok": 10},
        "avg_genz_score": 0.0,
        "slang_row_share": 0.8,
    }
    token_agg = dict(base_agg, candidate_type="keyword")
    slang_agg = dict(base_agg, candidate_type="slang")
    _, fit_token = sc.compute_c2_score(token_agg, set(), {"newterm": 20.0})
    _, fit_slang = sc.compute_c2_score(slang_agg, set(), {"newterm": 20.0})
    assert fit_token["slang"] == 0.0
    assert fit_slang["slang"] == pytest.approx(0.8)


def test_c2_weights_sum_to_one():
    assert pytest.approx((0.0, 0.05, 0.70, 0.25)) == (
        sc.C2_W_CO_OCCUR,
        sc.C2_W_VISUAL_AUDIO,
        sc.C2_W_DISTINCTIVENESS,
        sc.C2_W_SLANG,
    )
    total = sc.C2_W_CO_OCCUR + sc.C2_W_VISUAL_AUDIO + sc.C2_W_DISTINCTIVENESS + sc.C2_W_SLANG
    assert total == pytest.approx(1.0)


@pytest.mark.parametrize(
    "n_topics,expected",
    [(0, 0.0), (1, 1.0), (2, 0.8), (3, 0.6), (6, 0.0), (10, 0.0)],
)
def test_day_one_specificity_scale(n_topics, expected):
    assert sc._day_one_specificity(n_topics) == pytest.approx(expected)


def test_distinctiveness_specificity_fallback_day_one():
    # No baseline: distinctiveness falls back to topical specificity, a term
    # tied to one topic beats one spread across six.
    specific = {
        "term": "newterm",
        "frequency": 10,
        "topic_groups": {"music_amapiano"},
        "near_topics": set(),
        "platform_counts": {"tiktok": 10},
        "avg_genz_score": 0.0,
        "slang_row_share": 0.0,
        "candidate_type": "keyword",
    }
    broad = dict(specific, topic_groups={"a", "b", "c", "d", "e", "f"})
    _, fit_specific = sc.compute_c2_score(specific, set(), {})
    _, fit_broad = sc.compute_c2_score(broad, set(), {})
    assert fit_specific["distinctiveness"] == pytest.approx(1.0)
    assert fit_broad["distinctiveness"] == pytest.approx(0.0)
    # Once a real baseline exists it takes over from the specificity proxy.
    _, fit_base = sc.compute_c2_score(specific, set(), {"newterm": 20.0})
    assert fit_base["distinctiveness"] == pytest.approx(0.5)  # freq 10 / base 20


def test_candidate_score_and_fit_are_invariant_to_genz_measurement():
    agg = {
        "term": "newterm",
        "frequency": 10,
        "topic_groups": {"music_amapiano"},
        "near_topics": set(),
        "platform_counts": {"tiktok": 10},
        "avg_genz_score": 0.0,
        "slang_row_share": 0.0,
        "candidate_type": "keyword",
    }
    low_score, low_fit = sc.compute_c2_score(agg, set(), {})
    high_score, high_fit = sc.compute_c2_score({**agg, "avg_genz_score": 1.0}, set(), {})

    assert low_score == high_score
    assert low_fit == high_fit
    assert low_fit["genz"] == 0.0
    assert "genz" not in sc.score_rationale(low_score, low_fit).lower()


def test_reweight_specific_signal_beats_broad_generic():
    # A specific, genz-signalled term outranks a generic word that co-occurs
    # with many hot topics. This is the inversion the reweight fixes.
    specific = {
        "term": "maboneng",
        "frequency": 8,
        "topic_groups": {"genz_lifestyle"},
        "near_topics": set(),
        "platform_counts": {"tiktok": 8},
        "avg_genz_score": 0.35,
        "slang_row_share": 0.0,
        "candidate_type": "keyword",
    }
    generic = {
        "term": "vibe",
        "frequency": 40,
        "topic_groups": {"a", "b", "c", "d", "e", "f"},
        "near_topics": set(),
        "platform_counts": {"tiktok": 40},
        "avg_genz_score": 0.0,
        "slang_row_share": 0.0,
        "candidate_type": "keyword",
    }
    hot = {"a", "b", "c"}
    s_specific, _ = sc.compute_c2_score(specific, hot, {})
    s_generic, _ = sc.compute_c2_score(generic, hot, {})
    assert s_specific > s_generic


def test_broad_generic_stays_below_fast_floor():
    # A generic word touches many topics, so day-one specificity sinks it; with
    # no genz signal it lands below the fast floor. Post-inherited slang share
    # is ignored for keyword terms.
    agg = {
        "term": "content",
        "frequency": 30,
        "topic_groups": {"a", "b", "c", "d", "e", "f"},
        "near_topics": set(),
        "platform_counts": {"tiktok": 30},
        "avg_genz_score": 0.05,
        "slang_row_share": 0.65,
        "candidate_type": "keyword",
    }
    score, _ = sc.compute_c2_score(agg, set(), {})
    assert score < sc.DEFAULT_FAST_FLOOR


def test_seed_fit_persisted_without_distinctiveness_and_rationale_written():
    agg = {
        "market": "za",
        "term": "newterm",
        "candidate_type": "keyword",
        "frequency": 10,
        "topic_groups": {"music_amapiano"},
        "near_topics": set(),
        "platform_counts": {"tiktok": 10},
        "avg_genz_score": 0.4,
        "slang_row_share": 0.0,
        "sample_row_ids": ["r1"],
    }
    score, seed_fit = sc.compute_c2_score(agg, {"music_amapiano"}, {"newterm": 5.0})
    assert "distinctiveness" in seed_fit
    row = sc.build_candidate_rows(
        agg,
        lane="fast",
        proposed_date=date(2026, 7, 6),
        score=score,
        seed_fit=seed_fit,
        safety_flags=[],
    )
    assert set(row["seed_fit"].keys()) == {"genz", "slang", "visual_audio", "co_occur"}
    assert row["rationale"].startswith(f"score {score:.2f} = co_occur")
    assert "distinctiveness" in row["rationale"]


def test_persisted_candidate_is_identical_when_only_genz_changes():
    agg = {
        "market": "za",
        "term": "newterm",
        "candidate_type": "keyword",
        "frequency": 10,
        "topic_groups": {"music_amapiano"},
        "near_topics": set(),
        "platform_counts": {"tiktok": 10},
        "avg_genz_score": 0.0,
        "slang_row_share": 0.0,
        "sample_row_ids": ["r1"],
    }

    def build(source):
        score, fit = sc.compute_c2_score(source, {"music_amapiano"}, {"newterm": 5.0})
        return sc.build_candidate_rows(
            source,
            lane="fast",
            proposed_date=date(2026, 7, 6),
            score=score,
            seed_fit=fit,
            safety_flags=[],
        )

    assert build(agg) == build({**agg, "avg_genz_score": 1.0})


def test_top_fifteen_candidate_set_is_invariant_to_genz_measurement():
    terms = [
        {
            "term": f"term{index:02d}",
            "frequency": index + 1,
            "topic_groups": {"music_amapiano"},
            "near_topics": set(),
            "platform_counts": {"tiktok": index + 1},
            "avg_genz_score": 0.0,
            "slang_row_share": 0.0,
            "candidate_type": "keyword",
        }
        for index in range(20)
    ]
    baseline = {item["term"]: 30.0 for item in terms}

    def top(items):
        scored = [(sc.compute_c2_score(item, set(), baseline)[0], item["term"]) for item in items]
        return [term for _, term in sorted(scored, key=lambda pair: (-pair[0], pair[1]))[:15]]

    high_genz = [dict(item, avg_genz_score=(index % 5) / 4) for index, item in enumerate(terms)]
    assert top(terms) == top(high_genz)


def test_first_seen_skips_nat_entries():
    import pandas as pd

    rows = [_sg_row(event_date=date(2026, 7, 5))]
    fs_map = {("za", "newterm"): pd.NaT}
    agg = sc.aggregate_terms(rows, "za", first_seen=fs_map)
    assert agg["newterm"]["first_seen_event_date"] == date(2026, 7, 5)


def test_first_seen_nat_row_event_date_stays_none():
    import pandas as pd

    rows = [_sg_row(event_date=pd.NaT)]
    agg = sc.aggregate_terms(rows, "za")
    assert agg["newterm"]["first_seen_event_date"] is None
