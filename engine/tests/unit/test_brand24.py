"""Unit tests for src/ingestion/connectors/brand24.py."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from src.ingestion.connectors.base import RAW_COLUMNS
from src.ingestion.connectors.brand24 import (
    AI_INSIGHTS_LOOKBACK_DAYS,
    BRAND24_BASE_URL,
    DAILY_METRICS_LOOKBACK_DAYS,
    MAX_MENTIONS_PER_PROJECT_PER_RUN,
    MENTION_REACH_LOOKBACK_DAYS,
    MENTION_SENTIMENT_LOOKBACK_DAYS,
    MENTIONS_PAGE_SIZE,
    Brand24Connector,
    _ai_insight_to_row,
    _daily_metric_to_row,
    _mention_has_body,
    _mention_reach_to_row,
    _mention_sentiment_to_row,
    _mention_to_row,
    _mentions_page_ceiling,
    _platform_from_url,
    _sentiment_breakdown_to_tone,
    _vendor_sentiment_to_tone,
)


@pytest.fixture(autouse=True)
def _brand24_enabled_in_config(monkeypatch):
    """Pin the retirement gate open for the suite.

    configs/sources.yaml retired Brand24 (enabled: false, #305), so every
    fetch() here would short-circuit at the gate and test nothing. The gate
    itself is covered below, where load_sources is re-patched per test.
    """
    monkeypatch.setattr(
        "src.ingestion.connectors.brand24.load_sources",
        lambda: {"brand24": {"enabled": True}},
    )


def _mock_session(resp_map: dict[str, dict]) -> MagicMock:
    """Build a fake requests.Session whose .request() returns canned JSON
    keyed by path substring. Matches the first key substring found in the
    URL, first-match-wins. Unmatched URLs return an empty-success payload
    so tests do not have to enumerate every endpoint the connector calls."""

    def handler(method, url, **kwargs):
        for fragment, payload in resp_map.items():
            if fragment in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.content = b'{"status":"success"}'
                resp.json.return_value = {"status": "success", "message": payload}
                resp.raise_for_status = lambda: None
                return resp
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b'{"status":"success"}'
        resp.json.return_value = {"status": "success", "message": {}}
        resp.raise_for_status = lambda: None
        return resp

    session = MagicMock()
    session.request.side_effect = handler
    return session


def test_sentiment_breakdown_to_tone_weights():
    assert _sentiment_breakdown_to_tone({"positive": 1, "neutral": 0, "negative": 0}) == 1.0
    assert _sentiment_breakdown_to_tone({"positive": 0, "neutral": 1, "negative": 0}) == 0.5
    assert _sentiment_breakdown_to_tone({"positive": 0, "neutral": 0, "negative": 1}) == 0.0
    # Mixed: 0.5 positive, 0.5 neutral => (0.5 + 0.25) / 1.0 = 0.75
    assert _sentiment_breakdown_to_tone({"positive": 0.5, "neutral": 0.5, "negative": 0}) == 0.75


def test_sentiment_breakdown_handles_null_and_empty():
    assert _sentiment_breakdown_to_tone(None) is None
    assert _sentiment_breakdown_to_tone({}) is None
    assert _sentiment_breakdown_to_tone({"positive": 0, "neutral": 0, "negative": 0}) is None


def test_fetch_returns_empty_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("BRAND24_API_KEY", raising=False)
    df = Brand24Connector(market="za").fetch(projects=[{"id": "123"}])
    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)


def test_fetch_returns_empty_when_no_projects_configured(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    df = Brand24Connector(market="za").fetch(projects=[])
    assert df.empty


def test_fetch_topics_and_hashtags_produce_raw_rows(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _mock_session(
        {
            "/topics": {
                "topics": [
                    {
                        "topic_id": 1,
                        "topic_name": "Load shedding",
                        "description": "Electricity cuts across SA.",
                        "mentions": 120,
                        "reach": 5_000_000,
                        "sentiment": {"positive": 0.1, "neutral": 0.3, "negative": 0.6},
                    }
                ]
            },
            "/trending-hashtags": {
                "hashtags": [
                    {
                        "hashtag": "#southafrica",
                        "mentions_count": 3704,
                        "social_media_reach": 30_662_894,
                        "sentiment_score": None,
                    }
                ]
            },
        }
    )
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "9999", "name": "SA Trends", "query_group": "brand24_sa"}]
        )

    assert len(df) == 2
    assert list(df.columns) == list(RAW_COLUMNS)
    topic_row = df[df["content_type"] == "topic"].iloc[0]
    assert topic_row["title"] == "Load shedding"
    assert topic_row["market"] == "za"
    assert topic_row["query_group"] == "brand24_topics"
    # Sentiment mapping: (0.1*1 + 0.3*0.5 + 0.6*0) / 1.0 = 0.25 -> synth V2Tone
    assert topic_row["v2tone"].startswith("-50.00,")

    tag_row = df[df["content_type"] == "trending_hashtag"].iloc[0]
    assert tag_row["query_term"] == "#southafrica"
    assert tag_row["views"] == 30_662_894
    assert tag_row["likes"] == 3704


def test_fetch_http_error_is_swallowed(monkeypatch):
    import requests as req

    monkeypatch.setenv("BRAND24_API_KEY", "tok")

    session = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 500
    fake_resp.content = b"oops"
    err = req.exceptions.HTTPError()
    err.response = fake_resp
    fake_resp.raise_for_status.side_effect = err
    session.request.return_value = fake_resp

    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(projects=[{"id": "1"}])
    assert isinstance(df, pd.DataFrame)
    assert df.empty


def test_fetch_skips_project_without_id(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _mock_session({})  # no matches, should not be called
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="ng").fetch(projects=[{"name": "oops"}])
    assert df.empty
    session.request.assert_not_called()


def test_fetch_loads_projects_from_sources_yaml(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    fake_sources = {
        "brand24": {
            "za": {"projects": [{"id": "123", "name": "SA"}]},
        }
    }
    session = _mock_session(
        {
            "/topics": {"topics": []},
            "/trending-hashtags": {"hashtags": []},
        }
    )
    with (
        patch("src.ingestion.connectors.brand24.load_sources", return_value=fake_sources),
        patch.object(Brand24Connector, "_build_session", return_value=session),
    ):
        df = Brand24Connector(market="za").fetch()  # projects=None -> config load
    # Both endpoints empty -> empty DataFrame but both were called.
    assert df.empty
    urls = [call.args[1] for call in session.request.call_args_list]
    assert any("/topics" in u for u in urls)
    assert any("/trending-hashtags" in u for u in urls)
    assert any("123" in u for u in urls)
    assert all(u.startswith(BRAND24_BASE_URL) for u in urls)


def test_non_success_envelope_returns_empty(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b'{"status":"fail"}'
    resp.json.return_value = {"status": "fail", "message": "bad key"}
    resp.raise_for_status = lambda: None
    session.request.return_value = resp
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(projects=[{"id": "1"}])
    assert df.empty


def test_include_flags_disable_endpoints(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _mock_session(
        {
            "/topics": {"topics": [{"topic_name": "T", "mentions": 1, "reach": 1}]},
            "/trending-hashtags": {
                "hashtags": [{"hashtag": "#t", "mentions_count": 1, "social_media_reach": 1}]
            },
        }
    )
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1", "name": "SA"}],
            include_topics=True,
            include_hashtags=False,
        )
    assert len(df) == 1
    assert (df["content_type"] == "topic").all()


def test_sends_x_api_key_header_not_bearer(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "secret-key")
    session = _mock_session(
        {
            "/topics": {"topics": []},
            "/trending-hashtags": {"hashtags": []},
            "/most-followers": {"authors": []},
            "/trending-links": {"trending_links": []},
        }
    )
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(projects=[{"id": "1"}])
    # Inspect the headers of every call.
    for call in session.request.call_args_list:
        headers = call.kwargs.get("headers", {})
        assert headers.get("X-Api-Key") == "secret-key"
        assert "Authorization" not in headers


def test_platform_from_url_mapping():
    assert _platform_from_url("https://www.facebook.com/some/page") == "facebook"
    assert _platform_from_url("https://fb.com/x") == "facebook"
    assert _platform_from_url("https://www.tiktok.com/@user/video/123") == "tiktok"
    assert _platform_from_url("https://www.instagram.com/p/abc") == "instagram"
    assert _platform_from_url("https://x.com/user") == "twitter"
    assert _platform_from_url("https://twitter.com/user") == "twitter"
    assert _platform_from_url("https://www.youtube.com/watch?v=abc") == "youtube"
    assert _platform_from_url("https://youtu.be/abc") == "youtube"
    assert _platform_from_url("https://someblog.co.za/post") == "web"
    assert _platform_from_url("") == "web"


def test_fetch_most_followers_shapes_author_rows(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _mock_session(
        {
            "/topics": {"topics": []},
            "/trending-hashtags": {"hashtags": []},
            "/most-followers": {
                "authors": [
                    {
                        "name": "Trevor Noah",
                        "url": "http://www.facebook.com/65639912452",
                        "followers_count": 12558080,
                        "mentions_count": 1,
                        "reach": 457600,
                    },
                    {
                        "name": "timesofindia",
                        "url": "http://twitter.com/timesofindia",
                        "followers_count": 13958162,
                        "mentions_count": 1,
                        "reach": 279163,
                    },
                ]
            },
            "/trending-links": {"trending_links": []},
        }
    )
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(projects=[{"id": "1", "name": "SA Trends"}])
    author_rows = df[df["content_type"] == "top_author"]
    assert len(author_rows) == 2
    tn = author_rows[author_rows["author_name"] == "Trevor Noah"].iloc[0]
    assert tn["platform"] == "facebook"
    assert tn["views"] == 457600
    assert tn["author_handle"] == "http://www.facebook.com/65639912452"


def test_fetch_trending_links_infers_platform_from_url(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _mock_session(
        {
            "/topics": {"topics": []},
            "/trending-hashtags": {"hashtags": []},
            "/most-followers": {"authors": []},
            "/trending-links": {
                "trending_links": [
                    {"url": "https://www.facebook.com/profile.php?id=1", "mentions_count": 7},
                    {"url": "https://youtu.be/xyz", "mentions_count": 3},
                    {"url": "https://example.co.za/article", "mentions_count": 1},
                ]
            },
        }
    )
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(projects=[{"id": "1", "name": "SA Trends"}])
    links = df[df["content_type"] == "trending_link"]
    assert len(links) == 3
    platforms = sorted(links["platform"].tolist())
    assert platforms == ["facebook", "web", "youtube"]
    fb = links[links["platform"] == "facebook"].iloc[0]
    assert fb["likes"] == 7
    assert fb["url"].startswith("https://www.facebook.com/")


def test_include_flags_can_disable_followers_and_links(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _mock_session(
        {
            "/topics": {"topics": [{"topic_name": "T", "mentions": 1, "reach": 1}]},
            "/trending-hashtags": {
                "hashtags": [{"hashtag": "#t", "mentions_count": 1, "social_media_reach": 1}]
            },
            "/most-followers": {
                "authors": [
                    {
                        "name": "X",
                        "url": "http://fb.com/x",
                        "followers_count": 1,
                        "mentions_count": 1,
                        "reach": 1,
                    }
                ]
            },
            "/trending-links": {
                "trending_links": [{"url": "https://example.com", "mentions_count": 1}]
            },
        }
    )
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1", "name": "SA"}],
            include_topics=True,
            include_hashtags=True,
            include_followers=False,
            include_links=False,
        )
    types = set(df["content_type"].tolist())
    assert types == {"topic", "trending_hashtag"}


def test_get_retries_once_on_non_json_then_succeeds(monkeypatch):
    """First non-JSON response retries; second JSON response succeeds.

    Regression guard: KE Brand24 trending-hashtags returned non-JSON on
    2026-04-27 cloud cron (zero rows pulled) while the other 3 KE
    Brand24 endpoints succeeded. Hardened _get retries once with a short
    backoff to absorb transient hiccups.
    """
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    monkeypatch.setattr("src.ingestion.connectors.brand24.time.sleep", lambda _s: None)

    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.content = b"<html>upstream error</html>"
    bad_resp.text = "<html>upstream error</html>"
    bad_resp.headers = {"Content-Type": "text/html"}
    bad_resp.raise_for_status.return_value = None
    bad_resp.json.side_effect = ValueError("not JSON")

    good_resp = MagicMock()
    good_resp.status_code = 200
    good_resp.content = b'{"status":"success","data":{"hashtags":[]}}'
    good_resp.headers = {"Content-Type": "application/json"}
    good_resp.raise_for_status.return_value = None
    good_resp.json.return_value = {"status": "success", "data": {"hashtags": []}}

    session = MagicMock()
    session.request.side_effect = [bad_resp, good_resp]

    with patch.object(Brand24Connector, "_build_session", return_value=session):
        connector = Brand24Connector(market="ke")
        result = connector._get("/api-data/v1/project/1/trending-hashtags", token="tok")

    assert session.request.call_count == 2
    assert result == {"hashtags": []}


def test_get_returns_empty_when_non_json_persists_after_retry(monkeypatch):
    """Two consecutive non-JSON responses return empty dict, no crash.

    Logs the content-type and a body excerpt on the second failure so
    the operator can distinguish a transient hiccup from an upstream
    contract change.
    """
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    monkeypatch.setattr("src.ingestion.connectors.brand24.time.sleep", lambda _s: None)

    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.content = b"<html>still bad</html>"
    bad_resp.text = "<html>still bad</html>"
    bad_resp.headers = {"Content-Type": "text/html"}
    bad_resp.raise_for_status.return_value = None
    bad_resp.json.side_effect = ValueError("not JSON")

    session = MagicMock()
    session.request.side_effect = [bad_resp, bad_resp]

    with patch.object(Brand24Connector, "_build_session", return_value=session):
        connector = Brand24Connector(market="ke")
        result = connector._get("/api-data/v1/project/1/trending-hashtags", token="tok")

    assert session.request.call_count == 2
    assert result == {}


def test_per_project_include_hashtags_false_skips_hashtag_fetch(monkeypatch):
    """Project entry with include_hashtags=false skips the hashtag pull
    while leaving every other endpoint enabled."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _mock_session(
        {
            "/topics": {"topics": [{"topic_name": "T", "mentions": 1, "reach": 1}]},
            "/trending-hashtags": {
                "hashtags": [{"hashtag": "#t", "mentions_count": 1, "social_media_reach": 1}]
            },
            "/most-followers": {
                "authors": [
                    {
                        "name": "X",
                        "url": "http://fb.com/x",
                        "followers_count": 1,
                        "mentions_count": 1,
                        "reach": 1,
                    }
                ]
            },
            "/trending-links": {
                "trending_links": [{"url": "https://example.com", "mentions_count": 1}]
            },
        }
    )
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="ke").fetch(
            projects=[
                {"id": "1397483539", "name": "Kenya trends", "include_hashtags": False},
            ],
        )
    types = set(df["content_type"].tolist())
    assert "trending_hashtag" not in types
    assert "topic" in types
    assert "top_author" in types
    assert "trending_link" in types
    # Verify no /trending-hashtags call was issued.
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any("/trending-hashtags" in p for p in paths)


