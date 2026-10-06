"""Unit tests for YouTubeConnector."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import responses as responses_lib
from src.ingestion.connectors.youtube import (
    SEARCH_URL,
    VIDEOS_URL,
    YouTubeConnector,
)

SOURCES_ZA = {
    "youtube_queries": {
        "za": {
            "region_code": "ZA",
            "queries": ["amapiano south africa", "south africa gen z"],
        }
    }
}


def _search_response(video_ids: list[str], channel_title: str = "Test Channel") -> dict:
    """Build a fake search.list response body."""
    return {
        "items": [
            {
                "id": {"videoId": vid},
                "snippet": {
                    "channelTitle": channel_title,
                    "channelId": f"UC_{vid}",
                    "title": f"Video {vid}",
                    "description": f"Description for {vid}",
                    "publishedAt": "2026-04-19T12:00:00Z",
                },
            }
            for vid in video_ids
        ]
    }


def _videos_response(stats_by_id: dict[str, dict]) -> dict:
    """Build a fake videos.list response body."""
    return {"items": [{"id": vid, "statistics": stats} for vid, stats in stats_by_id.items()]}


def _make_connector(market: str = "za") -> YouTubeConnector:
    connector = YouTubeConnector(market=market)
    connector.RATE_LIMIT_DELAY = 0
    return connector


@responses_lib.activate
@patch("src.ingestion.connectors.youtube.load_sources", return_value=SOURCES_ZA)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_fetch_v2_columns_are_empty_strings_not_nan(_mock_secret, _mock_sources):
    """Row builders omit the four GDELT v2 fields, so the forced RAW_COLUMNS
    arrive as NaN; run_rss_now._s would stringify NaN to the literal "nan" in
    raw_content, violating the "empty string elsewhere" contract. They must be
    empty strings."""
    responses_lib.add(responses_lib.GET, SEARCH_URL, json=_search_response(["vid1"]), status=200)
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response(
            {"vid1": {"viewCount": "500", "likeCount": "10", "commentCount": "2"}}
        ),
        status=200,
    )

    df = _make_connector("za").fetch()

    assert len(df) == 1
    for col in ("v2tone", "v2persons", "v2orgs", "v2locations"):
        assert df[col].tolist() == [""], f"{col} should be '' not {df[col].tolist()}"


@responses_lib.activate
@patch("src.ingestion.connectors.youtube.load_sources", return_value=SOURCES_ZA)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_fetch_returns_raw_columns(_mock_secret, _mock_sources):
    """Happy path: 16-column DataFrame with one row per unique videoId."""
    responses_lib.add(
        responses_lib.GET,
        SEARCH_URL,
        json=_search_response(["vid1", "vid2"]),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        SEARCH_URL,
        json=_search_response(["vid3"]),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response(
            {
                "vid1": {"viewCount": "500", "likeCount": "10", "commentCount": "2"},
                "vid2": {"viewCount": "750", "likeCount": "15", "commentCount": "3"},
                "vid3": {"viewCount": "100", "likeCount": "4", "commentCount": "1"},
            }
        ),
        status=200,
    )

    df = _make_connector("za").fetch()

    from src.ingestion.connectors.base import RAW_COLUMNS

    assert len(df) == 3
    assert list(df.columns) == list(RAW_COLUMNS)
    assert (df["platform"] == "youtube").all()
    assert (df["market"] == "za").all()
    assert (df["content_type"] == "video").all()
    # search.list rows self-label so they are not rewritten to "news" downstream
    assert (df["query_group"] == "youtube_search").all()
    assert set(df["url"]) == {
        "https://www.youtube.com/watch?v=vid1",
        "https://www.youtube.com/watch?v=vid2",
        "https://www.youtube.com/watch?v=vid3",
    }


@responses_lib.activate
@patch("src.ingestion.connectors.youtube.load_sources", return_value=SOURCES_ZA)
@patch("src.ingestion.connectors.youtube.get_secret", return_value=None)
def test_fetch_when_api_key_missing_returns_empty(_mock_secret, _mock_sources):
    """No API key = empty DataFrame, no HTTP calls."""
    df = _make_connector("za").fetch()

    from src.ingestion.connectors.base import RAW_COLUMNS

    assert len(df) == 0
    assert list(df.columns) == list(RAW_COLUMNS)
    assert len(responses_lib.calls) == 0


@responses_lib.activate
@patch("src.ingestion.connectors.youtube.load_sources", return_value=SOURCES_ZA)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_fetch_handles_quota_exceeded_gracefully(_mock_secret, _mock_sources):
    """A 403 quotaExceeded on the first search call returns an empty frame, no crash."""
    quota_body = {
        "error": {
            "code": 403,
            "message": "The request cannot be completed because you have exceeded your quota.",
            "errors": [{"reason": "quotaExceeded", "domain": "youtube.quota"}],
        }
    }
    # urllib3 Retry treats 403 as non-retryable, so one registered response is enough.
    responses_lib.add(
        responses_lib.GET,
        SEARCH_URL,
        json=quota_body,
        status=403,
    )

    df = _make_connector("za").fetch()

    assert len(df) == 0
    assert "source" in df.columns


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={
        "youtube_queries": {
            "za": {
                "region_code": "ZA",
                "queries": ["amapiano south africa", "south africa gen z"],
            }
        }
    },
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_fetch_dedupes_on_video_id(_mock_secret, _mock_sources):
    """Duplicate videoIds across search responses collapse to one row."""
    responses_lib.add(
        responses_lib.GET,
        SEARCH_URL,
        json=_search_response(["vidA", "vidB"]),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        SEARCH_URL,
        json=_search_response(["vidA", "vidC"]),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response(
            {
                "vidA": {"viewCount": "10"},
                "vidB": {"viewCount": "20"},
                "vidC": {"viewCount": "30"},
            }
        ),
        status=200,
    )

    df = _make_connector("za").fetch()

    assert len(df) == 3
    assert set(df["url"]) == {
        "https://www.youtube.com/watch?v=vidA",
        "https://www.youtube.com/watch?v=vidB",
        "https://www.youtube.com/watch?v=vidC",
    }


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={"youtube_queries": {"za": {"region_code": "ZA", "queries": ["test term"]}}},
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_fetch_maps_statistics_correctly(_mock_secret, _mock_sources):
    """viewCount, likeCount, commentCount are mapped onto the row and coerced to int."""
    responses_lib.add(
        responses_lib.GET,
        SEARCH_URL,
        json=_search_response(["stats_vid"]),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response(
            {
                "stats_vid": {
                    "viewCount": "1000",
                    "likeCount": "50",
                    "commentCount": "7",
                }
            }
        ),
        status=200,
    )

    df = _make_connector("za").fetch()

    assert len(df) == 1
    row = df.iloc[0]
    assert int(row["views"]) == 1000
    assert int(row["likes"]) == 50
    assert int(row["comments"]) == 7
    assert int(row["shares"]) == 0


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={
        "youtube_queries": {
            "za": {
                "region_code": "ZA",
                "queries": ["term one", "term two"],
                "max_calls_per_run": 1,
            }
        }
    },
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_fetch_respects_max_calls_per_run(_mock_secret, _mock_sources):
    """Two queries configured but max_calls_per_run=1 issues only one search.list."""
    responses_lib.add(
        responses_lib.GET,
        SEARCH_URL,
        json=_search_response(["only_vid"]),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response({"only_vid": {"viewCount": "5"}}),
        status=200,
    )

    df = _make_connector("za").fetch()

    search_calls = [c for c in responses_lib.calls if SEARCH_URL in c.request.url]
    videos_calls = [c for c in responses_lib.calls if VIDEOS_URL in c.request.url]
    assert len(search_calls) == 1
    assert len(videos_calls) == 1
    assert len(df) == 1


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={"youtube_queries": {"za": {"region_code": "ZA", "queries": ["test term"]}}},
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_fetch_published_at_is_tz_aware_datetime(_mock_secret, _mock_sources):
    """publishedAt ISO string is parsed to a tz-aware datetime, not stored as raw string."""
    responses_lib.add(
        responses_lib.GET,
        SEARCH_URL,
        json={
            "items": [
                {
                    "id": {"videoId": "dt_vid"},
                    "snippet": {
                        "channelTitle": "DT Channel",
                        "channelId": "UC_dt",
                        "title": "Datetime video",
                        "description": "d",
                        "publishedAt": "2026-04-19T12:00:00Z",
                    },
                }
            ]
        },
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response({"dt_vid": {"viewCount": "1"}}),
        status=200,
    )

    df = _make_connector("za").fetch()

    assert len(df) == 1
    published = df.iloc[0]["published_at"]
    assert isinstance(published, datetime)
    assert published.tzinfo is not None
    assert published.year == 2026


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={"youtube_queries": {"za": {"region_code": "ZA", "queries": ["test term"]}}},
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_fetch_handles_missing_published_at(_mock_secret, _mock_sources):
    """Missing publishedAt on a snippet becomes None, not an empty string."""
    responses_lib.add(
        responses_lib.GET,
        SEARCH_URL,
        json={
            "items": [
                {
                    "id": {"videoId": "no_dt_vid"},
                    "snippet": {
                        "channelTitle": "NoDT Channel",
                        "channelId": "UC_nodt",
                        "title": "No date video",
                        "description": "d",
                        # publishedAt deliberately absent
                    },
                }
            ]
        },
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response({"no_dt_vid": {"viewCount": "1"}}),
        status=200,
    )

    df = _make_connector("za").fetch()

    assert len(df) == 1
    published = df.iloc[0]["published_at"]
    assert published is None or (hasattr(published, "__class__") and published != "")
    # Strict check: must be None, not empty string
    assert published is None


@responses_lib.activate
@patch("src.ingestion.connectors.youtube.load_sources", return_value=SOURCES_ZA)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_fetch_chunks_videos_list_when_over_50_ids(_mock_secret, _mock_sources):
    """videos.list rejects >50 ids per call. Connector must chunk into batches.

    Regression guard: a search return of >50 unique videoIds previously
    produced a single videos.list call with all ids comma-joined, which
    YouTube rejected with HTTP 400 and dropped stats enrichment for the
    whole market (logged as ``URL truncated mid-id list`` in cloud cron
    flags 2026-04-27).
    """
    from urllib.parse import parse_qs, urlparse

    from src.ingestion.connectors.youtube import VIDEOS_LIST_MAX_IDS

    # Build 120 unique videoIds across 2 search calls (60 each)
    ids_a = [f"vid_a_{i:03d}" for i in range(60)]
    ids_b = [f"vid_b_{i:03d}" for i in range(60)]
    all_ids = ids_a + ids_b

    responses_lib.add(
        responses_lib.GET,
        SEARCH_URL,
        json=_search_response(ids_a),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        SEARCH_URL,
        json=_search_response(ids_b),
        status=200,
    )
    # One stub per expected videos.list batch (50 + 50 + 20 for 120 ids).
    for batch_start in range(0, len(all_ids), VIDEOS_LIST_MAX_IDS):
        batch = all_ids[batch_start : batch_start + VIDEOS_LIST_MAX_IDS]
        responses_lib.add(
            responses_lib.GET,
            VIDEOS_URL,
            json=_videos_response(
                {vid: {"viewCount": "100", "likeCount": "1", "commentCount": "0"} for vid in batch}
            ),
            status=200,
        )

    df = _make_connector("za").fetch()

    # Stats enrichment landed for every videoId
    assert len(df) == 120
    assert df["views"].sum() == 120 * 100

    # Three videos.list calls were made (50 + 50 + 20)
    videos_calls = [c for c in responses_lib.calls if VIDEOS_URL in c.request.url]
    assert len(videos_calls) == 3

    # Every videos.list call carried <= VIDEOS_LIST_MAX_IDS ids
    for call in videos_calls:
        qs = parse_qs(urlparse(call.request.url).query)
        ids_in_call = qs["id"][0].split(",")
        assert len(ids_in_call) <= VIDEOS_LIST_MAX_IDS


# ---------------------------------------------------------------------------
# Trending (chart=mostPopular)
# ---------------------------------------------------------------------------


def _trending_response(video_ids: list[str], category_id: str = "10") -> dict:
    """Build a fake videos.list?chart=mostPopular response body."""
    return {
        "items": [
            {
                "id": vid,
                "snippet": {
                    "channelTitle": f"Channel {vid}",
                    "channelId": f"UC_chart_{vid}",
                    "title": f"Trending {vid}",
                    "description": f"Description for trending {vid}",
                    "publishedAt": "2026-05-25T08:00:00Z",
                    "categoryId": category_id,
                },
                "statistics": {
                    "viewCount": "1000000",
                    "likeCount": "50000",
                    "commentCount": "1000",
                },
            }
            for vid in video_ids
        ]
    }


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={
        "youtube_queries": {
            "za": {
                "region_code": "ZA",
                "queries": ["amapiano south africa"],
                "trending_enabled": True,
            }
        }
    },
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_videos_list_quota_short_circuits_trending(_mock_secret, _mock_sources):
    """A quotaExceeded on videos.list (stats) stops the trending call from firing.

    Without the quota_exceeded flag the trending endpoint fires next and 403s.
    """
    quota_body = {
        "error": {
            "code": 403,
            "message": "quota exceeded",
            "errors": [{"reason": "quotaExceeded", "domain": "youtube.quota"}],
        }
    }
    responses_lib.add(
        responses_lib.GET, SEARCH_URL, json=_search_response(["search_vid"]), status=200
    )
    # videos.list stats call: quota gone.
    responses_lib.add(responses_lib.GET, VIDEOS_URL, json=quota_body, status=403)
    # A trending response is registered but must NOT be consumed.
    responses_lib.add(
        responses_lib.GET, VIDEOS_URL, json=_trending_response(["chart_vid"]), status=200
    )

    df = _make_connector("za").fetch()

    # Only the search row survives; no trending row landed.
    assert set(df["url"]) == {"https://www.youtube.com/watch?v=search_vid"}
    # Exactly one VIDEOS_URL call (stats); the trending call never fired.
    videos_calls = [c for c in responses_lib.calls if c.request.url.startswith(VIDEOS_URL)]
    assert len(videos_calls) == 1


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={
        "youtube_queries": {
            "za": {
                "region_code": "ZA",
                "queries": ["amapiano south africa"],
                "trending_enabled": True,
            }
        }
    },
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_fetch_trending_appends_chart_rows(_mock_secret, _mock_sources):
    """trending_enabled=true triggers a chart=mostPopular call after search+stats."""
    responses_lib.add(
        responses_lib.GET, SEARCH_URL, json=_search_response(["search_vid"]), status=200
    )
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response({"search_vid": {"viewCount": "10"}}),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_trending_response(["chart_vid_a", "chart_vid_b"]),
        status=200,
    )

    df = _make_connector("za").fetch()

    assert set(df["url"]) == {
        "https://www.youtube.com/watch?v=search_vid",
        "https://www.youtube.com/watch?v=chart_vid_a",
        "https://www.youtube.com/watch?v=chart_vid_b",
    }
    # The two chart rows should be tagged query_group=youtube_trending and
    # carry stats from the mostPopular response, not zeros.
    chart_rows = df[df["url"].str.contains("chart_vid")]
    assert (chart_rows["query_group"] == "youtube_trending").all()
    assert (chart_rows["views"] == 1_000_000.0).all()
    assert (chart_rows["content_type"] == "video/10").all()


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={
        "youtube_queries": {
            "za": {
                "region_code": "ZA",
                "queries": ["amapiano south africa"],
                "trending_enabled": True,
            }
        }
    },
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_fetch_trending_dedupes_against_search(_mock_secret, _mock_sources):
    """A video appearing in both search and chart only contributes once."""
    responses_lib.add(responses_lib.GET, SEARCH_URL, json=_search_response(["dup_vid"]), status=200)
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response({"dup_vid": {"viewCount": "10"}}),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_trending_response(["dup_vid", "fresh_vid"]),
        status=200,
    )

    df = _make_connector("za").fetch()

    assert len(df) == 2
    assert set(df["url"]) == {
        "https://www.youtube.com/watch?v=dup_vid",
        "https://www.youtube.com/watch?v=fresh_vid",
    }


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={
        "youtube_queries": {
            "za": {
                "region_code": "ZA",
                "queries": ["amapiano south africa"],
                # trending_enabled omitted -> defaults to False
            }
        }
    },
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_fetch_trending_disabled_does_not_call_chart(_mock_secret, _mock_sources):
    """Without trending_enabled the connector issues only search.list + one videos.list."""
    responses_lib.add(responses_lib.GET, SEARCH_URL, json=_search_response(["v1"]), status=200)
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response({"v1": {"viewCount": "5"}}),
        status=200,
    )

    df = _make_connector("za").fetch()

    videos_calls = [c for c in responses_lib.calls if VIDEOS_URL in c.request.url]
    # Only the stats batch should fire. chart=mostPopular call would be a 2nd.
    assert len(videos_calls) == 1
    chart_calls = [c for c in videos_calls if "chart=mostPopular" in c.request.url]
    assert chart_calls == []
    assert len(df) == 1


SOURCES_ZA_ONE_TERM = {
    "youtube_queries": {
        "za": {
            "region_code": "ZA",
            "queries": ["amapiano south africa"],
        }
    }
}


@responses_lib.activate
@patch("src.ingestion.connectors.youtube.load_sources", return_value=SOURCES_ZA_ONE_TERM)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_search_cache_key_stable_within_day_across_reentrant_run(_mock_secret, _mock_sources):
    """A re-entrant fetch later the same day must reuse the search cache.

    published_after feeds the _SEARCH_CACHE key. When it was derived from
    datetime.now at second resolution, any retry more than one second later
    minted a fresh key and re-charged 100 quota units per term. Flooring to
    midnight UTC pins the key for the whole calendar day, so the second run
    hits the cache and search.list fires exactly once across both runs.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _real_datetime

    responses_lib.add(responses_lib.GET, SEARCH_URL, json=_search_response(["vid1"]), status=200)
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response({"vid1": {"viewCount": "5"}}),
        status=200,
    )

    # Two wall-clock readings several seconds apart but on the same UTC day.
    clock = [
        _real_datetime(2026, 4, 26, 6, 30, 12, 100000, tzinfo=_UTC),
        _real_datetime(2026, 4, 26, 6, 30, 31, 900000, tzinfo=_UTC),
    ]

    class _FakeDateTime(_real_datetime):
        _calls = 0

        @classmethod
        def now(cls, tz=None):
            value = clock[min(cls._calls, len(clock) - 1)]
            cls._calls += 1
            return value

    with patch("src.ingestion.connectors.youtube.datetime", _FakeDateTime):
        _make_connector("za").fetch()
        # Second run inside the same calendar day. Cache must be reused.
        _make_connector("za").fetch()

    search_calls = [c for c in responses_lib.calls if SEARCH_URL in c.request.url]
    assert len(search_calls) == 1, "re-entrant same-day run re-charged search.list quota"
    # The window is floored to midnight UTC minus 7 days, not second resolution.
    assert "publishedAfter=2026-04-19T00%3A00%3A00Z" in search_calls[0].request.url


