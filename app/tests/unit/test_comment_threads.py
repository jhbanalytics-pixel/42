"""LP-R-THREADS: post comment threads on voice proof refs."""

from src.api import bq, persona_registry, research


def test_extract_tiktok_video_id_from_url():
    url = "https://www.tiktok.com/@diaspora_japa/video/7123456789012345678"
    assert bq._extract_tiktok_video_id(url) == "7123456789012345678"


def test_extract_tiktok_video_id_from_mobile_share_url():
    url = (
        "https://m.tiktok.com/v/7651738653055618318.html"
        "?u_code=0&preview_pb=0&sharer_language=en&_d=e7h9fl154b7bk4"
        "&share_item_id=7651738653055618318&source=h5_m"
    )
    assert bq._extract_tiktok_video_id(url) == "7651738653055618318"


def test_extract_tiktok_video_id_from_share_item_id_only():
    url = "https://www.tiktok.com/share?share_item_id=7649859313300720910&source=h5_m"
    assert bq._extract_tiktok_video_id(url) == "7649859313300720910"


def test_extract_tiktok_video_id_from_photo_url():
    url = "https://www.tiktok.com/@someone/photo/7601234567890123456?_r=1"
    assert bq._extract_tiktok_video_id(url) == "7601234567890123456"


def test_extract_tiktok_video_id_no_match():
    assert bq._extract_tiktok_video_id("") == ""
    assert bq._extract_tiktok_video_id("https://www.tiktok.com/@someone") == ""


def test_post_parent_keys_tiktok_url():
    keys = bq._post_parent_keys(
        {
            "platform": "tiktok",
            "url": "https://www.tiktok.com/@diaspora_japa/video/7123456789012345678",
            "id": "post-row-1",
        }
    )
    assert "7123456789012345678" in keys
    assert "post-row-1" in keys


def test_fetch_comment_threads_for_posts(monkeypatch):
    post = {
        "id": "post-1",
        "market": "za",
        "platform": "tiktok",
        "url": "https://www.tiktok.com/@diaspora_japa/video/999888777",
    }

    def fake_run(sql, params=None):
        assert "query_term IN UNNEST(@parent_keys)" in sql
        return [
            {
                "id": "c1",
                "market": "za",
                "platform": "tiktok",
                "text": "this cosplay is fire",
                "author_handle": "@fan1",
                "url": "",
                "engagement_total": 12,
                "published_at": None,
                "content_type": "tiktok_comment",
                "query_term": "999888777",
            },
            {
                "id": "c2",
                "market": "za",
                "platform": "tiktok",
                "text": "where is this from",
                "author_handle": "@fan2",
                "url": "",
                "engagement_total": 4,
                "published_at": None,
                "content_type": "tiktok_comment",
                "query_term": "999888777",
            },
            {
                "id": "c3",
                "market": "za",
                "platform": "tiktok",
                "text": "need the fit breakdown",
                "author_handle": "@fan3",
                "url": "",
                "engagement_total": 2,
                "published_at": None,
                "content_type": "tiktok_comment",
                "query_term": "999888777",
            },
        ]

    monkeypatch.setattr(bq, "_run_query", fake_run)
    out = bq.fetch_comment_threads_for_posts([post], per_post=10)
    assert len(out["post-1"]) == 3
    assert out["post-1"][0]["text"] == "this cosplay is fire"


def test_posts_candidates_attach_thread_comments():
    rows = [
        {
            "id": "p1",
            "market": "za",
            "platform": "tiktok",
            "text": "Jester Miku cosplay",
            "author_handle": "@diaspora_japa",
            "url": "https://www.tiktok.com/@diaspora_japa/video/111",
            "engagement_total": 900,
            "topic_group": "fashion_ankara_asoebi",
            "content_type": "post",
            "voice_kind": "post",
            "thread_comments": [
                {"text": "a", "handle": "@x", "engagement": 1, "platform": "tiktok"},
                {"text": "b", "handle": "@y", "engagement": 2, "platform": "tiktok"},
                {"text": "c", "handle": "@z", "engagement": 3, "platform": "tiktok"},
            ],
        }
    ]
    refs = research._posts_candidates(rows, "2026-07-01")
    assert len(refs) == 1
    assert len(refs[0]["thread_comments"]) == 3
    assert refs[0]["thread_size"] == 3


def test_rank_and_filter_prefers_posts_with_threads():
    persona = persona_registry.get_persona("audience_neutral")
    candidates = [
        {
            "id": "thin-post",
            "ref_type": "post",
            "market": "za",
            "query_group": "tech_gemini_ai",
            "text": "post without thread",
            "relevance_score": 0.9,
        },
        {
            "id": "rich-post",
            "ref_type": "post",
            "market": "za",
            "query_group": "tech_gemini_ai",
            "text": "post with conversation",
            "thread_comments": [{"text": "y", "handle": "@a", "engagement": 1, "platform": "tiktok"}] * 4,
            "relevance_score": 0.9,
        },
    ]
    refs = persona_registry.rank_and_filter(candidates, persona)
    post_refs = [r for r in refs if r.get("ref_type") == "post"]
    assert post_refs[0]["id"] == "rich-post"
    assert float(post_refs[0].get("relevance_score") or 0) > float(post_refs[1].get("relevance_score") or 0)