# ----------------------------------------------------------------------
# mentions/list endpoint tests
# ----------------------------------------------------------------------


def _success_resp(payload: dict) -> MagicMock:
    """Build a 200 OK JSON success envelope mock."""
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b'{"status":"success"}'
    resp.headers = {"Content-Type": "application/json"}
    resp.json.return_value = {"status": "success", "data": payload}
    resp.raise_for_status = lambda: None
    return resp


def _mentions_envelope(mentions_payload: list[dict], has_more: bool = False) -> dict:
    """Vendor envelope verified by 28 May 2026 live probe against
    `/api-data/v1/project/{pid}/mentions`:
    `{status, message: {results, has_more_mentions, cursor}}`.
    """
    return {
        "status": "success",
        "message": {
            "results": mentions_payload,
            "has_more_mentions": has_more,
            "cursor": "next-cursor-token" if has_more else None,
        },
    }


def _mentions_resp(mentions_payload: list[dict], has_more: bool = False) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b'{"status":"success"}'
    resp.headers = {"Content-Type": "application/json"}
    resp.json.return_value = _mentions_envelope(mentions_payload, has_more=has_more)
    resp.raise_for_status = lambda: None
    return resp


def _mentions_session(
    mentions_payload: list[dict],
    other_payloads: dict[str, dict] | None = None,
    paginate: bool = False,
) -> MagicMock:
    """Session that returns the supplied mentions on `/mentions` (NOT
    `/mentions/list`; the `/list` suffix 404s per live vendor probe)
    and empty success payloads on every other endpoint.

    When `paginate=True`, every call returns has_more_mentions=true so
    the cap-honour test can verify the loop terminates by per-project cap.
    """
    other_payloads = other_payloads or {}

    def handler(method, url, **kwargs):
        if "/mentions" in url and "/mentions/list" not in url:
            return _mentions_resp(mentions_payload, has_more=paginate)
        for fragment, payload in other_payloads.items():
            if fragment in url:
                return _success_resp(payload)
        # Topics / hashtags / authors / links default to empty.
        return _success_resp({})

    session = MagicMock()
    session.request.side_effect = handler
    return session