# ---------------------------------------------------------------------------
# Comment threads enrichment (Phase 2, 27 May 2026)
# ---------------------------------------------------------------------------


from src.ingestion.connectors.youtube import COMMENT_THREADS_URL


def _comment_threads_response(texts: list[str]) -> dict:
    """Build a fake commentThreads.list response body."""
    return {
        "items": [
            {
                "snippet": {
                    "topLevelComment": {
                        "snippet": {
                            "textDisplay": t,
                            "authorDisplayName": "Fan",
                            "publishedAt": "2026-05-25T10:00:00Z",
                        }
                    }
                }
            }
            for t in texts
        ]
    }


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={
        "youtube_queries": {
            "za": {
                "region_code": "ZA",
                "queries": ["amapiano south africa"],
                "comment_threads_enabled": True,
                "comment_videos_per_run": 2,
            }
        }
    },
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_comment_threads_appended_to_text(_mock_secret, _mock_sources):
    """Top comments concat into the row text for downstream slang detection."""
    responses_lib.add(
        responses_lib.GET,
        SEARCH_URL,
        json=_search_response(["vidHi", "vidLow"]),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response(
            {
                "vidHi": {"viewCount": "1000000", "likeCount": "1", "commentCount": "1"},
                "vidLow": {"viewCount": "10", "likeCount": "1", "commentCount": "1"},
            }
        ),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        COMMENT_THREADS_URL,
        json=_comment_threads_response(["This is fire! amapiano forever"]),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        COMMENT_THREADS_URL,
        json=_comment_threads_response(["Mid track"]),
        status=200,
    )

    df = _make_connector("za").fetch()

    high_row = df[df["url"].str.contains("vidHi")].iloc[0]
    assert "amapiano forever" in high_row["text"]


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={
        "youtube_queries": {
            "za": {
                "region_code": "ZA",
                "queries": ["amapiano south africa"],
                "comment_threads_enabled": True,
                "comment_videos_per_run": 1,
            }
        }
    },
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_comment_threads_text_is_capped(_mock_secret, _mock_sources):
    """A huge comment block is truncated so one video cannot produce a ~200k
    char row that bloats raw_content and the embedding/classifier haystack."""
    from src.ingestion.connectors.youtube import COMMENT_ENRICHED_TEXT_MAX_CHARS

    responses_lib.add(responses_lib.GET, SEARCH_URL, json=_search_response(["vidBig"]), status=200)
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response(
            {"vidBig": {"viewCount": "1000", "likeCount": "1", "commentCount": "9"}}
        ),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        COMMENT_THREADS_URL,
        json=_comment_threads_response(["x" * 10000 for _ in range(20)]),
        status=200,
    )

    df = _make_connector("za").fetch()

    row = df[df["url"].str.contains("vidBig")].iloc[0]
    assert len(row["text"]) <= COMMENT_ENRICHED_TEXT_MAX_CHARS


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={
        "youtube_queries": {
            "za": {
                "region_code": "ZA",
                "queries": ["amapiano south africa"],
                # comment_threads_enabled omitted -> defaults False
            }
        }
    },
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_comment_threads_disabled_does_not_call(_mock_secret, _mock_sources):
    """Without the flag the connector never hits the commentThreads endpoint."""
    responses_lib.add(
        responses_lib.GET, SEARCH_URL, json=_search_response(["only_vid"]), status=200
    )
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response({"only_vid": {"viewCount": "1"}}),
        status=200,
    )

    _make_connector("za").fetch()

    comment_calls = [c for c in responses_lib.calls if COMMENT_THREADS_URL in c.request.url]
    assert comment_calls == []


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={
        "youtube_queries": {
            "za": {
                "region_code": "ZA",
                "queries": ["amapiano south africa"],
                "comment_threads_enabled": True,
                "comment_videos_per_run": 1,
            }
        }
    },
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_comment_threads_skips_on_403_comments_disabled(_mock_secret, _mock_sources):
    """403 commentsDisabled is logged and skipped, not raised."""
    responses_lib.add(
        responses_lib.GET, SEARCH_URL, json=_search_response(["vid_no_comments"]), status=200
    )
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response({"vid_no_comments": {"viewCount": "100"}}),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        COMMENT_THREADS_URL,
        json={
            "error": {
                "code": 403,
                "message": "Comments are disabled for this video.",
                "errors": [{"reason": "commentsDisabled"}],
            }
        },
        status=403,
    )

    df = _make_connector("za").fetch()

    # Pipeline survives, row still present with original description text.
    assert len(df) == 1
    assert df.iloc[0]["url"].endswith("vid_no_comments")


