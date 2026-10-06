"""Cloudflare Radar per-country domain-ranking connector (dark).

Pulls Cloudflare's Radar top-domains ranking per market. What a whole
country's internet attention is pointed at, updated daily; traffic
anomalies around protests or shutdowns surface here before news copy.

Endpoint:
    GET https://api.cloudflare.com/client/v4/radar/ranking/top
        ?location={CC}&limit={N}
    Authorization: Bearer <token>   (env CLOUDFLARE_RADAR_TOKEN)

Response shape is built from the published API docs, NOT live-probed
(token pending). A live-probe validation pass is REQUIRED before the
flag ever flips, per the validate-vendor-before-flag-flip rule. Docs
shape: {"success": true, "result": {"top_0": [{"rank", "domain",
"categories": [{"name"}]}]}} with the ranking array under a
"top_0"-style key or "rankings"; the parser accepts both plus a plain
list fallback.

Radar data is licensed CC BY 4.0; cite Cloudflare Radar as the source
in any rendered output that leans on it.

Dark on ship: cloudflare_radar.enabled defaults false in
configs/sources.yaml. Enabled without a token logs and returns empty,
never raises.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from src.ingestion.connectors.base import RAW_COLUMNS, BaseConnector
from src.utils.config_loader import load_sources

RADAR_RANKING_URL = "https://api.cloudflare.com/client/v4/radar/ranking/top"
LOCATION_BY_MARKET: dict[str, str] = {"za": "ZA", "ng": "NG", "ke": "KE"}
DEFAULT_LIMIT = 25

USER_AGENT = (
    "Mozilla/5.0 (compatible; OgilvyTrendsEngineV2/1.0; "
    "+https://github.com/jhbanalytics-pixel/trends-engine-v2)"
)


class CloudflareRadarConnector(BaseConnector):
    """Fetches Cloudflare Radar's top-domains ranking for one market."""

    SOURCE_NAME = "cloudflare_radar"
    PLATFORM = "web_attention"

    def fetch(self, **_: Any) -> pd.DataFrame:
        """Pull the per-country domain ranking for this market.

        Returns DataFrame matching RAW_COLUMNS. Empty on any failure or
        missing credentials; a dark supplementary signal must never
        block the cron.
        """
        cfg = self._config()
        if not cfg.get("enabled", False):
            return self._empty()

        token = os.environ.get("CLOUDFLARE_RADAR_TOKEN", "").strip()
        if not token:
            self.logger.info(
                "cloudflare_radar: enabled but CLOUDFLARE_RADAR_TOKEN absent, returning empty"
            )
            return self._empty()

        per_market = (cfg.get("markets") or {}).get(self.market) or {}
        location = per_market.get("location") or LOCATION_BY_MARKET.get(self.market)
        if not location:
            self.logger.info(
                "cloudflare_radar: unsupported market %s, returning empty", self.market
            )
            return self._empty()

        limit = int(cfg.get("limit") or DEFAULT_LIMIT)
        url = f"{RADAR_RANKING_URL}?location={location}&limit={limit}"
        headers = {"User-Agent": USER_AGENT, "Authorization": f"Bearer {token}"}
        try:
            resp = self._session.get(url, headers=headers, timeout=15)
        except Exception as exc:
            self.logger.warning("cloudflare_radar fetch failed: %s", exc)
            return self._empty()

        if resp.status_code != 200:
            self.logger.warning(
                "cloudflare_radar: status=%d for market=%s", resp.status_code, self.market
            )
            return self._empty()

        try:
            payload = resp.json()
        except ValueError as exc:
            self.logger.warning(
                "cloudflare_radar: malformed JSON for market=%s: %s", self.market, exc
            )
            return self._empty()

        rankings = self._extract_rankings(payload)
        if not rankings:
            self.logger.info("cloudflare_radar: empty ranking for market=%s", self.market)
            return self._empty()

        rows = [
            row for row in (self._normalise_entry(e, location) for e in rankings) if row is not None
        ]
        self.logger.info("cloudflare_radar: fetched %d rows for market=%s", len(rows), self.market)
        return pd.DataFrame(rows, columns=RAW_COLUMNS)

    def _config(self) -> dict[str, Any]:
        sources = load_sources()
        return sources.get("cloudflare_radar") or {}

    @staticmethod
    def _extract_rankings(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Accept the documented envelope variants without guessing hard."""
        result = payload.get("result")
        if isinstance(result, list):
            return [e for e in result if isinstance(e, dict)]
        if isinstance(result, dict):
            for key in ("top_0", "rankings", "top"):
                val = result.get(key)
                if isinstance(val, list):
                    return [e for e in val if isinstance(e, dict)]
        return []

    def _normalise_entry(self, entry: dict[str, Any], location: str) -> dict[str, Any] | None:
        domain = str(entry.get("domain") or "").strip().lower()
        if not domain:
            return None
        rank = entry.get("rank")
        categories = entry.get("categories") or []
        cat_names = [
            str(c.get("name")).strip() for c in categories if isinstance(c, dict) and c.get("name")
        ]
        text_parts = [
            f"internet attention rank {rank if rank is not None else 'n/a'} in {location}."
        ]
        if cat_names:
            text_parts.append("categories " + ", ".join(cat_names[:4]))
        return {
            "source": "cloudflare_radar",
            "platform": "web_attention",
            "market": self.market,
            "content_type": "domain_ranking",
            "query_group": "radar_top_domains",
            "query_term": domain,
            "author_name": "",
            "author_handle": "",
            "title": domain[:300],
            "text": " ".join(text_parts)[:4000],
            "url": f"https://{domain}",
            "published_at": datetime.now(UTC).isoformat(),
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

    def _empty(self) -> pd.DataFrame:
        return pd.DataFrame(columns=RAW_COLUMNS)


__all__ = ["LOCATION_BY_MARKET", "RADAR_RANKING_URL", "CloudflareRadarConnector"]