def test_vendor_sentiment_to_tone_maps_signed_int():
    assert _vendor_sentiment_to_tone(1).startswith("100.00,")
    assert _vendor_sentiment_to_tone(0).startswith("0.00,")
    assert _vendor_sentiment_to_tone(-1).startswith("-100.00,")
    # Out of range or missing -> empty
    assert _vendor_sentiment_to_tone(None) == ""
    assert _vendor_sentiment_to_tone(2) == ""
    assert _vendor_sentiment_to_tone("garbage") == ""


def test_mention_to_row_normalises_real_vendor_fields():
    """Vendor contract confirmed by 28 May 2026 live probe.

    Real fields: date, time, title, content, source (URL or
    "Tweet-ID:" string or null), host, category, sentiment, tags.
    No author handle, no reach at this endpoint.
    """
    row = _mention_to_row(
        {
            "date": "2026-05-27",
            "time": "14:32",
            "title": "Big news",
            "content": "Crowds gathered in CBD after announcement.",
            "source": "https://www.facebook.com/post/1",
            "host": "facebook.com",
            "category": "facebook",
            "sentiment": 1,
            "tags": None,
        },
        market="za",
        project_name="South Africa trends",
    )
    assert row["source"] == "brand24"
    assert row["platform"] == "facebook"
    assert row["market"] == "za"
    assert row["content_type"] == "mention"
    assert row["query_group"] == "brand24_mentions"
    assert (
        row["query_term"] == "South Africa trends"
    )  # vendor has no keyword field on this endpoint
    assert row["author_handle"] == ""  # vendor doesn't return one
    assert row["title"] == "Big news"
    assert row["text"] == "Crowds gathered in CBD after announcement."
    assert row["url"] == "https://www.facebook.com/post/1"
    assert row["views"] == 0  # no reach at this endpoint
    assert row["v2tone"].startswith("100.00,")  # sentiment +1 -> +100 tone
    parsed = datetime.fromisoformat(row["published_at"])
    assert parsed.tzinfo is not None
    assert parsed.year == 2026
    assert parsed.month == 5
    assert parsed.day == 27


def test_mention_has_body_filters_empty_social_pings():
    """29 May 2026: empty social pings (null title + null content) carry no
    classifiable signal and are skipped at ingestion. Day 1 those were 97%
    of X rows, 99% of Instagram rows, all classifying at 0%."""
    # Empty ping: no title, no content -> skip
    assert _mention_has_body({"title": None, "content": None, "host": "twitter.com"}) is False
    assert _mention_has_body({"title": "", "content": "", "host": "instagram.com"}) is False
    assert _mention_has_body({"host": "facebook.com", "category": "facebook"}) is False
    # Real content -> keep
    assert _mention_has_body({"title": None, "content": "Crowds gathered in CBD."}) is True
    assert _mention_has_body({"title": "Big news", "content": None}) is True
    assert _mention_has_body({"title": "  ", "content": "real body here"}) is True


def test_mention_to_row_handles_null_title_and_content_via_host_fallback():
    """Social platform pings often return null title + null content +
    null source. _mention_to_row stays defensive (host fallback) even though
    the fetch loop now skips these before they reach it."""
    row = _mention_to_row(
        {
            "date": "2026-05-27",
            "time": "07:00",
            "title": None,
            "content": None,
            "source": None,
            "host": "instagram.com",
            "category": "instagram",
            "sentiment": 1,
        },
        market="za",
        project_name="SA",
    )
    assert row["platform"] == "instagram"
    assert row["title"] == "instagram.com mention"
    assert row["text"] == ""
    assert row["url"] == ""


def test_mention_to_row_synthesises_title_from_content_when_title_null():
    """Title-null + content-present rows get a content-derived title."""
    row = _mention_to_row(
        {
            "date": "2026-05-27",
            "time": "07:00",
            "title": None,
            "content": "A long-form blog post about amapiano this weekend in Joburg",
            "source": "https://blog.example.com/post",
            "host": "blog.example.com",
            "category": "blogs",
        },
        market="za",
        project_name="SA",
    )
    assert row["title"].startswith("A long-form blog post")
    assert row["platform"] == "web"  # blogs -> web per vendor mapping


def test_mention_to_row_falls_back_to_url_platform_for_unknown_category():
    row = _mention_to_row(
        {
            "date": "2026-05-27",
            "time": "07:00",
            "content": "",
            "source": "https://www.tiktok.com/@user/video/1",
            "host": "tiktok.com",
            "category": "some_new_label",
        },
        market="ng",
        project_name="Nigeria trends",
    )
    assert row["platform"] == "tiktok"  # category unknown, URL host wins


def test_mention_text_is_truncated_to_4000_chars():
    long_content = "x" * 5000
    row = _mention_to_row(
        {
            "date": "2026-05-27",
            "time": "07:00",
            "content": long_content,
            "source": "https://x.com/a/1",
            "host": "twitter.com",
            "category": "x",
        },
        market="za",
        project_name="SA",
    )
    assert len(row["text"]) == 4000


def _real_mention(category: str = "x", **overrides: Any) -> dict:
    """Build a vendor-shape mention with sensible defaults.

    Matches the verified envelope on `/api-data/v1/project/{pid}/mentions`.
    """
    base: dict[str, Any] = {
        "date": "2026-05-27",
        "time": "10:00",
        "title": None,
        "content": "stub",
        "source": "https://example.com/p/1",
        "host": f"{category}.com"
        if category not in {"x", "blogs"}
        else ("twitter.com" if category == "x" else "blog.example.com"),
        "category": category,
        "sentiment": 0,
        "tags": None,
    }
    base.update(overrides)
    return base


def test_fetch_mentions_happy_path_three_rows(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    mentions = [
        _real_mention(
            category="instagram",
            host="instagram.com",
            source="https://www.instagram.com/p/abc",
            content="Outfit of the day",
            sentiment=1,
        ),
        _real_mention(
            category="tiktok",
            host="tiktok.com",
            source="https://www.tiktok.com/@khaby/video/123",
            content="Skit",
            sentiment=0,
        ),
        _real_mention(
            category="x",
            host="twitter.com",
            source="https://x.com/cassper/status/1",
            content="Tour announcement",
            sentiment=-1,
        ),
    ]
    session = _mentions_session(mentions)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=True,
        )
    mention_rows = df[df["content_type"] == "mention"]
    assert len(mention_rows) == 3
    assert set(mention_rows["platform"].tolist()) == {"instagram", "tiktok", "twitter"}
    assert list(df.columns) == list(RAW_COLUMNS)