# ---------------------------------------------------------------------------
# Wave 2: playlistItems channel-deep-dive endpoint tests.
# ---------------------------------------------------------------------------

from src.ingestion.connectors.youtube import _HANDLE_CACHE as _HANDLE_CACHE
from src.ingestion.connectors.youtube import (
    CHANNELS_URL,
    PLAYLIST_ITEMS_CONTENT_TYPE,
    PLAYLIST_ITEMS_QUERY_GROUP,
    PLAYLIST_ITEMS_URL,
)

SOURCES_PLAYLIST_DISABLED = {
    "youtube_queries": {
        "za": {
            "region_code": "ZA",
            "queries": ["amapiano south africa"],
            "playlist_items_enabled": False,
            "playlist_creator_handles": ["@TYLAOfficial"],
        }
    }
}

SOURCES_PLAYLIST_ON = {
    "youtube_queries": {
        "za": {
            "region_code": "ZA",
            "queries": [],
            "playlist_items_enabled": True,
            "playlist_creator_handles": ["@TYLAOfficial"],
            "playlist_items_per_creator": 3,
        }
    }
}


def _channels_response(uploads_playlist_id: str | None) -> dict:
    """Build a fake channels.list response with the uploads playlist id."""
    if uploads_playlist_id is None:
        return {"items": []}
    return {
        "items": [
            {
                "id": "UC_test_channel",
                "contentDetails": {
                    "relatedPlaylists": {"uploads": uploads_playlist_id},
                },
            }
        ]
    }


