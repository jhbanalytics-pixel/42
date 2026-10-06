"""LP-R-EVIDENCE: widened gather should retain plentiful refs when BQ has data."""

from datetime import date

import pytest

from src.api import bq, persona_registry, research

FROZEN = date(2026, 6, 15)


def _voice_rows_for_groups(groups: list, markets: list) -> list:
    rows = []
    for mk in markets:
        for g in groups:
            for j in range(12):
                rows.append(
                    {
                        "id": f"{mk}:{g}:post:{j}",
                        "market": mk,
                        "topic_group": g,
                        "topic_groups": [g],
                        "platform": "tiktok",
                        "text": f"Proof post {j} for {g} in {mk}",
                        "engagement_total": 500 - j,
                        "content_type": "post",
                        "voice_kind": "post",
                        "url": f"https://tiktok.com/@u/{mk}/{g}/{j}",
                    }
                )
            for j in range(8):
                rows.append(
                    {
                        "id": f"{mk}:{g}:comment:{j}",
                        "market": mk,
                        "topic_group": g,
                        "topic_groups": [g],
                        "platform": "tiktok",
                        "text": f"Proof comment {j} for {g}",
                        "engagement_total": 80 - j,
                        "content_type": "tiktok_comment",
                        "voice_kind": "comment",
                        "url": "",
                    }
                )
    return rows


@pytest.fixture(autouse=True)
def _reset_registry():
    persona_registry._registry_cache = None
    persona_registry._yaml_meta_cache = None
    yield
    persona_registry._registry_cache = None
    persona_registry._yaml_meta_cache = None


def test_build_research_evidence_retains_wide_voice_pool(monkeypatch):
    monkeypatch.setattr(bq, "latest_trend_date", lambda: FROZEN)
    monkeypatch.setattr(
        bq,
        "fetch_research_seeds",
        lambda *a, **k: [
            {
                "behaviour": "fintech exit planning on mobile money rails",
                "the_shift": "Creators compare japa routes",
                "evidence": "ng diaspora_japa",
                "markets": ["ng"],
                "trend_date": FROZEN,
            },
            {
                "behaviour": "ai adoption scripts for relocation research",
                "the_shift": "Gemini plans the move",
                "evidence": "ng diaspora_japa",
                "markets": ["ng"],
                "trend_date": FROZEN,
            },
        ],
    )
    monkeypatch.setattr(bq, "fetch_research_digest", lambda *a, **k: {"through_line": "Japa week", "summary_text": "Exit talk"})
    monkeypatch.setattr(
        bq,
        "fetch_topic_briefs_for_markets",
        lambda groups, markets, trend_date=None: [
            {
                "market": "ng",
                "query_group": groups[0],
                "trend_score": 0.8 - (i * 0.01),
                "velocity_score": 0.7,
                "search_velocity_score": 0.6,
                "item_count": 100 - i,
                "headline": f"Diaspora japa line {i}",
                "trend_synthesis": "Nigerians discuss relocation paths",
                "cultural_context": "Japa as exit strategy",
            }
            for i in range(4)
        ],
    )
    monkeypatch.setattr(
        bq,
        "fetch_rising_search_terms_for_markets",
        lambda markets, anchors, trend_date=None: [
            {
                "market": "ng",
                "query_group": "diaspora_japa",
                "search_velocity_score": 0.7,
                "trend_score": 0.6,
            },
            {
                "market": "ng",
                "query_group": "diaspora_japa",
                "search_velocity_score": 0.65,
                "trend_score": 0.55,
            },
        ],
    )
    monkeypatch.setattr(
        bq,
        "fetch_lexicon_rows_for_markets",
        lambda markets: [
            {"market": "ng", "term": "hustle"},
            {"market": "ng", "term": "gemini"},
        ],
    )
    def fake_voice(markets, groups, **kw):
        return _voice_rows_for_groups(groups, [m.lower() for m in markets])

    monkeypatch.setattr(bq, "fetch_posts_and_comments_for_topics", fake_voice)
    monkeypatch.setattr(bq, "fetch_comment_threads_for_posts", lambda posts, per_post=10: {})
    monkeypatch.setattr(
        bq,
        "fetch_research_posts",
        lambda markets, groups, limit=80: _voice_rows_for_groups(groups, [m.lower() for m in markets])[:limit],
    )

    ev = research.build_research_evidence(
        "audience_neutral",
        ["ng"],
        trend_date=FROZEN,
        focus_query_groups=["diaspora_japa"],
    )
    assert len(ev["refs"]) >= 25
    voice_refs = [r for r in ev["refs"] if r.get("ref_type") in ("post", "comment")]
    assert len(voice_refs) >= 10
    assert any(r.get("ref_type") == "comment" for r in voice_refs)
    ids = [r.get("id") for r in voice_refs if r.get("id")]
    assert len(ids) == len(set(ids))


def test_rank_and_filter_dedupes_by_id_not_text():
    persona = persona_registry.get_persona("audience_neutral")
    persona = {**persona, "query_groups": ["diaspora_japa"]}
    dup_text = "nakamura tokyo diaspora thread"
    candidates = [
        {
            "id": "row-a",
            "ref_type": "post",
            "market": "ng",
            "query_group": "diaspora_japa",
            "text": dup_text,
        },
        {
            "id": "row-b",
            "ref_type": "post",
            "market": "ng",
            "query_group": "diaspora_japa",
            "text": dup_text,
        },
        {
            "id": "row-c",
            "ref_type": "post",
            "market": "ng",
            "query_group": "diaspora_japa",
            "text": "different proof line",
        },
    ]
    refs = persona_registry.rank_and_filter(candidates, persona)
    assert len(refs) == 3