def test_include_mentions_false_short_circuits_call(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _mentions_session([_real_mention()])
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    # Neither the new URL nor the old (just in case the connector regresses)
    # should appear when include_mentions is False.
    assert not any(p.endswith("/mentions") or "/mentions/list" in p for p in paths)


def test_per_project_include_mentions_false_overrides_default(monkeypatch):
    """Per-project include_mentions: false skips the mentions pull while the
    connector-wide default stays True."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _mentions_session([_real_mention()])
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="ke").fetch(
            projects=[
                {"id": "1397483539", "name": "Kenya trends", "include_mentions": False},
            ],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            # include_mentions kwarg stays at default True.
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any(p.endswith("/mentions") or "/mentions/list" in p for p in paths)


def test_fetch_mentions_404_returns_empty(monkeypatch):
    """404 from /mentions (project deleted) returns empty without raising."""
    import requests as req

    monkeypatch.setenv("BRAND24_API_KEY", "tok")

    fake_resp = MagicMock()
    fake_resp.status_code = 404
    fake_resp.content = b"not found"
    err = req.exceptions.HTTPError()
    err.response = fake_resp
    fake_resp.raise_for_status.side_effect = err

    def handler(method, url, **kwargs):
        if "/mentions" in url:
            return fake_resp
        return _success_resp({})

    session = MagicMock()
    session.request.side_effect = handler

    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=True,
        )
    assert df.empty


def test_mentions_cap_honoured_when_vendor_returns_more(monkeypatch):
    """Per-project cap stops the loop even when the vendor reports more pages."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    # Vendor returns a full page with has_more_mentions=True every time.
    full_page = [_real_mention(content=f"row {i}") for i in range(MENTIONS_PAGE_SIZE)]
    session = _mentions_session(full_page, paginate=True)

    project_cap = 5  # well under one page size
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[
                {"id": "1", "name": "SA", "mentions_max_per_run": project_cap},
            ],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=True,
        )
    mention_rows = df[df["content_type"] == "mention"]
    assert len(mention_rows) == project_cap


def test_mentions_cap_default_capped_at_module_constant(monkeypatch):
    """When sources.yaml does not override the cap, the module default applies."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    full_page = [_real_mention(content=f"row {i}") for i in range(MENTIONS_PAGE_SIZE)]
    session = _mentions_session(full_page, paginate=True)

    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=True,
        )
    mention_rows = df[df["content_type"] == "mention"]
    assert len(mention_rows) == MAX_MENTIONS_PER_PROJECT_PER_RUN


def test_mentions_page_ceiling_scales_with_per_project_cap():
    """The page ceiling is derived from the per-project cap, not the module
    constant, so a cap above ~1000 gets more than the old fixed 10 pages."""
    assert _mentions_page_ceiling(0) == 0
    assert _mentions_page_ceiling(-5) == 0
    # cap 600 (live default): ceil(600/100)=6 naive pages, x6 + 2 = 38.
    assert _mentions_page_ceiling(600) == 38
    # A cap above 1000 must yield a ceiling tall enough to reach it:
    # ceil(1500/100)=15 naive pages need >=15 ceiling pages to be reachable.
    assert _mentions_page_ceiling(1500) >= 15
    # Hard cap bounds vendor calls no matter how high the per-project cap.
    assert _mentions_page_ceiling(1_000_000) == 50


def test_mentions_cap_above_1000_is_reachable(monkeypatch):
    """Regression for the old module-derived page limit (fixed at 10 pages =
    1000 mentions) that made any per-project cap above 1000 unreachable. Full
    non-empty pages with a 1500 cap must now yield 1500 kept rows."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    full_page = [_real_mention(content=f"row {i}") for i in range(MENTIONS_PAGE_SIZE)]
    session = _mentions_session(full_page, paginate=True)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1", "name": "SA", "mentions_max_per_run": 1500}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=True,
        )
    mention_rows = df[df["content_type"] == "mention"]
    assert len(mention_rows) == 1500


def _mixed_ping_session(non_empty_per_page: int, empty_per_page: int) -> MagicMock:
    """Session whose every /mentions page carries ``non_empty_per_page`` rows
    with content plus ``empty_per_page`` empty social pings (null title +
    null content), always reporting has_more so only the page ceiling or the
    kept-rows cap can end the loop."""

    def build_page() -> list[dict]:
        page: list[dict] = [_real_mention(content=f"kept {i}") for i in range(non_empty_per_page)]
        page += [
            _real_mention(title=None, content=None, source=None, host="twitter.com")
            for _ in range(empty_per_page)
        ]
        return page

    def handler(method, url, **kwargs):
        if "/mentions" in url and "/mentions/list" not in url:
            return _mentions_resp(build_page(), has_more=True)
        return _success_resp({})

    session = MagicMock()
    session.request.side_effect = handler
    return session


def test_mentions_empty_pings_do_not_bind_loop_before_cap(monkeypatch):
    """75% empty pages (25 kept + 75 empty per page). With a 300 cap the loop
    needs 12 pages; the old fixed 10-page limit would have stopped at 250 kept
    and silently dropped non-empty mentions. The cap-derived ceiling (20 pages
    for a 300 cap) lets the cap be reached."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _mixed_ping_session(non_empty_per_page=25, empty_per_page=75)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1", "name": "SA", "mentions_max_per_run": 300}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=True,
        )
    mention_rows = df[df["content_type"] == "mention"]
    assert len(mention_rows) == 300


def test_mentions_logs_warning_when_page_ceiling_ends_loop(monkeypatch):
    """When the page ceiling (not the cap, not vendor exhaustion) stops paging,
    the connector logs a warning so the operator can raise the cap rather than
    silently dropping non-empty mentions. 95% empty pages with a 300 cap fill
    only ~100 kept rows across the 20-page ceiling while the vendor keeps
    reporting has_more, so the ceiling binds."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _mixed_ping_session(non_empty_per_page=5, empty_per_page=95)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        connector = Brand24Connector(market="za")
        connector.logger = MagicMock()
        df = connector.fetch(
            projects=[{"id": "1", "name": "SA", "mentions_max_per_run": 300}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=True,
        )
    # Cap not reached because the ceiling bound first.
    assert len(df[df["content_type"] == "mention"]) < 300
    warning_messages = [c.args[0] for c in connector.logger.warning.call_args_list]
    assert any("hit page ceiling" in m for m in warning_messages)


def test_mentions_no_page_ceiling_warning_when_cap_reached(monkeypatch):
    """The page-ceiling warning must NOT fire when the per-project cap ends the
    loop normally on a clean non-empty stream."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    full_page = [_real_mention(content=f"row {i}") for i in range(MENTIONS_PAGE_SIZE)]
    session = _mentions_session(full_page, paginate=True)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        connector = Brand24Connector(market="za")
        connector.logger = MagicMock()
        connector.fetch(
            projects=[{"id": "1", "name": "SA", "mentions_max_per_run": 50}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=True,
        )
    warning_messages = [c.args[0] for c in connector.logger.warning.call_args_list]
    assert not any("hit page ceiling" in m for m in warning_messages)


def test_mentions_cursor_pagination_threads_through_loop(monkeypatch):
    """has_more_mentions=true + cursor causes a second call carrying the cursor."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    full_page = [_real_mention(content=f"row {i}") for i in range(MENTIONS_PAGE_SIZE)]
    session = _mentions_session(full_page, paginate=True)

    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1", "name": "SA", "mentions_max_per_run": 250}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=True,
        )

    mention_calls = [
        c
        for c in session.request.call_args_list
        if "/mentions" in c.args[1] and "/mentions/list" not in c.args[1]
    ]
    assert len(mention_calls) >= 2
    # Second call carries the cursor returned by the first envelope.
    second_params = mention_calls[1].kwargs.get("params", {})
    assert second_params.get("cursor") == "next-cursor-token"