def _playlist_items_response(video_specs: list[dict]) -> dict:
    """Build a fake playlistItems.list response body.

    Each spec dict supplies vid + optional title, description, channel_title,
    published_at. Mirrors the live envelope shape captured in the 28 May probe.
    """
    items = []
    for spec in video_specs:
        vid = spec["vid"]
        items.append(
            {
                "snippet": {
                    "title": spec.get("title", f"Video {vid}"),
                    "description": spec.get("description", ""),
                    "publishedAt": spec.get("published_at", "2026-05-20T12:00:00Z"),
                    "channelTitle": spec.get("channel_title", "Test Channel"),
                    "videoOwnerChannelTitle": spec.get("channel_title", "Test Channel"),
                    "resourceId": {"kind": "youtube#video", "videoId": vid},
                },
                "contentDetails": {
                    "videoId": vid,
                    "videoPublishedAt": spec.get("published_at", "2026-05-20T12:00:00Z"),
                },
            }
        )
    return {"items": items}


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value=SOURCES_PLAYLIST_DISABLED,
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_playlist_items_disabled_short_circuits(_mock_secret, _mock_sources):
    """playlist_items_enabled: false skips the channels.list + playlistItems calls."""
    responses_lib.add(responses_lib.GET, SEARCH_URL, json=_search_response(["vid1"]), status=200)
    responses_lib.add(
        responses_lib.GET,
        VIDEOS_URL,
        json=_videos_response({"vid1": {"viewCount": "10"}}),
        status=200,
    )
    # If the code under test reached playlistItems we would see an
    # AssertionError from `responses` (no matching stub registered).
    df = _make_connector("za").fetch()

    assert len(df) == 1
    called_urls = [c.request.url for c in responses_lib.calls]
    assert not any(CHANNELS_URL in u for u in called_urls)
    assert not any(PLAYLIST_ITEMS_URL in u for u in called_urls)


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value=SOURCES_PLAYLIST_ON,
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_resolve_handle_to_uploads_playlist_id(_mock_secret, _mock_sources):
    """@handle resolves to channels[0].contentDetails.relatedPlaylists.uploads."""
    responses_lib.add(
        responses_lib.GET,
        CHANNELS_URL,
        json=_channels_response("UU8HOgNWipVorrlBH8oN2R-A"),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        PLAYLIST_ITEMS_URL,
        json=_playlist_items_response([{"vid": "vidA", "channel_title": "Tyla"}]),
        status=200,
    )

    df = _make_connector("za").fetch()

    assert len(df) == 1
    # Confirm channels.list was called with forHandle param.
    channels_calls = [c for c in responses_lib.calls if CHANNELS_URL in c.request.url]
    assert len(channels_calls) == 1
    assert "forHandle=" in channels_calls[0].request.url


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value=SOURCES_PLAYLIST_ON,
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_fetch_playlist_items_extracts_videos(_mock_secret, _mock_sources):
    """playlistItems.list rows surface in the DataFrame with correct fields."""
    responses_lib.add(
        responses_lib.GET,
        CHANNELS_URL,
        json=_channels_response("UU_uploads_playlist"),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        PLAYLIST_ITEMS_URL,
        json=_playlist_items_response(
            [
                {"vid": "vidA", "title": "Tyla Live", "channel_title": "Tyla"},
                {"vid": "vidB", "title": "POP Trailer", "channel_title": "Tyla"},
                {"vid": "vidC", "title": "Studio Vlog", "channel_title": "Tyla"},
            ]
        ),
        status=200,
    )

    df = _make_connector("za").fetch()

    assert len(df) == 3
    assert set(df["title"]) == {"Tyla Live", "POP Trailer", "Studio Vlog"}
    assert (df["query_group"] == PLAYLIST_ITEMS_QUERY_GROUP).all()
    assert (df["content_type"] == PLAYLIST_ITEMS_CONTENT_TYPE).all()


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value=SOURCES_PLAYLIST_ON,
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_playlist_items_normalise_into_raw_columns_schema(_mock_secret, _mock_sources):
    """Each playlistItems row matches RAW_COLUMNS shape with the expected values."""
    from src.ingestion.connectors.base import RAW_COLUMNS

    responses_lib.add(
        responses_lib.GET,
        CHANNELS_URL,
        json=_channels_response("UU_uploads"),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        PLAYLIST_ITEMS_URL,
        json=_playlist_items_response(
            [
                {
                    "vid": "VID_ABC",
                    "title": "New Track",
                    "description": "Drop date Friday",
                    "channel_title": "Tyla",
                    "published_at": "2026-05-21T10:30:00Z",
                }
            ]
        ),
        status=200,
    )

    df = _make_connector("za").fetch()

    assert list(df.columns) == list(RAW_COLUMNS)
    row = df.iloc[0]
    assert row["platform"] == "youtube"
    assert row["market"] == "za"
    assert row["content_type"] == PLAYLIST_ITEMS_CONTENT_TYPE
    assert row["query_group"] == PLAYLIST_ITEMS_QUERY_GROUP
    assert row["author_name"] == "Tyla"
    assert row["author_handle"] == "@TYLAOfficial"
    assert row["title"] == "New Track"
    assert row["text"] == "Drop date Friday"
    assert row["url"] == "https://youtu.be/VID_ABC"
    assert row["views"] == 0.0
    assert row["likes"] == 0.0
    assert row["comments"] == 0.0
    assert row["shares"] == 0.0
    # source falls back to channel title for visibility in run logs.
    assert row["source"] == "Tyla"


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={
        "youtube_queries": {
            "za": {
                "region_code": "ZA",
                "queries": [],
                "playlist_items_enabled": True,
                "playlist_creator_handles": ["@TYLAOfficial", "@tylaofficial"],
                "playlist_items_per_creator": 2,
            }
        }
    },
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_playlist_items_handle_resolution_cached_per_process(_mock_secret, _mock_sources):
    """A repeated @handle (case-insensitive) hits the cache and skips channels.list."""
    # Only ONE channels.list response registered. A second call would 500 or
    # AssertionError out of the responses library.
    responses_lib.add(
        responses_lib.GET,
        CHANNELS_URL,
        json=_channels_response("UU_uploads_shared"),
        status=200,
    )
    # Two playlistItems calls (one per handle) but they share the cached
    # uploads playlist id so only one channels.list happens.
    responses_lib.add(
        responses_lib.GET,
        PLAYLIST_ITEMS_URL,
        json=_playlist_items_response([{"vid": "vidA", "channel_title": "Tyla"}]),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        PLAYLIST_ITEMS_URL,
        json=_playlist_items_response([{"vid": "vidB", "channel_title": "Tyla"}]),
        status=200,
    )

    df = _make_connector("za").fetch()

    assert len(df) == 2
    channels_calls = [c for c in responses_lib.calls if CHANNELS_URL in c.request.url]
    assert len(channels_calls) == 1, "handle resolution should be cached per process"


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={
        "youtube_queries": {
            "za": {
                "region_code": "ZA",
                "queries": [],
                "playlist_items_enabled": True,
                "playlist_creator_handles": ["@DeadHandle", "@TYLAOfficial"],
                "playlist_items_per_creator": 2,
            }
        }
    },
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_playlist_items_404_returns_empty_per_creator(_mock_secret, _mock_sources):
    """A handle that resolves to no channel skips that creator without raising."""
    # First channels.list -> empty items (handle not found, NOT a 404).
    responses_lib.add(responses_lib.GET, CHANNELS_URL, json=_channels_response(None), status=200)
    # Second channels.list -> real uploads playlist for Tyla.
    responses_lib.add(
        responses_lib.GET,
        CHANNELS_URL,
        json=_channels_response("UU_tyla"),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        PLAYLIST_ITEMS_URL,
        json=_playlist_items_response([{"vid": "vidT", "channel_title": "Tyla"}]),
        status=200,
    )

    df = _make_connector("za").fetch()

    # Dead handle drops out, Tyla row survives.
    assert len(df) == 1
    assert df.iloc[0]["author_handle"] == "@TYLAOfficial"


