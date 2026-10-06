"""Bluesky public feed connector (Wave 2).

Searches Bluesky's public AppView for posts matching per-market terms. No
auth, no key. Bluesky is open and growing in SSA; this gives the engine a
second open-web social signal alongside the EnsembleData surface.

Docs-confirmed (lexicon app.bsky.feed.searchPosts) and live probe
(19 Jun 2026). The public AppView host public.api.bsky.app is fronted by a
CDN that 403s server-side fetchers; the unfronted AppView api.bsky.app
returns 200 with no auth, so that is the host we use.

    GET https://api.bsky.app/xrpc/app.bsky.feed.searchPosts
        ?q={term}&limit={n}&sort={top|latest}&lang={lang}

Response envelope (verified live):
    {
      "posts": [
        {
          "uri": "at://did:plc:.../app.bsky.feed.post/{rkey}",
          "cid": "...",
          "author": {"did": "...", "handle": "boomkat.com",
                     "displayName": "boomkat.com"},
          "record": {"$type": "app.bsky.feed.post",
                     "text": "...", "createdAt": "2026-06-15T14:11:25.055Z",
                     "langs": ["en"]},
          "replyCount": 1, "repostCount": 3, "likeCount": 6, "quoteCount": 0,
          "indexedAt": "2026-06-15T14:11:26.762Z"
        }
      ],
      "cursor": "..."
    }

Notes that shaped the parser:
- hitsTotal is optional and was absent on the live probe; we never depend on
  it.
- The at:// uri carries the rkey as its last path segment; the human web URL
  is https://bsky.app/profile/{handle}/post/{rkey}.
- One HTTP call per search term. We dedupe posts by uri across terms within a
  run so a post matching two terms is one row.

Dark on ship: sources.yaml `bluesky.enabled` defaults false, so the connector
is wired into the registry but fetches nothing until the operator flips the
flag.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd

from src.ingestion.connectors.base import RAW_COLUMNS, BaseConnector
from src.utils.config_loader import load_sources

BLUESKY_XRPC_BASE = "https://api.bsky.app/xrpc"
SEARCH_POSTS_PATH = "app.bsky.feed.searchPosts"
DEFAULT_LIMIT = 25
DEFAULT_SORT = "top"
SUPPORTED_MARKETS: frozenset[str] = frozenset({"za", "ng", "ke"})


class BlueskyConnector(BaseConnector):
    """Searches Bluesky public posts for one market's terms."""

    SOURCE_NAME = "bluesky"
    PLATFORM = "bluesky"
    BASE_URL = BLUESKY_XRPC_BASE
    RATE_LIMIT_DELAY = 0.5

    def fetch(
        self,
        *,
        active: bool | None = None,
        limit: int | None = None,
        **_: Any,
    ) -> pd.DataFrame:
        """Search per-market terms and return one row per unique post.

        Args:
            active: per-config global gate. When False, returns empty.
            limit: posts per search term (1-100, vendor cap).

        Returns a DataFrame matching RAW_COLUMNS. Empty on any failure or
        when the flag is off.
        """
        cfg = self._config()
        is_enabled = active if active is not None else cfg.get("enabled", False)
        if not is_enabled:
            return self._empty()

        if self.market not in SUPPORTED_MARKETS:
            self.logger.info("bluesky: unsupported market %s, returning empty", self.market)
            return self._empty()

        terms = self._market_terms(cfg)
        if not terms:
            self.logger.info("bluesky: no search terms for market=%s", self.market)
            return self._empty()

        n = int(limit or cfg.get("limit") or DEFAULT_LIMIT)
        n = max(1, min(n, 100))
        sort = str(cfg.get("sort") or DEFAULT_SORT)
        lang = (cfg.get("markets") or {}).get(self.market, {}).get("lang") or ""

        seen_uris: set[str] = set()
        rows: list[dict[str, Any]] = []
        for term in terms:
            posts = self._search(term, n, sort, lang)
            for post in posts:
                uri = str(post.get("uri") or "")
                if not uri or uri in seen_uris:
                    continue
                seen_uris.add(uri)
                row = self._normalise_post(post, term)
                if row is not None:
                    rows.append(row)

        if not rows:
            return self._empty()

        self.logger.info(
            "bluesky: fetched %d unique posts across %d terms for market=%s",
            len(rows),
            len(terms),
            self.market,
        )
        return pd.DataFrame(rows, columns=RAW_COLUMNS)

    def _search(self, term: str, limit: int, sort: str, lang: str) -> list[dict[str, Any]]:
        """One searchPosts call. Returns the posts array, empty on any failure."""
        params: dict[str, Any] = {"q": term, "limit": limit, "sort": sort}
        if lang:
            params["lang"] = lang
        url = f"{BLUESKY_XRPC_BASE}/{SEARCH_POSTS_PATH}"
        try:
            resp = self._session.get(
                url,
                params=params,
                timeout=15,
                headers={"User-Agent": "TEV2-trends/1.0 (Ogilvy SSA trends engine)"},
            )
        except Exception as exc:
            self.logger.warning("bluesky search failed for term=%r: %s", term, exc)
            return []

        if resp.status_code != 200:
            self.logger.warning(
                "bluesky: status=%d for term=%r market=%s",
                resp.status_code,
                term,
                self.market,
            )
            return []

        try:
            return (resp.json() or {}).get("posts") or []
        except ValueError:
            self.logger.warning("bluesky: non-JSON response for term=%r", term)
            return []

    def _market_terms(self, cfg: dict[str, Any]) -> list[str]:
        """Resolve the search-term list for this market from config."""
        markets = cfg.get("markets") or {}
        per_market = markets.get(self.market) or {}
        terms = per_market.get("search_terms") or per_market.get("terms") or []
        return [str(t).strip() for t in terms if str(t).strip()]

    def _config(self) -> dict[str, Any]:
        sources = load_sources()
        return sources.get("bluesky") or {}

    def _normalise_post(self, post: dict[str, Any], term: str) -> dict[str, Any] | None:
        """Shape one postView into RAW_COLUMNS."""
        record = post.get("record") or {}
        text = str(record.get("text") or "").strip()
        if not text:
            return None

        author = post.get("author") or {}
        handle = str(author.get("handle") or "").strip()
        display_name = str(author.get("displayName") or handle).strip()
        created_at = self._parse_ts(record.get("createdAt") or post.get("indexedAt"))
        url = self._post_url(str(post.get("uri") or ""), handle)

        return {
            "source": "bluesky",
            "platform": "bluesky",
            "market": self.market,
            "content_type": "social_post",
            "query_group": "bluesky_search",
            "query_term": term[:300],
            "author_name": display_name[:300],
            "author_handle": handle[:300],
            "title": "",
            "text": text[:4000],
            "url": url,
            "published_at": created_at,
            "views": 0,
            "likes": int(post.get("likeCount") or 0),
            "comments": int(post.get("replyCount") or 0),
            "shares": int(post.get("repostCount") or 0) + int(post.get("quoteCount") or 0),
            "v2tone": "",
            "v2persons": "",
            "v2orgs": "",
            "v2locations": "",
            "v2gcam": "",
        }

    def _post_url(self, uri: str, handle: str) -> str:
        """Map an at:// post uri to the bsky.app web URL.

        at://did:plc:abc/app.bsky.feed.post/{rkey} ->
        https://bsky.app/profile/{handle}/post/{rkey}
        """
        if not uri:
            return ""
        rkey = uri.rsplit("/", 1)[-1] if "/" in uri else ""
        if handle and rkey:
            return f"https://bsky.app/profile/{handle}/post/{rkey}"
        return ""

    def _parse_ts(self, raw: Any) -> str:
        """Normalise an ISO timestamp to tz-aware ISO. Falls back to now."""
        if isinstance(raw, str) and raw:
            try:
                cleaned = raw.replace("Z", "+00:00")
                return datetime.fromisoformat(cleaned).astimezone(UTC).isoformat()
            except ValueError:
                pass
        return datetime.now(UTC).isoformat()

    def _empty(self) -> pd.DataFrame:
        return pd.DataFrame(columns=RAW_COLUMNS)


__all__ = [
    "BLUESKY_XRPC_BASE",
    "DEFAULT_LIMIT",
    "SEARCH_POSTS_PATH",
    "BlueskyConnector",
]