def test_mentions_date_window_is_t_minus_1_to_today(monkeypatch):
    """Vendor params bound date_from to yesterday and date_to to today."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _mentions_session([])

    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=True,
        )

    expected_from = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    expected_to = datetime.now(UTC).date().isoformat()
    mentions_calls = [
        c
        for c in session.request.call_args_list
        if "/mentions" in c.args[1] and "/mentions/list" not in c.args[1]
    ]
    assert mentions_calls, "expected at least one /mentions call"
    params = mentions_calls[0].kwargs.get("params", {})
    assert params["date_from"] == expected_from
    assert params["date_to"] == expected_to
    # Vendor /mentions endpoint does not honour `order` or `order_direction`
    # params per live probe; the envelope returns date-DESC by default.
    assert "order" not in params or params.get("order") in (None, "")


def test_mentions_sentiment_mapping_minus1_zero_plus1(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    mentions = [
        _real_mention(category="x", content="love", sentiment=1),
        _real_mention(category="x", content="ok", sentiment=0),
        _real_mention(category="x", content="bad", sentiment=-1),
    ]
    session = _mentions_session(mentions)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=True,
        )
    rows = df[df["content_type"] == "mention"]
    tones = sorted(rows["v2tone"].tolist())
    # Sorted strings: -100.00,..., 0.00,..., 100.00,...
    assert tones[0].startswith("-100.00")
    assert tones[1].startswith("0.00")
    assert tones[2].startswith("100.00")


def test_mentions_platform_mapping_for_major_vendor_labels(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    mentions = [
        _real_mention(
            category="facebook", host="facebook.com", source="https://www.facebook.com/post/1"
        ),
        _real_mention(
            category="instagram", host="instagram.com", source="https://www.instagram.com/p/2"
        ),
        _real_mention(
            category="tiktok", host="tiktok.com", source="https://www.tiktok.com/@u/video/3"
        ),
        _real_mention(category="x", host="twitter.com", source="https://x.com/u/status/4"),
    ]
    session = _mentions_session(mentions)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=True,
        )
    rows = df[df["content_type"] == "mention"]
    platforms = sorted(rows["platform"].tolist())
    assert platforms == ["facebook", "instagram", "tiktok", "twitter"]


# ----------------------------------------------------------------------
# ai-insights endpoint tests. Vendor shape verified by 28 May 2026 live
# probe against `/api-data/v1/project/{pid}/ai-insights`. Only the top-
# level path returns 200; sub-paths (topics / competitors / sentiment-
# trajectory / themes / summary / clusters) all 404 on Business tier.
# Response: `{status: success, message: {project_id, date_from, date_to,
# headline, trends, insights, recommendations}}`. Four narrative string
# fields per project per call, normalised into four RAW rows.
# ----------------------------------------------------------------------


def _ai_insights_envelope(
    *,
    headline: str = "Stub headline",
    trends: str = "- Stub trend bullet",
    insights: str = "- Stub insight bullet",
    recommendations: str = "- Stub recommendation",
    date_from: str = "2026-05-21",
    date_to: str = "2026-05-28",
    project_id: int = 1397483532,
    envelope: str = "message",
) -> dict:
    """Build a vendor-shape ai-insights response envelope.

    `envelope` toggles between `message` (verified shape) and `data`
    (legacy / defensive shape) so the both-shapes test can flip it.
    """
    body = {
        "project_id": project_id,
        "date_from": date_from,
        "date_to": date_to,
        "headline": headline,
        "trends": trends,
        "insights": insights,
        "recommendations": recommendations,
    }
    return {"status": "success", envelope: body}


def _ai_insights_resp(envelope_payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b'{"status":"success"}'
    resp.headers = {"Content-Type": "application/json"}
    resp.json.return_value = envelope_payload
    resp.raise_for_status = lambda: None
    return resp


def _ai_insights_session(
    envelope_payload: dict | None = None,
    status_code: int = 200,
) -> MagicMock:
    """Session that returns the supplied ai-insights envelope on the
    `/ai-insights` path and empty success on every other endpoint."""
    import requests as req

    def handler(method, url, **kwargs):
        if "/ai-insights" in url:
            if status_code == 404:
                fake = MagicMock()
                fake.status_code = 404
                fake.content = b"not found"
                err = req.exceptions.HTTPError()
                err.response = fake
                fake.raise_for_status.side_effect = err
                return fake
            return _ai_insights_resp(envelope_payload or _ai_insights_envelope())
        return _success_resp({})

    session = MagicMock()
    session.request.side_effect = handler
    return session


def test_ai_insight_to_row_shape_matches_raw_columns():
    """One narrative field maps to one RAW row with the documented shape."""
    row = _ai_insight_to_row(
        field="headline",
        text="South African Digital Conversations Show Stable Volume",
        market="za",
        project_name="South Africa trends",
        project_id="1397483532",
        date_from="2026-05-21",
        date_to="2026-05-28",
    )
    assert row["source"] == "brand24"
    # platform="aggregate" so the row skips the trend-scoring loop like the
    # sibling Wave-2 aggregate surfaces; content_type stays brand24_ai_insight
    # so the brief generator still reads it.
    assert row["platform"] == "aggregate"
    assert row["market"] == "za"
    assert row["content_type"] == "brand24_ai_insight"
    assert row["query_group"] == "brand24_ai_insights"
    assert row["query_term"] == "1397483532:headline"
    assert "South Africa trends Headline" in row["title"]
    assert "(2026-05-21 to 2026-05-28)" in row["title"]
    assert row["text"].startswith("South African Digital Conversations")
    assert row["url"] == ""
    assert row["views"] == 0
    assert row["v2tone"] == ""


def test_ai_insight_text_is_truncated_to_4000_chars():
    long_body = "x" * 5000
    row = _ai_insight_to_row(
        field="trends",
        text=long_body,
        market="ng",
        project_name="Nigeria trends",
        project_id="1397483537",
        date_from="2026-05-21",
        date_to="2026-05-28",
    )
    assert len(row["text"]) == 4000


def test_ai_insights_disabled_skips_call(monkeypatch):
    """include_ai_insights=False short-circuits the vendor call entirely."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _ai_insights_session()
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_ai_insights=False,
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any("/ai-insights" in p for p in paths)


def test_ai_insights_default_kwarg_is_false_dark_on_ship(monkeypatch):
    """Connector-wide default is False so flag flip is a follow-up commit."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _ai_insights_session()
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            # include_ai_insights omitted; default must be False.
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any("/ai-insights" in p for p in paths)


def test_ai_insights_per_project_disable_overrides_default(monkeypatch):
    """Per-project include_ai_insights: false skips the call while the
    connector-wide kwarg stays True."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _ai_insights_session()
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="ke").fetch(
            projects=[
                {
                    "id": "1397483539",
                    "name": "Kenya trends",
                    "include_ai_insights": False,
                },
            ],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_ai_insights=True,
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any("/ai-insights" in p for p in paths)


def test_ai_insights_auto_resolves_from_per_project_flag(monkeypatch):
    """Regression: the kwarg is omitted (as the orchestrator calls safe_fetch)
    but a project sets include_ai_insights=true, so the call MUST fire. Mirrors
    the Wave-2 sentiment/reach/daily_metrics auto-resolve; ai_insights was the
    one Wave-2 sibling left without it, so a YAML-only flip silently no-opped."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _ai_insights_session()
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA", "include_ai_insights": True}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            # include_ai_insights kwarg omitted, exactly like safe_fetch().
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert any("/ai-insights" in p for p in paths)


def test_ai_insights_happy_path_produces_four_rows(monkeypatch):
    """All four narrative fields populated -> four RAW rows."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    envelope = _ai_insights_envelope(
        headline="SA Amapiano Surge",
        trends="- Mentions stable at 54.5k",
        insights="- Amapiano dominance",
        recommendations="- Weekend engagement strategy",
    )
    session = _ai_insights_session(envelope)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "South Africa trends"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_ai_insights=True,
        )
    rows = df[df["content_type"] == "brand24_ai_insight"]
    assert len(rows) == 4
    fields = sorted(r.split(":", 1)[1] for r in rows["query_term"].tolist() if ":" in r)
    assert fields == ["headline", "insights", "recommendations", "trends"]
    # platform="aggregate" so these rows skip the trend-scoring loop; the
    # content_type filter above confirms the rows still carry their type.
    assert set(rows["platform"].tolist()) == {"aggregate"}
    assert list(df.columns) == list(RAW_COLUMNS)


