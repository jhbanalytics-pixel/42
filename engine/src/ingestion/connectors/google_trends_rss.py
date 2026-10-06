"""Google Trends daily-trending RSS connector (dark).

Pulls Google's public daily-trending-searches RSS feed per market. Free,
no auth, no rate limits documented.

Endpoint shape:
    https://trends.google.com/trending/rss?geo={GEO}

Response shape (live-verified 3 Jul 2026): RSS 2.0, each <item> carries
title (the search term), ht:approx_traffic (e.g. "1000+"), pubDate, one
or more ht:news_item blocks (ht:news_item_title, ht:news_item_url,
ht:news_item_source), ht:picture. Namespace ht =
https://trends.google.com/trending/rss.

Dark on ship: enabled defaults false in configs/sources.yaml. Zero
fetches when off.
"""

from __future__ import annotations

import email.utils
import xml.etree.ElementTree as ET  # nosec B405 - only used for the ParseError type; parsing itself goes through defusedxml
from datetime import UTC, datetime
from typing import Any

import defusedxml.ElementTree as DefusedET
import pandas as pd

from src.ingestion.connectors.base import RAW_COLUMNS, BaseConnector
from src.utils.config_loader import load_sources

GOOGLE_TRENDS_RSS_BASE = "https://trends.google.com/trending/rss"
GEO_BY_MARKET: dict[str, str] = {"za": "ZA", "ng": "NG", "ke": "KE"}
HT_NAMESPACE = "https://trends.google.com/trending/rss"
MAX_NEWS_ITEMS = 3

# Polite-bot UA, same convention as the rss connector: token/version +
# compatible parenthetical + contact URL, per RFC 9110 section 10.1.5.
USER_AGENT = (
    "Mozilla/5.0 (compatible; OgilvyTrendsEngineV2/1.0; "
    "+https://github.com/jhbanalytics-pixel/trends-engine-v2)"
)


class GoogleTrendsRssConnector(BaseConnector):
    """Fetches Google's daily-trending-searches RSS feed per market."""

    SOURCE_NAME = "google_trends_rss"
    PLATFORM = "google_search"

    def fetch(self, **_: Any) -> pd.DataFrame:
        """Pull the daily trending-searches feed for this market.

        Returns DataFrame matching RAW_COLUMNS schema. Empty on any
        fetch failure, this is a dark supplementary signal, cron
        should never block on it.
        """
        cfg = self._config()
        if not cfg.get("enabled", False):
            return self._empty()

        per_market = (cfg.get("markets") or {}).get(self.market) or {}
        geo = per_market.get("geo") or GEO_BY_MARKET.get(self.market)
        if not geo:
            self.logger.info(
                "google_trends_rss: unsupported market %s, returning empty", self.market
            )
            return self._empty()

        url = f"{GOOGLE_TRENDS_RSS_BASE}?geo={geo}"
        headers = {"User-Agent": USER_AGENT}
        try:
            resp = self._session.get(url, headers=headers, timeout=15)
        except Exception as exc:
            self.logger.warning("google_trends_rss fetch failed: %s", exc)
            return self._empty()

        if resp.status_code != 200:
            self.logger.warning(
                "google_trends_rss: status=%d for market=%s", resp.status_code, self.market
            )
            return self._empty()

        try:
            root = DefusedET.fromstring(resp.content)
        except ET.ParseError as exc:
            self.logger.warning(
                "google_trends_rss: malformed feed for market=%s: %s", self.market, exc
            )
            return self._empty()

        items = root.findall(".//item")
        if not items:
            self.logger.info("google_trends_rss: empty feed for market=%s", self.market)
            return self._empty()

        rows = [self._normalise_item(item) for item in items]
        self.logger.info("google_trends_rss: fetched %d rows for market=%s", len(rows), self.market)
        return pd.DataFrame(rows, columns=RAW_COLUMNS)

    def _config(self) -> dict[str, Any]:
        sources = load_sources()
        return sources.get("google_trends_rss") or {}

    def _normalise_item(self, item: ET.Element) -> dict[str, Any]:
        """Shape one <item> into RAW_COLUMNS."""
        ht = f"{{{HT_NAMESPACE}}}"
        title = (item.findtext("title") or "").strip()
        traffic = (item.findtext(f"{ht}approx_traffic") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()

        news_items = item.findall(f"{ht}news_item")
        news_titles: list[str] = []
        first_url = ""
        for news_item in news_items[:MAX_NEWS_ITEMS]:
            news_title = (news_item.findtext(f"{ht}news_item_title") or "").strip()
            if news_title:
                news_titles.append(news_title)
            if not first_url:
                news_url = (news_item.findtext(f"{ht}news_item_url") or "").strip()
                if news_url:
                    first_url = news_url

        text_parts = [f"search interest ~{traffic or 'unknown'}."]
        if news_titles:
            text_parts.append("; ".join(news_titles))
        text = " ".join(text_parts)

        return {
            "source": "google_trends_rss",
            "platform": "google_search",
            "market": self.market,
            "content_type": "trending_search",
            "query_group": "google_trends_daily",
            "query_term": title,
            "author_name": "",
            "author_handle": "",
            "title": title[:300],
            "text": text[:4000],
            "url": first_url,
            "published_at": self._parse_pub_date(pub_date),
            "views": 0,
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "v2tone": "",
            "v2persons": "",
            "v2orgs": "",
            "v2locations": "",
            "v2gcam": "",
        }

    def _parse_pub_date(self, raw: str) -> str:
        """RFC 2822 pubDate; normalise to ISO with UTC. Falls back to now."""
        if raw:
            try:
                parsed = email.utils.parsedate_to_datetime(raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed.astimezone(UTC).isoformat()
            except (TypeError, ValueError):
                pass
        return datetime.now(UTC).isoformat()

    def _empty(self) -> pd.DataFrame:
        return pd.DataFrame(columns=RAW_COLUMNS)


__all__ = ["GEO_BY_MARKET", "GOOGLE_TRENDS_RSS_BASE", "GoogleTrendsRssConnector"]
