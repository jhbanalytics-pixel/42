from datetime import UTC, datetime

from src.api import bq, creator


def test_creator_post_projection_retains_source_url_and_dates(monkeypatch):
    stamp = datetime.now(UTC)
    calls = []
    row = {
        "id": "post_1",
        "text": "A source-backed observation from a music event.",
        "platform": "tiktok",
        "market": "za",
        "author_handle": "maker",
        "url": "https://example.test/post_1",
        "published_at": stamp,
        "collected_at": stamp,
        "topic_groups": ["music_amapiano"],
    }
    monkeypatch.setattr(
        bq, "_run_query", lambda sql, params: calls.append(sql) or [row]
    )
    post = bq.fetch_creator_posts("maker")[0]
    card = creator._wall_card(post)
    assert "url" in calls[0].split("FROM")[0]
    assert card["id"] == row["id"]
    assert card["url"] == row["url"]
    assert card["published_at"] == stamp
    assert card["collected_at"] == stamp


def test_creator_missing_source_metadata_stays_null():
    card = creator._wall_card(
        {"id": "post_1", "text": "An observation without source dates."}
    )
    assert card["url"] is None
    assert card["published_at"] is None
    assert card["collected_at"] is None
