"""The Listen feed carries people speaking, never a stat digest.

An aggregator once landed top_author, trending_link and trending_hashtag rows
in enriched_content with synthetic text and a real platform name. The feed used
to exclude them by source name; the source is retired, so the guard is the
content_type column the pipeline carries plus the aggregate-text predicate for
any row that arrives untagged.
"""

from datetime import datetime, timezone

from src.api import bq

STAMP = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


def _row(text, content_type="post", platform="tiktok"):
    return {
        "market": "za",
        "platform": platform,
        "title": None,
        "text": text,
        "url": "https://example.test/p/1",
        "published_at": STAMP,
        "sent": 1,
        "content_type": content_type,
    }


def test_listen_feed_sql_excludes_the_aggregate_content_types(monkeypatch):
    seen = {}

    def fake_run(sql, params):
        seen["sql"] = sql
        seen["params"] = {name: value for name, _typ, value in params}
        return []

    monkeypatch.setattr(bq, "_run_query", fake_run)
    bq.listen_feed("za")
    assert (
        "LOWER(IFNULL(content_type, '')) NOT IN UNNEST(@aggregate_types)" in seen["sql"]
    )
    assert set(seen["params"]["aggregate_types"]) == {
        "top_author",
        "trending_link",
        "trending_hashtag",
    }


def test_listen_feed_drops_aggregate_text_and_keeps_a_real_post(monkeypatch):
    rows = [
        _row(
            "#amapiano trending in South Africa trends. 7401 mentions, reach 23,424,833"
        ),
        _row(
            "Link shared 312 times in South Africa trends. 9,120 mentions, reach 4,000,120"
        ),
        _row("Load shedding is back and my whole street is on the braai tonight"),
    ]
    monkeypatch.setattr(bq, "_run_query", lambda sql, params: rows)
    out = bq.listen_feed("za")["results"]
    assert [r["content"] for r in out] == [
        "Load shedding is back and my whole street is on the braai tonight"
    ]
