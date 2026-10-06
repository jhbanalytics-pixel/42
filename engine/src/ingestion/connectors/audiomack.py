"""Audiomack trending connector (dark).

Pulls Audiomack's trending tracks. Audiomack is Nigeria's dominant
streaming platform, so trending there leads the charts the engine
already reads (Apple Music) by days for afrobeats and adjacent genres.

Endpoint (docs: https://www.audiomack.com/data-api/docs):
    GET https://api.audiomack.com/v1/music/trending[?genre=<slug>]
    OAuth 1.0a signed (2-legged, consumer key + secret only), env
    AUDIOMACK_CONSUMER_KEY / AUDIOMACK_CONSUMER_SECRET.

Response shape is built from the public docs, NOT live-probed: consumer
keys were requested 4 Jul 2026 and are pending. A live-probe validation
pass is REQUIRED before the flag ever flips, per the
validate-vendor-before-flag-flip rule. Docs shape: {"results": [{"id",
"title", "artist", "genre", "url_slug", "uploader": {...}, "stats":
{"plays-raw", ...}, "released"}]} with plays possibly at
stats["plays-raw"] or a top-level "plays"; the parser accepts both.

Geo note: the API is not geo-scoped. Market attribution is a per-market
genre heuristic carried in configs/sources.yaml (ng afrobeats, za
amapiano/house, ke gengetone), documented as best-effort.

Dark on ship: audiomack.enabled defaults false. Enabled without keys
logs and returns empty, never raises.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from src.ingestion.connectors.base import RAW_COLUMNS, BaseConnector
from src.utils.config_loader import load_sources

AUDIOMACK_TRENDING_URL = "https://api.audiomack.com/v1/music/trending"
DEFAULT_GENRES_BY_MARKET: dict[str, list[str]] = {
    "ng": ["afrobeats"],
    "za": ["afrobeats"],
    "ke": ["afrobeats"],
}

USER_AGENT = (
    "Mozilla/5.0 (compatible; OgilvyTrendsEngineV2/1.0; "
    "+https://github.com/jhbanalytics-pixel/trends-engine-v2)"
)


class AudiomackConnector(BaseConnector):
    """Fetches Audiomack trending tracks per market genre pool."""

    SOURCE_NAME = "audiomack"
    PLATFORM = "audiomack"

    def fetch(self, **_: Any) -> pd.DataFrame:
        """Pull trending tracks for this market's configured genres.

        Returns DataFrame matching RAW_COLUMNS. Empty on any failure or
        missing credentials; a dark supplementary signal must never
        block the cron.
        """
        cfg = self._config()
        if not cfg.get("enabled", False):
            return self._empty()

        key = os.environ.get("AUDIOMACK_CONSUMER_KEY", "").strip()
        secret = os.environ.get("AUDIOMACK_CONSUMER_SECRET", "").strip()
        if not key or not secret:
            self.logger.info("audiomack: enabled but consumer key/secret absent, returning empty")
            return self._empty()

        per_market = (cfg.get("markets") or {}).get(self.market) or {}
        genres = per_market.get("genres") or DEFAULT_GENRES_BY_MARKET.get(self.market)
        if not genres:
            self.logger.info("audiomack: unsupported market %s, returning empty", self.market)
            return self._empty()

        try:
            from requests_oauthlib import OAuth1
        except ImportError:
            self.logger.warning("audiomack: requests-oauthlib not installed, returning empty")
            return self._empty()

        auth = OAuth1(key, client_secret=secret)
        headers = {"User-Agent": USER_AGENT}
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for genre in genres:
            url = f"{AUDIOMACK_TRENDING_URL}?genre={genre}"
            try:
                resp = self._session.get(url, headers=headers, auth=auth, timeout=15)
            except Exception as exc:
                self.logger.warning("audiomack fetch failed for genre=%s: %s", genre, exc)
                continue
            if resp.status_code != 200:
                self.logger.warning(
                    "audiomack: status=%d genre=%s market=%s",
                    resp.status_code,
                    genre,
                    self.market,
                )
                continue
            try:
                payload = resp.json()
            except ValueError as exc:
                self.logger.warning("audiomack: malformed JSON genre=%s: %s", genre, exc)
                continue

            for i, item in enumerate(payload.get("results") or [], start=1):
                if not isinstance(item, dict):
                    continue
                track_id = str(item.get("id") or "")
                if track_id and track_id in seen_ids:
                    continue
                row = self._normalise_track(item, genre, i)
                if row is not None:
                    rows.append(row)
                    if track_id:
                        seen_ids.add(track_id)

        if not rows:
            self.logger.info("audiomack: no rows for market=%s", self.market)
            return self._empty()
        self.logger.info("audiomack: fetched %d rows for market=%s", len(rows), self.market)
        return pd.DataFrame(rows, columns=RAW_COLUMNS)

    def _config(self) -> dict[str, Any]:
        sources = load_sources()
        return sources.get("audiomack") or {}

    def _normalise_track(
        self, item: dict[str, Any], genre: str, rank: int
    ) -> dict[str, Any] | None:
        title = str(item.get("title") or "").strip()
        if not title:
            return None
        artist = str(item.get("artist") or "").strip()
        stats = item.get("stats") or {}
        plays = stats.get("plays-raw") if isinstance(stats, dict) else None
        if plays is None:
            plays = item.get("plays")
        try:
            plays_int = int(plays) if plays is not None else 0
        except (TypeError, ValueError):
            plays_int = 0

        slug = str(item.get("url_slug") or "").strip()
        uploader = item.get("uploader") or {}
        uploader_slug = (
            str(uploader.get("url_slug") or "").strip() if isinstance(uploader, dict) else ""
        )
        url = f"https://audiomack.com/{uploader_slug}/song/{slug}" if slug and uploader_slug else ""

        text = f"audiomack trending rank {rank}. {artist or 'unknown artist'}. genre {genre}."
        if plays_int:
            text += f" plays {plays_int}."

        return {
            "source": "audiomack",
            "platform": "audiomack",
            "market": self.market,
            "content_type": "music_trending",
            "query_group": f"audiomack_trending_{genre}",
            "query_term": title,
            "author_name": artist,
            "author_handle": "",
            "title": title[:300],
            "text": text[:4000],
            "url": url,
            "published_at": self._parse_released(item.get("released")),
            "views": plays_int,
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "v2tone": "",
            "v2persons": "",
            "v2orgs": "",
            "v2locations": "",
            "v2gcam": "",
        }

    @staticmethod
    def _parse_released(raw: Any) -> str:
        """Docs show a unix-epoch released field; fall back to now UTC."""
        if raw is not None:
            try:
                return datetime.fromtimestamp(int(raw), tz=UTC).isoformat()
            except (TypeError, ValueError, OSError):
                pass
        return datetime.now(UTC).isoformat()

    def _empty(self) -> pd.DataFrame:
        return pd.DataFrame(columns=RAW_COLUMNS)


__all__ = ["AUDIOMACK_TRENDING_URL", "AudiomackConnector"]
