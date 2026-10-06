"""Unit tests for the event-state ledger builder.

All BQ reads and the Gemini client are mocked; NO live calls. The BQ read is
mocked by monkeypatching _fetch_factual_rows so a test supplies synthetic
enriched_content rows directly. Gemini is mocked with a fake client whose
generate_brief returns a canned BriefResponse.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timezone
from typing import Any

import pytest
from src.analysis import event_ledger as el
from src.analysis.gemini_client import BriefResponse

TREND_DATE = date(2026, 6, 26)


class FakeGeminiClient:
    """Stand-in for GeminiClient.generate_brief returning canned JSON."""

    def __init__(self, items: list[dict[str, Any]], model: str = "gemini-2.5-flash") -> None:
        self._items = items
        self._model = model
        self.calls = 0

    def generate_brief(self, prompt, response_schema, **kwargs):
        self.calls += 1
        return BriefResponse(
            parsed=self._items,
            raw_text="",
            prompt_tokens=10,
            completion_tokens=10,
            model=self._model,
        )


def _gdelt_row(persons: str = "", orgs: str = "", title: str = "", published=None):
    return {
        "source": "gdelt",
        "content_type": "gdelt_gkg",
        "title": title,
        "text": "",
        "query_term": "",
        "v2persons": persons,
        "v2orgs": orgs,
        "v2locations": "",
        "v2tone": "0.0",
        "search_velocity_score": 0.0,
        "published_at": published or datetime(2026, 6, 26, 9, 0, tzinfo=UTC),
    }


def _rss_row(title: str, published=None):
    return {
        "source": "rss",
        "content_type": None,
        "title": title,
        "text": "",
        "query_term": "",
        "v2persons": "",
        "v2orgs": "",
        "v2locations": "",
        "v2tone": "",
        "search_velocity_score": 0.0,
        "published_at": published or datetime(2026, 6, 26, 10, 0, tzinfo=UTC),
    }


def _trends_row(term: str, velocity: float, published=None):
    return {
        "source": "bigquery_trends",
        "content_type": None,
        "title": "",
        "text": "",
        "query_term": term,
        "v2persons": "",
        "v2orgs": "",
        "v2locations": "",
        "v2tone": "",
        "search_velocity_score": velocity,
        "published_at": published or datetime(2026, 6, 26, 8, 0, tzinfo=UTC),
    }


# ---------------------------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------------------------


def test_candidate_extraction_splits_gdelt_entities():
    rows = [_gdelt_row(persons="Cyril Ramaphosa,123;Julius Malema,9", title="SA politics latest")]
    cands = el._extract_candidates(rows)
    keys = {c["entity_key"] for c in cands}
    assert "cyril ramaphosa" in keys
    assert "julius malema" in keys
    # Offset suffix stripped.
    assert all("123" not in c["entity_key"] for c in cands)
    assert all(c["family"] == "gdelt" for c in cands)


def test_candidate_extraction_rss_and_trends():
    rows = [_rss_row("Bafana Bafana win the cup"), _trends_row("bafana bafana", 0.8)]
    cands = el._extract_candidates(rows)
    families = {c["family"] for c in cands}
    assert families == {"rss", "trends"}
    trends = next(c for c in cands if c["family"] == "trends")
    assert trends["is_search_spike"] is True


def test_channel_family_accepts_live_article_and_search_term_shapes():
    article = _rss_row("Bafana Bafana win the cup")
    article["source"] = "Nation Africa"
    article["content_type"] = "article"
    search_term = _trends_row("bafana bafana", 0.8)
    search_term["source"] = "Google Trends"
    search_term["content_type"] = "search_term"
    top_term = _trends_row("amapiano", 0.4)
    top_term["source"] = "Google Trends"
    top_term["content_type"] = "top_term"

    assert el._channel_family(article) == "rss"
    assert el._channel_family(search_term) == "trends"
    assert el._channel_family(top_term) == "trends"


def test_channel_family_accepts_youtube_video_and_apple_music_chart():
    video = _rss_row("Burna Boy - New Song (Official Video)")
    video["source"] = "Burna Boy"
    video["content_type"] = "video"
    video_with_category = _rss_row("Amapiano Mix 2026")
    video_with_category["source"] = "DJ Sbu"
    video_with_category["content_type"] = "video/10"
    chart = _rss_row("New Song - Burna Boy")
    chart["source"] = "apple_music"
    chart["content_type"] = "chart_track"

    assert el._channel_family(video) == "youtube"
    assert el._channel_family(video_with_category) == "youtube"
    assert el._channel_family(chart) == "music"


def test_fetch_factual_rows_sql_covers_live_article_and_search_term_shapes():
    captured: dict[str, str] = {}

    class _FakeJob:
        def result(self):
            return []

    class _FakeClient:
        project = "proj"

        def query(self, sql, job_config=None):
            captured["sql"] = sql
            captured["job_config"] = job_config
            return _FakeJob()

    rows = el._fetch_factual_rows(_FakeClient(), "ds", TREND_DATE, "ke")
    assert rows == []
    assert "content_type = 'article'" in captured["sql"]
    assert "content_type IN ('search_term', 'top_term', 'trending_search')" in captured["sql"]
    assert "content_type = 'video'" in captured["sql"]
    assert "content_type LIKE 'video/%'" in captured["sql"]
    assert "chart_track" in captured["sql"]


def test_extract_candidates_youtube_and_music_headlines():
    video = _rss_row("Bafana Bafana vs Nigeria Match Highlights")
    video["source"] = "SuperSport"
    video["content_type"] = "video"
    chart = _rss_row("New Song - Burna Boy")
    chart["source"] = "apple_music"
    chart["content_type"] = "chart_track"

    cands = el._extract_candidates([video, chart])
    families = {c["family"] for c in cands}
    assert families == {"youtube", "music"}
    # Each row yields its headline candidate plus extracted name anchors.
    kinds = {c["event_kind"] for c in cands}
    assert kinds == {"headline", "entity"}
    headline_keys = {c["entity_key"] for c in cands if c["event_kind"] == "headline"}
    assert headline_keys == {
        "bafana bafana vs nigeria match highlights",
        "new song burna boy",
    }


def test_noise_floor_youtube_corroborates_gdelt():
    rows = [
        _gdelt_row(persons="Bafana Bafana,1", title="Bafana Bafana vs Nigeria"),
        _rss_row("Bafana Bafana vs Nigeria Match Highlights"),
    ]
    rows[1]["source"] = "SuperSport"
    rows[1]["content_type"] = "video"
    cands = el._extract_candidates(rows)
    clusters = el._cluster_candidates(cands)
    bafana = next(c for c in clusters if c["entity_key"] == "bafana bafana")
    assert set(bafana["families"]) == {"gdelt", "youtube"}


# ---------------------------------------------------------------------------
# Clustering + noise floor
# ---------------------------------------------------------------------------


def test_noise_floor_requires_two_families_or_spike():
    # Same entity across rss + gdelt -> two families -> kept.
    rows = [
        _gdelt_row(persons="Bafana Bafana,1", title="Bafana Bafana match report"),
        _rss_row("Bafana Bafana win the cup"),
    ]
    cands = el._extract_candidates(rows)
    clusters = el._cluster_candidates(cands)
    keys = {c["entity_key"] for c in clusters}
    assert "bafana bafana" in keys


def test_noise_floor_drops_single_family_no_spike():
    # One entity, one family (rss only), no spike -> dropped.
    rows = [_rss_row("A quiet local story about nothing")]
    cands = el._extract_candidates(rows)
    clusters = el._cluster_candidates(cands)
    assert clusters == []


def test_noise_floor_single_family_with_spike_kept():
    # Trends only (one family) but a real velocity spike -> kept.
    rows = [_trends_row("load shedding", 0.9)]
    cands = el._extract_candidates(rows)
    clusters = el._cluster_candidates(cands)
    keys = {c["entity_key"] for c in clusters}
    assert "load shedding" in keys
    spike_cluster = next(c for c in clusters if c["entity_key"] == "load shedding")
    assert spike_cluster["max_search_velocity"] == pytest.approx(0.9)


def test_vs_headline_attaches_to_both_fixture_anchors():
    # Two independent GDELT anchors, each already corroborated by a trends
    # spike so they clear the noise floor on their own. A single "X vs Y"
    # headline should corroborate BOTH sides of the fixture, not just
    # whichever anchor string happens to be longer.
    rows = [
        _gdelt_row(persons="Springboks,1", title="Springboks training update"),
        _trends_row("springboks", 0.9),
        _gdelt_row(persons="England,1", title="England training update"),
        _trends_row("england", 0.9),
        _rss_row("Springboks vs England: full match preview"),
    ]
    cands = el._extract_candidates(rows)
    clusters = el._cluster_candidates(cands)
    by_key = {c["entity_key"]: c for c in clusters}
    assert "rss" in by_key["springboks"]["families"]
    assert "rss" in by_key["england"]["families"]


def test_non_fixture_headline_still_attaches_to_longest_anchor_only():
    # No vs/versus marker: keep the original longest-anchor-wins behaviour so
    # a headline does not fan out to every substring match.
    rows = [
        _gdelt_row(persons="Bafana Bafana,1", title="Bafana Bafana update"),
        _trends_row("bafana bafana", 0.9),
        _gdelt_row(persons="Bafana,1", title="Bafana note"),
        _trends_row("bafana", 0.9),
        _rss_row("Bafana Bafana win the cup"),
    ]
    cands = el._extract_candidates(rows)
    clusters = el._cluster_candidates(cands)
    by_key = {c["entity_key"]: c for c in clusters}
    assert "rss" in by_key["bafana bafana"]["families"]
    assert "rss" not in by_key["bafana"]["families"]


def test_noise_floor_drop_logs_near_miss(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="src.analysis.event_ledger"):
        # A lone GDELT mention: one family, no spike -> dropped at the noise
        # floor. Should still log a near-miss so an operator can see what
        # almost cleared the bar.
        rows = [_gdelt_row(persons="Julius Malema,1", title="Julius Malema speaks")]
        cands = el._extract_candidates(rows)
        el._cluster_candidates(cands)
    assert any("noise floor" in r.message.lower() for r in caplog.records)


def test_cluster_collects_sources_and_freshest_quote():
    older = datetime(2026, 6, 26, 6, 0, tzinfo=UTC)
    newer = datetime(2026, 6, 26, 11, 0, tzinfo=UTC)
    rows = [
        _gdelt_row(persons="Burna Boy,1", title="Old headline", published=older),
        _rss_row("Burna Boy releases new album", published=newer),
    ]
    cands = el._extract_candidates(rows)
    clusters = el._cluster_candidates(cands)
    burna = next(c for c in clusters if c["entity_key"] == "burna boy")
    assert set(burna["families"]) == {"gdelt", "rss"}
    # gdelt entity + rss headline + the name anchor extracted from it.
    assert burna["source_count"] == 3
    assert burna["as_of"] == newer
    assert burna["evidence_quote"] == "Burna Boy releases new album"


# ---------------------------------------------------------------------------
# Deterministic state patterns
# ---------------------------------------------------------------------------


def test_deterministic_resolved():
    members = [{"headline": "Bafana Bafana win the final"}]
    assert el._deterministic_state(members) == "resolved"


def test_deterministic_scheduled():
    members = [{"headline": "Bafana Bafana vs Nigeria preview"}]
    assert el._deterministic_state(members) == "scheduled"


def test_deterministic_unknown():
    members = [{"headline": "Bafana Bafana training camp opens"}]
    assert el._deterministic_state(members) == "unknown"


def test_deterministic_resolved_recognizes_reported_speech():
    # Real live headline shapes (2026-07-04 ZA pull): a reported-speech verb
    # means the speech act already happened, even with no win/beat/results.
    assert (
        el._deterministic_state(
            [
                {
                    "headline": "President Cyril Ramaphosa told people from Hammanskraal that they must boil water"
                }
            ]
        )
        == "resolved"
    )
    assert (
        el._deterministic_state(
            [
                {
                    "headline": "President Cyril Ramaphosa has called for the full implementation of the plan"
                }
            ]
        )
        == "resolved"
    )
    assert (
        el._deterministic_state(
            [{"headline": "BREAKING: Obasanjo Endorses Peter Obi For 2023 Presidency"}]
        )
        == "resolved"
    )


def test_deterministic_scheduled_recognizes_to_address():
    # Real live headline (2026-07-04 ZA pull): a future address has no "vs"
    # or "fixture" but is clearly not-yet-happened.
    assert (
        el._deterministic_state(
            [{"headline": "BREAKING | President Cyril Ramaphosa to address the nation at 20:30"}]
        )
        == "scheduled"
    )


# ---------------------------------------------------------------------------
# Confidence computed in Python
# ---------------------------------------------------------------------------


def test_confidence_formula_in_python():
    # source_count 3 -> 0.4, recency 1.0 -> 0.3, citation -> 0.3 => 1.0
    assert el._compute_confidence(3, 1.0, True) == pytest.approx(1.0)
    # source_count 1 -> 0.4*0.333, recency 0 -> 0, no citation -> 0
    assert el._compute_confidence(1, 0.0, False) == pytest.approx(0.4 * (1 / 3))
    # clamps to [0,1]
    assert 0.0 <= el._compute_confidence(10, 1.0, True) <= 1.0


def test_recency_factor():
    same_day = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
    assert el._recency_factor(same_day, TREND_DATE) == 1.0
    one_day = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)
    assert el._recency_factor(one_day, TREND_DATE) == 0.5
    two_day = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)
    assert el._recency_factor(two_day, TREND_DATE) == 0.0
    assert el._recency_factor(None, TREND_DATE) == 0.0


# ---------------------------------------------------------------------------
# Gemini post-validation (anti-hallucination)
# ---------------------------------------------------------------------------


def _two_family_rows():
    return [
        _gdelt_row(persons="Bafana Bafana,1", title="Bafana Bafana vs Nigeria"),
        _rss_row("Bafana Bafana vs Nigeria fixture set"),
    ]


def test_gemini_state_with_valid_citation_wins(monkeypatch):
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: _two_family_rows())
    # Both evidence rows are index 0 and 1, both belong to 'bafana bafana'.
    fake = FakeGeminiClient(
        [
            {
                "entity_key": "bafana bafana",
                "state_label": "resolved",
                "state_text": "Bafana Bafana won.",
                "evidence_index": 0,
            }
        ]
    )
    out = el.build_event_ledger(TREND_DATE, "za", client=fake, bq_client=object(), dataset="ds")
    row = next(r for r in out if r["entity_key"] == "bafana bafana")
    assert row["resolved_by"] == "gemini"
    assert row["state_label"] == "resolved"
    assert row["state_text"] == "Bafana Bafana won."


def test_gemini_state_with_bad_evidence_index_dropped(monkeypatch):
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: _two_family_rows())
    # evidence_index 99 points at no supplied row -> dropped -> deterministic.
    fake = FakeGeminiClient(
        [
            {
                "entity_key": "bafana bafana",
                "state_label": "resolved",
                "state_text": "Bafana Bafana won (invented).",
                "evidence_index": 99,
            }
        ]
    )
    out = el.build_event_ledger(TREND_DATE, "za", client=fake, bq_client=object(), dataset="ds")
    row = next(r for r in out if r["entity_key"] == "bafana bafana")
    assert row["resolved_by"] == "deterministic"
    # 'vs' / 'fixture' headline -> deterministic scheduled, NOT the model's resolved.
    assert row["state_label"] == "scheduled"


def test_uncitable_resolved_falls_back(monkeypatch):
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: _two_family_rows())
    # No evidence_index at all -> uncitable -> dropped -> deterministic.
    fake = FakeGeminiClient(
        [
            {
                "entity_key": "bafana bafana",
                "state_label": "resolved",
                "state_text": "Bafana Bafana won.",
            }
        ]
    )
    out = el.build_event_ledger(TREND_DATE, "za", client=fake, bq_client=object(), dataset="ds")
    row = next(r for r in out if r["entity_key"] == "bafana bafana")
    assert row["resolved_by"] == "deterministic"
    assert row["state_label"] == "scheduled"


def test_citation_to_other_entity_rejected(monkeypatch):
    # Two distinct entities, two families each. Model cites entity B's row index
    # while claiming entity A -> cross-entity citation rejected.
    rows = [
        _gdelt_row(persons="Burna Boy,1", title="Burna Boy releases album"),  # idx 0
        _rss_row("Burna Boy drops new album"),  # idx 1
        _gdelt_row(persons="Davido,1", title="Davido vs critics"),  # idx 2
        _rss_row("Davido concert fixture announced"),  # idx 3
    ]
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: rows)
    fake = FakeGeminiClient(
        [
            {
                "entity_key": "burna boy",
                "state_label": "resolved",
                "state_text": "wrong cite",
                "evidence_index": 3,  # belongs to davido
            }
        ]
    )
    out = el.build_event_ledger(TREND_DATE, "za", client=fake, bq_client=object(), dataset="ds")
    burna = next(r for r in out if r["entity_key"] == "burna boy")
    assert burna["resolved_by"] == "deterministic"


def test_never_invent_free_text_no_citation(monkeypatch):
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: _two_family_rows())
    # Model returns prose with a non-integer evidence_index -> no resolved state.
    fake = FakeGeminiClient(
        [
            {
                "entity_key": "bafana bafana",
                "state_label": "resolved",
                "state_text": "I happen to know they won.",
                "evidence_index": "n/a",
            }
        ]
    )
    out = el.build_event_ledger(TREND_DATE, "za", client=fake, bq_client=object(), dataset="ds")
    row = next(r for r in out if r["entity_key"] == "bafana bafana")
    assert row["resolved_by"] == "deterministic"
    assert row["state_label"] != "resolved"


def test_no_gemini_client_uses_deterministic(monkeypatch):
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: _two_family_rows())
    out = el.build_event_ledger(TREND_DATE, "za", client=None, bq_client=object(), dataset="ds")
    row = next(r for r in out if r["entity_key"] == "bafana bafana")
    assert row["resolved_by"] == "deterministic"
    assert row["confidence"] >= 0.0


def test_ledger_row_contract(monkeypatch):
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: _two_family_rows())
    out = el.build_event_ledger(TREND_DATE, "za", client=None, bq_client=object(), dataset="ds")
    assert out
    row = out[0]
    expected_keys = {
        "entity_key",
        "entity_aliases",
        "event_kind",
        "state_label",
        "state_text",
        "as_of",
        "evidence_quote",
        "corroborating_sources",
        "source_count",
        "confidence",
        "resolved_by",
        "_gemini_model",
    }
    assert set(row.keys()) == expected_keys
    assert isinstance(row["entity_aliases"], list)
    assert isinstance(row["corroborating_sources"], list)
    assert isinstance(row["source_count"], int)
    assert isinstance(row["confidence"], float)
    # as_of is an ISO string or None.
    assert row["as_of"] is None or isinstance(row["as_of"], str)


def test_empty_rows_returns_empty(monkeypatch):
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: [])
    out = el.build_event_ledger(TREND_DATE, "za", client=None, bq_client=object(), dataset="ds")
    assert out == []


# ---------------------------------------------------------------------------
# Phase 2: fixture-language pattern coverage
# ---------------------------------------------------------------------------


def test_deterministic_resolved_covers_sport_result_verbs():
    assert el._deterministic_state([{"headline": "Springboks thrash England 30-10"}]) == "resolved"
    assert el._deterministic_state([{"headline": "Chiefs hammered by Sundowns"}]) == "resolved"
    assert el._deterministic_state([{"headline": "Bafana clinched the trophy"}]) == "resolved"


def test_deterministic_scheduled_covers_sport_preview_verbs():
    assert (
        el._deterministic_state([{"headline": "Springboks to take on England in Twickenham"}])
        == "scheduled"
    )
    assert (
        el._deterministic_state([{"headline": "Bafana Bafana kick off their campaign Friday"}])
        == "scheduled"
    )


# ---------------------------------------------------------------------------
# Phase 2: GDELT theme-blob sanity for state_text / evidence_quote
# ---------------------------------------------------------------------------


def test_looks_like_theme_blob_detects_gdelt_codes():
    assert el._looks_like_theme_blob("TAX_FNCACT_LEADER WB_1249_ECONOMIC_GROWTH Cyril Ramaphosa")
    assert not el._looks_like_theme_blob("Bafana Bafana win the cup")


def test_cluster_prefers_clean_headline_over_gdelt_blob_for_evidence_quote():
    # The GDELT row is freshest but its text is a synthesised theme blob (no
    # title). An RSS headline in the same cluster is older but readable, so
    # evidence_quote should surface the readable one, never the blob.
    older = datetime(2026, 6, 26, 6, 0, tzinfo=UTC)
    newer = datetime(2026, 6, 26, 11, 0, tzinfo=UTC)
    rows = [
        _rss_row("Bafana Bafana win the cup", published=older),
        _gdelt_row(persons="Bafana Bafana,1", published=newer),
    ]
    rows[1]["text"] = "TAX_FNCACT_LEADER WB_1249_ECONOMIC_GROWTH Bafana Bafana bbc.com"
    cands = el._extract_candidates(rows)
    clusters = el._cluster_candidates(cands)
    bafana = next(c for c in clusters if c["entity_key"] == "bafana bafana")
    assert bafana["evidence_quote"] == "Bafana Bafana win the cup"
    # as_of still reflects the true freshest member.
    assert bafana["as_of"] == newer


def test_cluster_prefers_clean_trends_term_over_gdelt_blob():
    rows = [
        _gdelt_row(persons="Julius Malema,1", title=""),
        _trends_row("julius malema", 0.9),
    ]
    rows[0]["text"] = "TAX_FNCACT_LEADER WB_1249_POLITICS Julius Malema news24.com"
    cands = el._extract_candidates(rows)
    clusters = el._cluster_candidates(cands)
    malema = next(c for c in clusters if c["entity_key"] == "julius malema")
    assert not el._looks_like_theme_blob(malema["evidence_quote"])


def test_pick_evidence_quote_returns_freshest_blob_when_all_members_are_blobs():
    # Direct check of the true "no clean alternative" branch: every member
    # a theme blob (computed from headline text, no precomputed flag) still
    # returns something rather than raising, using the freshest one.
    older = datetime(2026, 6, 26, 6, 0, tzinfo=UTC)
    newer = datetime(2026, 6, 26, 11, 0, tzinfo=UTC)
    members = [
        {"headline": "TAX_OLD_BLOB news24.com", "published_at": older},
        {"headline": "TAX_FRESH_BLOB news24.com", "published_at": newer},
    ]
    assert el._pick_evidence_quote(members) == "TAX_FRESH_BLOB news24.com"


def test_state_text_safety_net_falls_back_when_gemini_returns_a_blob(monkeypatch):
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: _two_family_rows())
    fake = FakeGeminiClient(
        [
            {
                "entity_key": "bafana bafana",
                "state_label": "resolved",
                "state_text": "TAX_FNCACT_LEADER WB_1249_SPORT Bafana Bafana",
                "evidence_index": 0,
            }
        ]
    )
    out = el.build_event_ledger(TREND_DATE, "za", client=fake, bq_client=object(), dataset="ds")
    row = next(r for r in out if r["entity_key"] == "bafana bafana")
    assert not el._looks_like_theme_blob(row["state_text"])


# ---------------------------------------------------------------------------
# Phase 2: partial citation credit for a grounded deterministic resolution
# ---------------------------------------------------------------------------


def test_deterministic_resolved_with_clean_headline_gets_citation_credit(monkeypatch):
    # rss + gdelt, deterministic 'resolved' via the RSS headline's 'win'.
    # evidence_quote is the clean RSS headline, so the resolved verdict is
    # grounded in a real citable quote and should earn the same confidence
    # boost a Gemini citation would.
    rows = [
        _gdelt_row(persons="Bafana Bafana,1"),
        _rss_row("Bafana Bafana win the final"),
    ]
    rows[0]["text"] = "TAX_FNCACT_LEADER WB_1249_SPORT Bafana Bafana bbc.com"
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: rows)
    out = el.build_event_ledger(TREND_DATE, "za", client=None, bq_client=object(), dataset="ds")
    row = next(r for r in out if r["entity_key"] == "bafana bafana")
    assert row["resolved_by"] == "deterministic"
    assert row["state_label"] == "resolved"
    # citation credit (+0.3) on top of source_count=3 (the rss headline now
    # also contributes its extracted name-anchor member) + recency=1.0
    assert row["confidence"] == pytest.approx(el._compute_confidence(3, 1.0, True), abs=1e-4)


def test_deterministic_scheduled_gets_no_citation_credit(monkeypatch):
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: _two_family_rows())
    out = el.build_event_ledger(TREND_DATE, "za", client=None, bq_client=object(), dataset="ds")
    row = next(r for r in out if r["entity_key"] == "bafana bafana")
    assert row["state_label"] == "scheduled"
    assert row["confidence"] == pytest.approx(el._compute_confidence(3, 1.0, False), abs=1e-4)


# ---------------------------------------------------------------------------
# Phase 3: curated entity alias map
# ---------------------------------------------------------------------------


def test_merge_aliases_adds_curated_and_drops_blocklisted_tokens():
    merged = el._merge_aliases(["Bafana Bafana"], ["Bafana", "President", ""])
    assert "Bafana" in merged
    assert "Bafana Bafana" in merged
    assert "President" not in merged


def test_entity_alias_map_reads_configured_market(monkeypatch):
    monkeypatch.setattr(
        el,
        "load_entity_aliases",
        lambda: {"za": {"cyril ramaphosa": ["Ramaphosa", "President Ramaphosa"]}},
    )
    alias_map = el._entity_alias_map("za")
    assert alias_map["cyril ramaphosa"] == ["Ramaphosa", "President Ramaphosa"]
    assert el._entity_alias_map("ng") == {}


def test_entity_alias_map_degrades_to_empty_on_error(monkeypatch):
    def _boom():
        raise RuntimeError("config missing")

    monkeypatch.setattr(el, "load_entity_aliases", _boom)
    assert el._entity_alias_map("za") == {}


def test_build_event_ledger_injects_curated_alias_for_matched_entity(monkeypatch):
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: _two_family_rows())
    monkeypatch.setattr(
        el,
        "load_entity_aliases",
        lambda: {"za": {"bafana bafana": ["Bafana", "South Africa national team"]}},
    )
    out = el.build_event_ledger(TREND_DATE, "za", client=None, bq_client=object(), dataset="ds")
    row = next(r for r in out if r["entity_key"] == "bafana bafana")
    assert "Bafana" in row["entity_aliases"]
    assert "South Africa national team" in row["entity_aliases"]
    # Aliases seen in today's data are still kept alongside the curated ones.
    assert "Bafana Bafana" in row["entity_aliases"]


# Live-shape regressions (4 Jul 2026): enriched_content RSS rows carry
# content_type='article' with the outlet name as source; GDELT rows carry an
# empty title and a raw theme dump in text; v2persons can be the literal
# string 'nan' from a pandas round-trip.
# ---------------------------------------------------------------------------


def test_rss_family_from_article_content_type():
    row = _rss_row("Bafana Bafana win the cup")
    row["source"] = "kenyastar.com"
    row["content_type"] = "article"
    cands = el._extract_candidates([row])
    # Headline candidate plus the extracted 'Bafana Bafana' name anchor.
    assert len(cands) == 2
    assert all(c["family"] == "rss" for c in cands)


def test_gdelt_theme_dump_never_becomes_headline():
    row = _gdelt_row(persons="Cyril Ramaphosa,123", title="")
    row["text"] = "CRIME_COMMON_ROBBERY,60 WB_135_TRANSPORT,1218"
    cands = el._extract_candidates([row])
    assert cands, "entity candidate expected"
    assert "CRIME_COMMON_ROBBERY" not in cands[0]["headline"]
    assert cands[0]["headline"] == "Cyril Ramaphosa"


def test_nan_string_is_not_an_entity():
    row = _gdelt_row(persons="nan", orgs="NaN")
    assert el._extract_candidates([row]) == []


def test_entity_key_junk_rejection():
    # Bare numbers / years never anchor an event.
    assert el._entity_key_is_junk("2026")
    assert el._entity_key_is_junk("68000")
    # Generic single tokens observed live as junk ledger keys.
    assert el._entity_key_is_junk("young")
    assert el._entity_key_is_junk("netflix")
    assert el._entity_key_is_junk("facebook")
    # Short single tokens carry no identity.
    assert el._entity_key_is_junk("mtn")
    assert el._entity_key_is_junk("")
    # Real anchors pass: multi-word keys and specific single names.
    assert not el._entity_key_is_junk("nelson mandela")
    assert not el._entity_key_is_junk("super eagles")
    assert not el._entity_key_is_junk("ramaphosa")
    assert not el._entity_key_is_junk("springboks")


def test_extract_candidates_drops_junk_keys():
    rows = [
        _gdelt_row(persons="Cyril Ramaphosa,123;Young,456", orgs="Netflix,7"),
        {
            "source": "bigquery_trends",
            "content_type": "search_term",
            "title": "",
            "text": "",
            "query_term": "2026",
            "search_velocity_score": 9.0,
            "published_at": None,
        },
    ]
    cands = el._extract_candidates(rows)
    keys = {c["entity_key"] for c in cands}
    assert "cyril ramaphosa" in keys
    assert "young" not in keys
    assert "netflix" not in keys
    assert "2026" not in keys


def test_proper_noun_phrases_extraction():
    phrases = el._proper_noun_phrases("Springboks beat England at Ellis Park")
    # Sentence-initial single token dropped; real names kept.
    assert "Springboks" not in phrases
    assert "England" in phrases
    assert "Ellis Park" in phrases
    # Sentence-initial 2+ token run kept.
    assert "Super Eagles" in el._proper_noun_phrases("Super Eagles beat Ghana in Uyo")


def test_proper_noun_phrases_blocks_demonyms_junk_and_shouting():
    assert el._proper_noun_phrases("Young Nigerians turn stress into comedy") == []
    # Bare year and generic tokens rejected via the junk filter.
    assert el._proper_noun_phrases("2026 Netflix Original") == ["Netflix Original"]
    # 3+ token all-caps run is a shouting fragment, not a name.
    assert el._proper_noun_phrases("LISTEN TO ME BRO, big vision needs work") == []
    # Cap at 5 phrases.
    text = "Alpha One meets Beta Two, Gamma Three, Delta Four, Epsilon Five and Zeta Six"
    assert len(el._proper_noun_phrases(text)) == 5


def test_extract_candidates_emits_name_anchors_from_headline():
    cands = el._extract_candidates([_rss_row("Ramaphosa announces cabinet reshuffle for Gauteng")])
    kinds = {(c["event_kind"], c["entity_key"]) for c in cands}
    assert ("headline", "ramaphosa announces cabinet reshuffle for gauteng") in kinds
    assert ("entity", "gauteng") in kinds
    # Sentence-initial single token is not extracted as an anchor.
    assert ("entity", "ramaphosa") not in kinds


def test_headline_only_rows_cluster_on_extracted_name():
    youtube_row = dict(_rss_row("Cyril Ramaphosa cabinet reshuffle explained"))
    youtube_row["source"] = "youtube"
    youtube_row["content_type"] = "video"
    rows = [
        _rss_row("SA politics: Cyril Ramaphosa announces cabinet reshuffle"),
        youtube_row,
    ]
    clusters = el._cluster_candidates(el._extract_candidates(rows))
    by_key = {c["entity_key"]: c for c in clusters}
    assert "cyril ramaphosa" in by_key
    cluster = by_key["cyril ramaphosa"]
    assert set(cluster["families"]) == {"rss", "youtube"}
    assert "Cyril Ramaphosa" in cluster["entity_aliases"]


def _trending_search_row(term: str, published=None):
    row = dict(_trends_row(term, 0.0, published))
    row["source"] = "google_trends_rss"
    row["content_type"] = "trending_search"
    return row


# ---------------------------------------------------------------------------
# Headline-filler entity keys + curated-alias canonicalization (2026-08-03).
# ---------------------------------------------------------------------------


def test_headline_filler_is_junk_as_an_entity_key():
    # Every one of these was a live matched_entity_key in the week to
    # 2026-08-03, anchoring any claim that used the word.
    for token in ("live", "life", "what", "daily", "these", "free", "digital"):
        assert el._entity_key_is_junk(token), token
    # Real single-name anchors are untouched.
    assert not el._entity_key_is_junk("ramaphosa")
    assert not el._entity_key_is_junk("springboks")
    # A multi-word key containing filler still passes: "free state" is a place.
    assert not el._entity_key_is_junk("free state")


def test_filler_capitalisation_never_becomes_an_anchor():
    cands = el._extract_candidates([_rss_row("Watch Live as the Free Digital Life summit opens")])
    keys = {c["entity_key"] for c in cands if c["event_kind"] == "entity"}
    assert "live" not in keys
    assert "free" not in keys


def test_alias_canonical_map_folds_aliases_onto_the_canonical_key():
    canonical = el._alias_canonical_map(
        {"cyril ramaphosa": ["Ramaphosa", "President Ramaphosa"], "eskom": ["Eskom"]}
    )
    assert canonical["ramaphosa"] == "cyril ramaphosa"
    assert canonical["cyril ramaphosa"] == "cyril ramaphosa"
    assert canonical["president ramaphosa"] == "cyril ramaphosa"
    assert canonical["eskom"] == "eskom"


def test_alias_canonical_map_never_folds_a_generic_role_word():
    canonical = el._alias_canonical_map({"cyril ramaphosa": ["President", "Ramaphosa"]})
    assert "president" not in canonical


def test_ramaphosa_and_cyril_ramaphosa_are_one_entity(monkeypatch):
    # GDELT spells the person out; a headline names him by surname only. Before
    # canonicalization these ran as two ledger events keyed on one human being
    # ('ramaphosa' and 'cyril ramaphosa' both live as matched_entity_key).
    monkeypatch.setattr(
        el, "load_entity_aliases", lambda: {"za": {"cyril ramaphosa": ["Ramaphosa"]}}
    )
    rows = [
        _gdelt_row(persons="Cyril Ramaphosa,1", title="Cyril Ramaphosa update"),
        _rss_row("Eskom board briefed as Ramaphosa signs the energy bill"),
    ]
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: rows)

    out = el.build_event_ledger(TREND_DATE, "za", client=None, bq_client=object(), dataset="ds")
    keys = {r["entity_key"] for r in out}

    assert "cyril ramaphosa" in keys
    assert "ramaphosa" not in keys
    row = next(r for r in out if r["entity_key"] == "cyril ramaphosa")
    # The surname headline still reached the canonical cluster, so the two
    # channel families are counted together.
    assert set(row["corroborating_sources"]) == {"gdelt", "rss"}


def test_trending_search_maps_to_trends_family():
    assert el._channel_family(_trending_search_row("pape thiaw")) == "trends"


def test_trending_search_yields_search_term_anchor():
    cands = el._extract_candidates([_trending_search_row("Pape Thiaw")])
    assert len(cands) == 1
    assert cands[0]["event_kind"] == "search_term"
    assert cands[0]["entity_key"] == "pape thiaw"
    # velocity 0.0: contributes an anchor + family, never a spike bypass.
    assert cands[0]["is_search_spike"] is False


def test_trending_search_plus_rss_clears_noise_floor():
    rows = [
        _rss_row("Pape Thiaw names his squad for the qualifiers"),
        _trending_search_row("pape thiaw"),
    ]
    clusters = el._cluster_candidates(el._extract_candidates(rows))
    by_key = {c["entity_key"]: c for c in clusters}
    assert "pape thiaw" in by_key
    assert set(by_key["pape thiaw"]["families"]) == {"rss", "trends"}


def test_pick_evidence_quote_prefers_a_news_headline_over_a_fresher_video_title():
    # Live defect, ng/chelsea on 2026-08-25. The cluster held a Vanguard
    # report of the real match ("Alonso makes winning start with Chelsea",
    # 21:13) and two eFootball simulation uploads (21:27 and 22:02). Freshest
    # wins handed the quote to a simulation, so the ledger published
    # "Fulham 2 - 3 Chelsea | Premier League 2026/27 Simulation | efootball
    # Game Match" as a resolved event and reconcile used it verbatim to
    # overwrite a true brief sentence. A video title is a title, not a
    # statement of what happened, so it only wins when nothing else is there.
    older = datetime(2026, 8, 25, 6, 0, tzinfo=UTC)
    newer = datetime(2026, 8, 25, 11, 0, tzinfo=UTC)
    members = [
        {
            "headline": "Chelsea confirm Premier League squad for the new season",
            "published_at": older,
            "family": "rss",
            "event_kind": "entity",
        },
        {
            "headline": "Fulham 2 - 3 Chelsea | Premier League 2026/27 Simulation | efootball Game Match",
            "published_at": newer,
            "family": "youtube",
            "event_kind": "entity",
        },
    ]
    quote = el._pick_evidence_quote(members)
    assert quote == "Chelsea confirm Premier League squad for the new season"


def test_pick_evidence_quote_still_returns_a_video_title_when_that_is_all_there_is():
    # Demotion, not exclusion. A youtube-only cluster keeps its quote rather
    # than losing the event, and the freshest one still wins inside that tier.
    older = datetime(2026, 8, 25, 6, 0, tzinfo=UTC)
    newer = datetime(2026, 8, 25, 11, 0, tzinfo=UTC)
    members = [
        {"headline": "Older video", "published_at": older, "family": "youtube"},
        {"headline": "Fresher video", "published_at": newer, "family": "youtube"},
    ]
    assert el._pick_evidence_quote(members) == "Fresher video"
