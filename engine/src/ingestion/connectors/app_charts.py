"""Apple App Store top-charts RSS connector.

Pulls Apple's free public top-charts feed per market storefront. Same
marketing-tools RSS API family as apple_music.py (Apple Music charts),
free, no auth, no rate limits.

Endpoint shape:
    https://rss.marketingtools.apple.com/api/v2/{storefront}/apps/{chart}/{limit}/apps.json

Response shape (live-verified 4 Jul 2026, za/ng/ke storefronts all HTTP 200):
    {
      "feed": {
        "title": "...",
        "updated": "...",
        "results": [
          {"artistName": "...", "id": "...", "name": "...",
           "releaseDate": "...", "kind": "...", "artworkUrl100": "...",
           "genres": [{"name": "...", ...}], "url": "..."}
        ]
      }
    }

Ships DARK: app_charts.enabled defaults to false in configs/sources.yaml.
With the flag off, zero HTTP calls occur.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd

from src.ingestion.connectors.base import RAW_COLUMNS, BaseConnector
from src.utils.config_loader import load_sources

APP_CHARTS_RSS_BASE = "https://rss.marketingtools.apple.com/api/v2"
DEFAULT_LIMIT = 25
DEFAULT_CHARTS = ["top-free"]


class AppChartsConnector(BaseConnector):
    """Fetches Apple App Store top-charts RSS per market storefront."""

    SOURCE_NAME = "app_charts"
    PLATFORM = "app_store"
    RATE_LIMIT_DELAY = 0.5

    def fetch(self, **_: Any) -> pd.DataFrame:
        """Pull the configured chart(s) for this market via Apple's feed generator.

        Returns DataFrame matching RAW_COLUMNS schema. Empty on any failure
        for a given market/chart combination (chart data is supplementary
        signal, cron should never block on it).
        """
        cfg = self._config()
        if not cfg.get("enabled", False):
            return self._empty()

        markets_cfg = cfg.get("markets") or {}
        market_cfg = markets_cfg.get(self.market) or {}
        storefront = str(market_cfg.get("storefront") or "").strip()
        if not storefront:
            self.logger.info(
                "app_charts: no storefront configured for market=%s, returning empty",
                self.market,
            )
            return self._empty()

        limit = int(cfg.get("limit") or DEFAULT_LIMIT)
        charts = cfg.get("charts") or DEFAULT_CHARTS

        all_rows: list[dict[str, Any]] = []
        for chart in charts:
            all_rows.extend(self._fetch_chart(storefront, chart, limit))

        if not all_rows:
            return self._empty()

        self.logger.info("app_charts: fetched %d rows for market=%s", len(all_rows), self.market)
        return pd.DataFrame(all_rows, columns=RAW_COLUMNS)

    def _fetch_chart(self, storefront: str, chart: str, limit: int) -> list[dict[str, Any]]:
        url = f"{APP_CHARTS_RSS_BASE}/{storefront}/apps/{chart}/{limit}/apps.json"
        try:
            resp = self._session.get(url, timeout=15)
        except Exception as exc:
            self.logger.warning("app_charts fetch failed for chart=%s: %s", chart, exc)
            return []

        if resp.status_code != 200:
            self.logger.warning(
                "app_charts: status=%d for market=%s chart=%s",
                resp.status_code,
                self.market,
                chart,
            )
            return []

        try:
            feed = (resp.json() or {}).get("feed") or {}
        except ValueError:
            self.logger.warning(
                "app_charts: non-JSON response for market=%s chart=%s", self.market, chart
            )
            return []

        results = feed.get("results") or []
        if not results:
            self.logger.info("app_charts: empty results for market=%s chart=%s", self.market, chart)
            return []

        published_at = self._parse_updated(str(feed.get("updated") or ""))
        return [
            self._normalise_app(app, rank, chart, published_at)
            for rank, app in enumerate(results, 1)
        ]

    def _config(self) -> dict[str, Any]:
        sources = load_sources()
        return sources.get("app_charts") or {}

    def _normalise_app(
        self, app: dict[str, Any], rank: int, chart: str, published_at: str
    ) -> dict[str, Any]:
        """Shape one chart entry into RAW_COLUMNS."""
        name = str(app.get("name") or "").strip()
        artist = str(app.get("artistName") or "").strip()
        url = str(app.get("url") or "").strip()
        genres = app.get("genres") or []
        genre_names = [str(g.get("name")) for g in genres if isinstance(g, dict) and g.get("name")]
        genre_text = ", ".join(genre_names)
        text = f"app chart {chart} rank {rank}. {genre_text}. by {artist}"
        return {
            "source": "app_charts",
            "platform": "app_store",
            "market": self.market,
            "content_type": "app_chart",
            "query_group": "app_charts_top_25",
            "query_term": name,
            "author_name": artist[:300],
            "author_handle": "",
            "title": name[:300],
            "text": text[:4000],
            "url": url,
            "published_at": published_at,
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

    def _parse_updated(self, raw: str) -> str:
        """Apple returns an ISO8601 timestamp with offset; normalise to UTC.
        Falls back to now() if missing or unparseable.
        """
        if raw:
            try:
                d = datetime.fromisoformat(raw)
                if d.tzinfo is None:
                    d = d.replace(tzinfo=UTC)
                return d.astimezone(UTC).isoformat()
            except ValueError:
                pass
        return datetime.now(UTC).isoformat()

    def _empty(self) -> pd.DataFrame:
        return pd.DataFrame(columns=RAW_COLUMNS)


__all__ = ["APP_CHARTS_RSS_BASE", "DEFAULT_CHARTS", "DEFAULT_LIMIT", "AppChartsConnector"]