def test_ai_insights_skips_empty_string_fields(monkeypatch):
    """Empty narrative fields are dropped so no hollow rows land."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    envelope = _ai_insights_envelope(
        headline="Real headline",
        trends="",
        insights="   ",  # whitespace only
        recommendations="Real recs",
    )
    session = _ai_insights_session(envelope)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="ng").fetch(
            projects=[{"id": "1397483537", "name": "Nigeria trends"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_ai_insights=True,
        )
    rows = df[df["content_type"] == "brand24_ai_insight"]
    assert len(rows) == 2
    fields = sorted(r.split(":", 1)[1] for r in rows["query_term"].tolist())
    assert fields == ["headline", "recommendations"]


def test_ai_insights_404_returns_empty(monkeypatch):
    """404 from /ai-insights (e.g. tier downgrade) returns empty cleanly."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _ai_insights_session(status_code=404)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_ai_insights=True,
        )
    assert df.empty


def test_ai_insights_envelope_handles_data_shape_defensively(monkeypatch):
    """Vendor verified shape is `message.X` but the connector also accepts
    `data.X` so a mid-flight contract change does not silently empty out
    the connector."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    envelope = _ai_insights_envelope(
        headline="From data envelope",
        trends="- t",
        insights="- i",
        recommendations="- r",
        envelope="data",
    )
    session = _ai_insights_session(envelope)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_ai_insights=True,
        )
    rows = df[df["content_type"] == "brand24_ai_insight"]
    assert len(rows) == 4
    headline_row = rows[rows["query_term"].str.endswith(":headline")].iloc[0]
    assert headline_row["text"] == "From data envelope"


def test_ai_insights_one_call_per_project(monkeypatch):
    """Each project gets exactly one /ai-insights call per run (vendor
    quota discipline; the narrative field split happens client-side)."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _ai_insights_session()
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[
                {"id": "1397483532", "name": "SA-a"},
                {"id": "9999999999", "name": "SA-b"},
            ],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_ai_insights=True,
        )
    ai_paths = [
        call.args[1] for call in session.request.call_args_list if "/ai-insights" in call.args[1]
    ]
    assert len(ai_paths) == 2


def test_ai_insights_per_project_lookback_override(monkeypatch):
    """Per-project ai_insights_lookback_days flows into date_from param."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _ai_insights_session()
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[
                {
                    "id": "1397483532",
                    "name": "SA",
                    "ai_insights_lookback_days": 30,
                },
            ],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_ai_insights=True,
        )
    ai_calls = [call for call in session.request.call_args_list if "/ai-insights" in call.args[1]]
    assert len(ai_calls) == 1
    params = ai_calls[0].kwargs.get("params") or {}
    expected = (datetime.now(UTC).date() - timedelta(days=30)).isoformat()
    assert params.get("date_from") == expected


def test_ai_insights_default_lookback_constant_is_used(monkeypatch):
    """Absent override, the module-level AI_INSIGHTS_LOOKBACK_DAYS drives
    the date_from window."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _ai_insights_session()
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_ai_insights=True,
        )
    ai_calls = [call for call in session.request.call_args_list if "/ai-insights" in call.args[1]]
    assert len(ai_calls) == 1
    params = ai_calls[0].kwargs.get("params") or {}
    expected = (datetime.now(UTC).date() - timedelta(days=AI_INSIGHTS_LOOKBACK_DAYS)).isoformat()
    assert params.get("date_from") == expected


# ----------------------------------------------------------------------
# Wave 2 endpoint tests (28 May 2026): mentions/sentiment + mentions/reach
# + daily-metrics. Live probe confirmed all three return 200 on Business
# tier across all three SSA projects and are per-day project aggregates
# (NOT per-mention rows). One row per (project, date). All three default
# off on ship; flip via sources.yaml after Day 1 live validation.
# ----------------------------------------------------------------------


def _mention_sentiment_envelope(
    *,
    dates: tuple[str, ...] = ("2026-05-27", "2026-05-28"),
    mentions: tuple[int, ...] = (14246, 5842),
    positives: tuple[int, ...] = (3500, 1500),
    negatives: tuple[int, ...] = (1200, 400),
    envelope: str = "message",
) -> dict:
    body = {
        "mentions": dict(zip(dates, mentions, strict=False)),
        "total_mentions": sum(mentions),
        "positive_mentions": dict(zip(dates, positives, strict=False)),
        "total_positive_mentions": sum(positives),
        "negative_mentions": dict(zip(dates, negatives, strict=False)),
        "total_negative_mentions": sum(negatives),
    }
    return {"status": "success", envelope: body}


def _mention_reach_envelope(
    *,
    dates: tuple[str, ...] = ("2026-05-27", "2026-05-28"),
    social: tuple[int, ...] = (98_511_943, 12_131_363),
    non_social: tuple[int, ...] = (4_000_000, 800_000),
    envelope: str = "message",
) -> dict:
    body = {
        "social_media_reach": dict(zip(dates, social, strict=False)),
        "social_media_reach_total": sum(social),
        "non_social_media_reach": dict(zip(dates, non_social, strict=False)),
        "non_social_media_reach_total": sum(non_social),
    }
    return {"status": "success", envelope: body}


def _daily_metrics_envelope(
    *,
    days: list[dict] | None = None,
    envelope: str = "message",
) -> dict:
    # Dates RELATIVE to today so both stub days always fall inside the
    # connector's 7-day daily-metrics lookback (DAILY_METRICS_LOOKBACK_DAYS).
    # Hardcoded 2026-05-27/28 dates aged out of the window once real time
    # passed ~4 Jun 2026 and broke the happy-path row-survival assertion.
    _today = datetime.now(UTC).date()
    _d2 = (_today - timedelta(days=2)).isoformat()
    _d1 = (_today - timedelta(days=1)).isoformat()
    if days is None:
        days = [
            {
                "date": _d2,
                "mentions_count": 9102,
                "reach_total": 80_272_867,
                "sentiment": {"positive": 0.25, "neutral": 0.61, "negative": 0.14},
                "engagement": {"likes": 2_834_350, "comments": 40_211, "shares": 99_395},
                "by_source": [],
            },
            {
                "date": _d1,
                "mentions_count": 4500,
                "reach_total": 20_000_000,
                "sentiment": {"positive": 0.30, "neutral": 0.55, "negative": 0.15},
                "engagement": {"likes": 800_000, "comments": 12_000, "shares": 30_000},
                "by_source": [],
            },
        ]
    body = {
        "project_id": 1397483532,
        "from": (_today - timedelta(days=7)).isoformat(),
        "to": _d1,
        "days": days,
    }
    return {"status": "success", envelope: body}


def _wave2_resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b'{"status":"success"}'
    resp.headers = {"Content-Type": "application/json"}
    resp.json.return_value = payload
    resp.raise_for_status = lambda: None
    return resp


def _wave2_session(
    endpoint_fragment: str,
    envelope_payload: dict | None,
    status_code: int = 200,
) -> MagicMock:
    """Session that returns the supplied envelope on the matching Wave 2
    sub-path and empty success on everything else."""
    import requests as req

    def handler(method, url, **kwargs):
        if endpoint_fragment in url:
            if status_code == 404:
                fake = MagicMock()
                fake.status_code = 404
                fake.content = b"not found"
                err = req.exceptions.HTTPError()
                err.response = fake
                fake.raise_for_status.side_effect = err
                return fake
            return _wave2_resp(envelope_payload or {})
        return _success_resp({})

    session = MagicMock()
    session.request.side_effect = handler
    return session


