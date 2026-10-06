"""Unit tests for src/ingestion/connectors/bluesky.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.ingestion.connectors.base import RAW_COLUMNS
from src.ingestion.connectors.bluesky import (
    BLUESKY_XRPC_BASE,
    DEFAULT_LIMIT,
    SEARCH_POSTS_PATH,
    BlueskyConnector,
)


def _ok_resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def _post(
    rkey: str = "abc123",
    handle: str = "boomkat.com",
    text: str = "amapiano and gqom fiends take note",
    likes: int = 6,
    reposts: int = 3,
    replies: int = 1,
    quotes: int = 0,
) -> dict:
    """postView shape verified live 19 Jun 2026 on api.bsky.app."""
    return {
        "uri": f"at://did:plc:7ketojjqq3ygtxbdjx5j4zv4/app.bsky.feed.post/{rkey}",
        "cid": "bafyrei...",
        "author": {
            "did": "did:plc:7ketojjqq3ygtxbdjx5j4zv4",
            "handle": handle,
            "displayName": "Boomkat",
        },
        "record": {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": "2026-06-15T14:11:25.055Z",
            "langs": ["en"],
        },
        "replyCount": replies,
        "repostCount": reposts,
        "likeCount": likes,
        "quoteCount": quotes,
        "indexedAt": "2026-06-15T14:11:26.762Z",
    }


def _cfg(terms: dict | None = None, **extra) -> dict:
    markets = {
        "za": {"search_terms": (terms or {}).get("za", ["amapiano"])},
        "ng": {"search_terms": (terms or {}).get("ng", ["afrobeats"])},
        "ke": {"search_terms": (terms or {}).get("ke", ["gengetone"])},
    }
    return {"bluesky": {"enabled": True, "markets": markets, **extra}}


def test_fetch_returns_empty_when_disabled():
    """enabled:false short-circuits to empty without any HTTP call."""
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.bluesky.load_sources",
            return_value={"bluesky": {"enabled": False}},
        ),
        patch.object(BlueskyConnector, "_build_session", return_value=session),
    ):
        df = BlueskyConnector(market="za").fetch()
    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    session.get.assert_not_called()


def test_fetch_returns_empty_for_unsupported_market():
    session = MagicMock()
    with (
        patch(
            "src.ingestion.connectors.bluesky.load_sources",
            return_value=_cfg(),
        ),
        patch.object(BlueskyConnector, "_build_session", return_value=session),
    ):
        df = BlueskyConnector(market="xx").fetch()
    assert df.empty
    session.get.assert_not_called()


def test_fetch_returns_empty_when_no_terms():
    session = MagicMock()
    cfg = {"bluesky": {"enabled": True, "markets": {"za": {"search_terms": []}}}}
    with (
        patch("src.ingestion.connectors.bluesky.load_sources", return_value=cfg),
        patch.object(BlueskyConnector, "_build_session", return_value=session),
    ):
        df = BlueskyConnector(market="za").fetch()
    assert df.empty
    session.get.assert_not_called()


def test_fetch_happy_path_parses_envelope():
    """posts[] -> RAW_COLUMNS rows with author, counts, and web url."""
    session = MagicMock()
    session.get.return_value = _ok_resp({"posts": [_post()], "cursor": "x"})
    with (
        patch("src.ingestion.connectors.bluesky.load_sources", return_value=_cfg()),
        patch.object(BlueskyConnector, "_build_session", return_value=session),
    ):
        df = BlueskyConnector(market="za").fetch()
    assert len(df) == 1
    assert list(df.columns) == list(RAW_COLUMNS)
    row = df.iloc[0]
    assert row["source"] == "bluesky"
    assert row["platform"] == "bluesky"
    assert row["market"] == "za"
    assert row["content_type"] == "social_post"
    assert row["query_group"] == "bluesky_search"
    assert row["query_term"] == "amapiano"
    assert row["author_handle"] == "boomkat.com"
    assert int(row["likes"]) == 6
    assert int(row["comments"]) == 1
    # shares = reposts + quotes.
    assert int(row["shares"]) == 3
    assert row["url"] == "https://bsky.app/profile/boomkat.com/post/abc123"


def test_search_url_and_params_shape():
    """Request hits the searchPosts XRPC path with q + limit + sort."""
    session = MagicMock()
    session.get.return_value = _ok_resp({"posts": [_post()]})
    with (
        patch(
            "src.ingestion.connectors.bluesky.load_sources",
            return_value=_cfg(sort="latest", limit=10),
        ),
        patch.object(BlueskyConnector, "_build_session", return_value=session),
    ):
        BlueskyConnector(market="ng").fetch()
    call = session.get.call_args
    assert call[0][0] == f"{BLUESKY_XRPC_BASE}/{SEARCH_POSTS_PATH}"
    params = call.kwargs["params"]
    assert params["q"] == "afrobeats"
    assert params["limit"] == 10
    assert params["sort"] == "latest"


def test_dedupes_posts_across_terms():
    """A post matching two terms is one row."""
    session = MagicMock()
    same = _post(rkey="dup", handle="user.bsky.social")
    session.get.return_value = _ok_resp({"posts": [same]})
    cfg = {
        "bluesky": {
            "enabled": True,
            "markets": {"za": {"search_terms": ["amapiano", "kasi"]}},
        }
    }
    with (
        patch("src.ingestion.connectors.bluesky.load_sources", return_value=cfg),
        patch.object(BlueskyConnector, "_build_session", return_value=session),
    ):
        df = BlueskyConnector(market="za").fetch()
    # Two terms, same post returned each time -> deduped to one row.
    assert session.get.call_count == 2
    assert len(df) == 1


def test_empty_text_post_dropped():
    session = MagicMock()
    blank = _post(text="")
    session.get.return_value = _ok_resp({"posts": [blank]})
    with (
        patch("src.ingestion.connectors.bluesky.load_sources", return_value=_cfg()),
        patch.object(BlueskyConnector, "_build_session", return_value=session),
    ):
        df = BlueskyConnector(market="za").fetch()
    assert df.empty


def test_non_200_returns_empty():
    session = MagicMock()
    bad = MagicMock()
    bad.status_code = 503
    session.get.return_value = bad
    with (
        patch("src.ingestion.connectors.bluesky.load_sources", return_value=_cfg()),
        patch.object(BlueskyConnector, "_build_session", return_value=session),
    ):
        df = BlueskyConnector(market="za").fetch()
    assert df.empty


def test_network_exception_returns_empty():
    session = MagicMock()
    session.get.side_effect = ConnectionError("network down")
    with (
        patch("src.ingestion.connectors.bluesky.load_sources", return_value=_cfg()),
        patch.object(BlueskyConnector, "_build_session", return_value=session),
    ):
        df = BlueskyConnector(market="za").fetch()
    assert df.empty


def test_created_at_parsed_to_iso_with_tz():
    session = MagicMock()
    session.get.return_value = _ok_resp({"posts": [_post()]})
    with (
        patch("src.ingestion.connectors.bluesky.load_sources", return_value=_cfg()),
        patch.object(BlueskyConnector, "_build_session", return_value=session),
    ):
        df = BlueskyConnector(market="za").fetch()
    from datetime import datetime as dt

    parsed = dt.fromisoformat(df.iloc[0]["published_at"])
    assert parsed.tzinfo is not None
    assert parsed.year == 2026


def test_constants_match_documented_values():
    assert DEFAULT_LIMIT == 25
    assert BLUESKY_XRPC_BASE == "https://api.bsky.app/xrpc"
    assert SEARCH_POSTS_PATH == "app.bsky.feed.searchPosts"