@responses_lib.activate
@patch(
    "src.ingestion.connectors.youtube.load_sources",
    return_value={
        "youtube_queries": {
            "ng": {
                "region_code": "NG",
                "queries": [],
                "playlist_items_enabled": True,
                "playlist_creator_handles": ["@DavidoOfficial"],
                "playlist_items_per_creator": 1,
            },
            "ke": {
                "region_code": "KE",
                "queries": [],
                "playlist_items_enabled": False,
                "playlist_creator_handles": ["@sautisol"],
            },
        }
    },
)
@patch("src.ingestion.connectors.youtube.get_secret", return_value="fake-key")
def test_playlist_items_respects_per_market_flag(_mock_secret, _mock_sources):
    """NG (enabled) fetches; KE (disabled) does NOT fetch even with handles configured."""
    # NG path stubs.
    responses_lib.add(
        responses_lib.GET,
        CHANNELS_URL,
        json=_channels_response("UU_davido"),
        status=200,
    )
    responses_lib.add(
        responses_lib.GET,
        PLAYLIST_ITEMS_URL,
        json=_playlist_items_response([{"vid": "vidD", "channel_title": "Davido"}]),
        status=200,
    )

    ng_connector = _make_connector("ng")
    ng_df = ng_connector.fetch()
    ke_connector = _make_connector("ke")
    # KE branch with disabled flag + zero queries hits the early-return
    # warning log; the module logger triggers the known Windows + Py3.13
    # logging segfault. Patch the logger to a MagicMock to skip the
    # native log path (same approach as other connector tests).
    ke_connector.logger = MagicMock()
    ke_df = ke_connector.fetch()

    assert len(ng_df) == 1
    assert ng_df.iloc[0]["author_handle"] == "@DavidoOfficial"
    # KE flag off -> empty DataFrame, no channels.list or playlistItems calls
    # beyond the NG ones already registered.
    assert len(ke_df) == 0