# ---------- _mention_sentiment_to_row pure shape tests ----------


def test_mention_sentiment_to_row_shape_matches_raw_columns():
    row = _mention_sentiment_to_row(
        sample_date="2026-05-27",
        total=14246,
        positive=3500,
        negative=1200,
        market="za",
        project_name="South Africa trends",
        project_id="1397483532",
    )
    assert row["source"] == "brand24"
    assert row["platform"] == "aggregate"
    assert row["content_type"] == "brand24_mention_sentiment"
    assert row["query_group"] == "brand24_mention_sentiment"
    assert row["query_term"] == "1397483532:2026-05-27"
    assert row["views"] == 14246
    assert row["likes"] == 3500
    assert row["comments"] == 1200
    # tone = (3500 - 1200) / 14246 * 100 ~= 16.15
    assert row["v2tone"].startswith("16.1")
    assert "14,246 mentions" in row["text"]
    assert row["published_at"].startswith("2026-05-27")


def test_mention_sentiment_to_row_handles_zero_total_safely():
    row = _mention_sentiment_to_row(
        sample_date="2026-05-27",
        total=0,
        positive=0,
        negative=0,
        market="za",
        project_name="SA",
        project_id="1397483532",
    )
    # tone falls to 0 not NaN
    assert row["v2tone"].startswith("0.00,")


# ---------- _mention_reach_to_row pure shape tests ----------


def test_mention_reach_to_row_shape_matches_raw_columns():
    row = _mention_reach_to_row(
        sample_date="2026-05-27",
        social_reach=98_511_943,
        non_social_reach=4_000_000,
        market="za",
        project_name="SA",
        project_id="1397483532",
    )
    assert row["content_type"] == "brand24_mention_reach"
    assert row["query_group"] == "brand24_mention_reach"
    assert row["views"] == 98_511_943
    assert row["likes"] == 4_000_000
    assert "social 98,511,943" in row["text"]
    assert row["published_at"].startswith("2026-05-27")


# ---------- _daily_metric_to_row pure shape tests ----------


def test_daily_metric_to_row_carries_engagement_and_tone():
    day = {
        "date": "2026-05-27",
        "mentions_count": 9102,
        "reach_total": 80_272_867,
        "sentiment": {"positive": 0.25, "neutral": 0.61, "negative": 0.14},
        "engagement": {"likes": 2_834_350, "comments": 40_211, "shares": 99_395},
        "by_source": [],
    }
    row = _daily_metric_to_row(day=day, market="za", project_name="SA", project_id="1397483532")
    assert row["content_type"] == "brand24_daily_metric"
    assert row["query_group"] == "brand24_daily_metrics"
    assert row["views"] == 80_272_867
    assert row["likes"] == 2_834_350
    assert row["comments"] == 40_211
    assert row["shares"] == 99_395
    # _sentiment_breakdown_to_tone -> 0.555; scaled to [-100,+100] = 11.0
    assert row["v2tone"].startswith("11.0")


def test_daily_metric_to_row_tolerates_missing_subdicts():
    day = {"date": "2026-05-27", "mentions_count": 100, "reach_total": 5000}
    row = _daily_metric_to_row(day=day, market="za", project_name="SA", project_id="1397483532")
    assert row["likes"] == 0
    assert row["comments"] == 0
    assert row["shares"] == 0
    assert row["v2tone"] == ""


# ---------- mentions/sentiment endpoint behaviour ----------


def test_mention_sentiment_disabled_skips_call(monkeypatch):
    """include_mentions_sentiment=False short-circuits the vendor call."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/mentions/sentiment", _mention_sentiment_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_mentions_sentiment=False,
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any("/mentions/sentiment" in p for p in paths)


def test_mention_sentiment_dark_when_no_project_opts_in(monkeypatch):
    """With the kwarg omitted AND no project setting the flag, the surface is
    dark. The top-level gate auto-resolves from the per-project flags, so a
    project without ``include_mentions_sentiment`` keeps the call dark."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/mentions/sentiment", _mention_sentiment_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            # include_mentions_sentiment omitted; no project opts in -> dark.
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any("/mentions/sentiment" in p for p in paths)


def test_mention_sentiment_auto_resolves_from_per_project_flag(monkeypatch):
    """Regression: the kwarg is omitted (as the orchestrator calls it) but a
    project sets include_mentions_sentiment=true, so the call MUST fire. The
    30 May bug was the top-level kwarg defaulting False and the per-project
    flags in sources.yaml never being reached, so the surface wrote 0 rows
    despite the 29 May flag flip."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/mentions/sentiment", _mention_sentiment_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA", "include_mentions_sentiment": True}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            # include_mentions_sentiment kwarg omitted, exactly like safe_fetch().
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert any("/mentions/sentiment" in p for p in paths)


def test_mention_sentiment_auto_resolve_does_not_leak_to_sibling_project(monkeypatch):
    """Mixed-project opt-in: with the kwarg omitted, one project setting
    include_mentions_sentiment=true MUST NOT enable the surface for a sibling
    project that never set the key. _resolve flips the top-level gate True off
    the opting-in project, but the per-project default is opt-in (False) when
    resolved from config, so project B stays dark while project A fires."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/mentions/sentiment", _mention_sentiment_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[
                {"id": "111", "name": "A", "include_mentions_sentiment": True},
                {"id": "222", "name": "B"},
            ],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            # include_mentions_sentiment kwarg omitted, exactly like safe_fetch().
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    # Project A opted in -> its sentiment endpoint is called.
    assert any("/project/111/mentions/sentiment" in p for p in paths)
    # Project B never set the key -> its sentiment endpoint is NOT called.
    assert not any("/project/222/mentions/sentiment" in p for p in paths)


def test_mention_sentiment_happy_path_normalises_correctly(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    envelope = _mention_sentiment_envelope(
        dates=("2026-05-27", "2026-05-28"),
        mentions=(14246, 5842),
        positives=(3500, 1500),
        negatives=(1200, 400),
    )
    session = _wave2_session("/mentions/sentiment", envelope)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_mentions_sentiment=True,
        )
    rows = df[df["content_type"] == "brand24_mention_sentiment"]
    assert len(rows) == 2
    assert sorted(rows["query_term"].tolist()) == [
        "1397483532:2026-05-27",
        "1397483532:2026-05-28",
    ]
    assert set(rows["platform"].tolist()) == {"aggregate"}
    assert list(df.columns) == list(RAW_COLUMNS)


def test_mention_sentiment_404_returns_empty(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/mentions/sentiment", None, status_code=404)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_mentions_sentiment=True,
        )
    assert df.empty


