"""Voice fetch helpers: posts+comments pools and metric aggregates."""

from src.api import bq


def test_voice_kind_tags_comments():
    assert bq._voice_kind("tiktok_comment") == "comment"
    assert bq._voice_kind("instagram_post_comment") == "comment"
    assert bq._voice_kind("threads_reply") == "comment"
    assert bq._voice_kind("post") == "post"
    assert bq._voice_kind(None) == "post"


def test_fetch_posts_and_comments_builds_union_query(monkeypatch):
    captured = {}

    def fake_run(sql, params=None):
        captured["sql"] = sql
        captured["params"] = dict((p[0], p[2]) for p in (params or []))
        return [
            {
                "id": "p1",
                "market": "ng",
                "platform": "tiktok",
                "text": "sapa is real",
                "title": "",
                "author_handle": "@a",
                "url": "https://tiktok.com/@a/1",
                "engagement_total": 900,
                "published_at": None,
                "topic_group": "economy_sapa_hustle",
                "topic_groups": ["economy_sapa_hustle"],
                "slang_terms": "",
                "content_type": "post",
                "voice_kind": "post",
            },
            {
                "id": "c1",
                "market": "ng",
                "platform": "instagram",
                "text": "same here bru",
                "title": "",
                "author_handle": "@b",
                "url": "",
                "engagement_total": 40,
                "published_at": None,
                "topic_group": "economy_sapa_hustle",
                "topic_groups": ["economy_sapa_hustle"],
                "slang_terms": "",
                "content_type": "instagram_post_comment",
                "voice_kind": "comment",
            },
        ]

    monkeypatch.setattr(bq, "_run_query", fake_run)
    rows = bq.fetch_posts_and_comments_for_topics(
        ["ng"], ["economy_sapa_hustle"], per_topic_posts=3, per_topic_comments=5
    )
    assert "UNION ALL" in captured["sql"]
    assert captured["params"]["per_posts"] == 3
    assert captured["params"]["per_comments"] == 5
    assert len(rows) == 2
    kinds = {r["voice_kind"] for r in rows}
    assert kinds == {"post", "comment"}


def test_fetch_voice_metrics_groups_by_topic(monkeypatch):
    def fake_run(sql, params=None):
        return [
            {
                "market": "ng",
                "topic_group": "economy_sapa_hustle",
                "post_count": 12,
                "comment_count": 8,
                "engagement_total": 4200,
                "platform_count": 3,
            }
        ]

    monkeypatch.setattr(bq, "_run_query", fake_run)
    out = bq.fetch_voice_metrics_for_topics(["ng"], ["economy_sapa_hustle"])
    assert out[("ng", "economy_sapa_hustle")]["comment_count"] == 8
    assert out[("ng", "economy_sapa_hustle")]["post_count"] == 12