def test_mention_sentiment_envelope_handles_both_message_and_data_shapes(monkeypatch):
    """Vendor verified shape is `message.X` but the connector accepts
    `data.X` too so a mid-flight contract change does not empty rows."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    envelope = _mention_sentiment_envelope(envelope="data")
    session = _wave2_session("/mentions/sentiment", envelope)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_mentions_sentiment=True,
        )
    rows = df[df["content_type"] == "brand24_mention_sentiment"]
    assert len(rows) == 2


# ---------- mentions/reach endpoint behaviour ----------


def test_mention_reach_disabled_skips_call(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/mentions/reach", _mention_reach_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_mentions_reach=False,
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any("/mentions/reach" in p for p in paths)


def test_mention_reach_default_kwarg_is_false_dark_on_ship(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/mentions/reach", _mention_reach_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any("/mentions/reach" in p for p in paths)


def test_mention_reach_happy_path_normalises_correctly(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    envelope = _mention_reach_envelope()
    session = _wave2_session("/mentions/reach", envelope)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_mentions_reach=True,
        )
    rows = df[df["content_type"] == "brand24_mention_reach"]
    assert len(rows) == 2
    # Sort by date so the first row maps to 2026-05-27 -> social 98_511_943.
    sorted_rows = rows.sort_values("query_term").reset_index(drop=True)
    assert int(sorted_rows.iloc[0]["views"]) == 98_511_943
    assert int(sorted_rows.iloc[0]["likes"]) == 4_000_000


def test_mention_reach_404_returns_empty(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/mentions/reach", None, status_code=404)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_mentions_reach=True,
        )
    assert df.empty


def test_mention_reach_envelope_handles_both_message_and_data_shapes(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    envelope = _mention_reach_envelope(envelope="data")
    session = _wave2_session("/mentions/reach", envelope)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_mentions_reach=True,
        )
    rows = df[df["content_type"] == "brand24_mention_reach"]
    assert len(rows) == 2


# ---------- daily-metrics endpoint behaviour ----------


def test_daily_metrics_disabled_skips_call(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/daily-metrics", _daily_metrics_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_daily_metrics=False,
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any("/daily-metrics" in p for p in paths)


def test_daily_metrics_default_kwarg_is_false_dark_on_ship(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/daily-metrics", _daily_metrics_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any("/daily-metrics" in p for p in paths)


def test_daily_metrics_happy_path_normalises_correctly(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    envelope = _daily_metrics_envelope()
    session = _wave2_session("/daily-metrics", envelope)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_daily_metrics=True,
        )
    rows = df[df["content_type"] == "brand24_daily_metric"]
    # The fixture stub days are relative (today minus 1 and minus 2), so both
    # always fall inside the 7-day daily-metrics lookback and survive the trim.
    assert len(rows) == 2
    assert set(rows["platform"].tolist()) == {"aggregate"}
    assert list(df.columns) == list(RAW_COLUMNS)


def test_daily_metrics_404_returns_empty(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/daily-metrics", None, status_code=404)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_daily_metrics=True,
        )
    assert df.empty


def test_daily_metrics_envelope_handles_both_message_and_data_shapes(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    envelope = _daily_metrics_envelope(envelope="data")
    session = _wave2_session("/daily-metrics", envelope)
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        df = Brand24Connector(market="za").fetch(
            projects=[
                {
                    "id": "1397483532",
                    "name": "SA",
                    "daily_metrics_lookback_days": 365,
                }
            ],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_daily_metrics=True,
        )
    rows = df[df["content_type"] == "brand24_daily_metric"]
    assert len(rows) >= 1


def test_daily_metrics_only_one_call_per_project(monkeypatch):
    """One vendor call per project per run regardless of how many days
    come back (vendor returns ~31 days per call)."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/daily-metrics", _daily_metrics_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[
                {"id": "1397483532", "name": "SA-a"},
                {"id": "9999999999", "name": "SA-b"},
            ],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_daily_metrics=True,
        )
    daily_paths = [
        call.args[1] for call in session.request.call_args_list if "/daily-metrics" in call.args[1]
    ]
    assert len(daily_paths) == 2


# ---------- Per-project disable + lookback overrides ----------


def test_mention_sentiment_per_project_disable_overrides_default(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/mentions/sentiment", _mention_sentiment_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="ke").fetch(
            projects=[
                {
                    "id": "1397483539",
                    "name": "Kenya trends",
                    "include_mentions_sentiment": False,
                }
            ],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_mentions_sentiment=True,
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any("/mentions/sentiment" in p for p in paths)


def test_mention_reach_per_project_disable_overrides_default(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/mentions/reach", _mention_reach_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="ke").fetch(
            projects=[
                {
                    "id": "1397483539",
                    "name": "Kenya trends",
                    "include_mentions_reach": False,
                }
            ],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_mentions_reach=True,
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any("/mentions/reach" in p for p in paths)


def test_daily_metrics_per_project_disable_overrides_default(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/daily-metrics", _daily_metrics_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="ke").fetch(
            projects=[
                {
                    "id": "1397483539",
                    "name": "Kenya trends",
                    "include_daily_metrics": False,
                }
            ],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_daily_metrics=True,
        )
    paths = [call.args[1] for call in session.request.call_args_list]
    assert not any("/daily-metrics" in p for p in paths)


def test_mention_sentiment_per_project_lookback_override(monkeypatch):
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/mentions/sentiment", _mention_sentiment_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[
                {
                    "id": "1397483532",
                    "name": "SA",
                    "mention_sentiment_lookback_days": 14,
                }
            ],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_mentions_sentiment=True,
        )
    calls = [
        call for call in session.request.call_args_list if "/mentions/sentiment" in call.args[1]
    ]
    assert len(calls) == 1
    params = calls[0].kwargs.get("params") or {}
    expected = (datetime.now(UTC).date() - timedelta(days=14)).isoformat()
    assert params.get("date_from") == expected


def test_default_lookback_constants_drive_date_from_when_not_overridden(monkeypatch):
    """Sanity: all three module-level lookback constants are wired into
    the connector and default to the documented 7-day window."""
    assert MENTION_SENTIMENT_LOOKBACK_DAYS == 7
    assert MENTION_REACH_LOOKBACK_DAYS == 7
    assert DAILY_METRICS_LOOKBACK_DAYS == 7
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _wave2_session("/mentions/reach", _mention_reach_envelope())
    with patch.object(Brand24Connector, "_build_session", return_value=session):
        Brand24Connector(market="za").fetch(
            projects=[{"id": "1397483532", "name": "SA"}],
            include_topics=False,
            include_hashtags=False,
            include_followers=False,
            include_links=False,
            include_mentions=False,
            include_mentions_reach=True,
        )
    calls = [call for call in session.request.call_args_list if "/mentions/reach" in call.args[1]]
    assert len(calls) == 1
    params = calls[0].kwargs.get("params") or {}
    expected = (datetime.now(UTC).date() - timedelta(days=MENTION_REACH_LOOKBACK_DAYS)).isoformat()
    assert params.get("date_from") == expected


# Vendor retirement gate. Brand24 was cancelled 21 Aug 2026 and its billing
# cycle ends on the 24th, so from that date every call is against a dead
# account. Same defect class as the EnsembleData gate in #300: fetch read the
# credential first and proceeded on a credential alone, so a key reaching the
# job by any route would keep calling a cancelled vendor.


def test_brand24_enabled_helper_reads_the_block():
    from src.ingestion.connectors.brand24 import _brand24_enabled

    assert _brand24_enabled({"brand24": {"enabled": False}}) is False
    assert _brand24_enabled({"brand24": {"enabled": True}}) is True


def test_brand24_gate_defaults_to_enabled_when_key_absent():
    """A config that never carried the flag must behave exactly as before."""
    from src.ingestion.connectors.brand24 import _brand24_enabled

    assert _brand24_enabled({}) is True
    assert _brand24_enabled({"brand24": {"za": {"projects": []}}}) is True


def test_fetch_is_inert_when_brand24_disabled(monkeypatch):
    """Disabled in config means no vendor call, even with a live key present
    and an explicit project list passed in."""
    monkeypatch.setenv("BRAND24_API_KEY", "tok")
    session = _mock_session({"/topics": {"topics": [{"name": "x", "mentions": 1}]}})
    with (
        patch(
            "src.ingestion.connectors.brand24.load_sources",
            return_value={"brand24": {"enabled": False}},
        ),
        patch("requests.Session", return_value=session),
    ):
        df = Brand24Connector(market="za").fetch(projects=[{"id": "123"}])

    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    assert session.request.call_count == 0


def test_disabled_brand24_checks_the_gate_before_the_api_key(caplog):
    """Ordering proof. With the connector disabled AND no key set, the log must
    be the retirement line, not the missing-key warning. A gate that ran after
    the credential read would emit the warning instead, which is an error
    nobody can act on sitting on top of a decision already taken."""
    import logging
    import os

    os.environ.pop("BRAND24_API_KEY", None)
    with (
        patch(
            "src.ingestion.connectors.brand24.load_sources",
            return_value={"brand24": {"enabled": False}},
        ),
        caplog.at_level(logging.INFO),
    ):
        df = Brand24Connector(market="ng").fetch(projects=[{"id": "1"}])

    assert df.empty
    text = caplog.text
    assert "BRAND24_API_KEY not set" not in text
    assert "disabled in config" in text.lower()
