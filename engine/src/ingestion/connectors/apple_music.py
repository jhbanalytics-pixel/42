"""Apple Music RSS Charts connector.

Pulls Apple's free public RSS "Most Played" charts per market. Replaces
the Spotify chart connector because Spotify's Web API client_credentials
flow blocks all playlist access (verified via live probe 28 May 2026:
editorial AND community playlists return 403 / 404). Apple Music RSS is
free, no auth, no rate limits, returns 50 tracks per market with name,
artist, genre.

Endpoint shape:
    https://rss.marketingtools.apple.com/api/v2/{market}/music/most-played/{N}/songs.json

Response shape (verified live 28 May 2026):
    {
      "feed": {
        "title": "Top Songs",
        "country": "za",
        "results": [
          {"name": "...", "artistName": "...", "url": "...",
           "genres": [{"name": "Afrobeats", ...}],
           "releaseDate": "2026-05-15",
           "kind": "song",
           "id": "..."}
        ]
      }
    }

Direct verification of music_amapiano / music_afrobeats / music_gengetone
scoring. ~150 rows/day across 3 markets at zero $.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from src.ingestion.connectors.base import RAW_COLUMNS, BaseConnector
from src.utils.config_loader import load_sources

# rss.applemarketingtools.com now 301-redirects here (verified live 20 Jul 2026).
APPLE_MUSIC_RSS_BASE = "https://rss.marketingtools.apple.com/api/v2"
DEFAULT_TRACKS = 50
# One bounded app-level retry. Session read-retries are disabled globally
# (13/14 Jul dead night), but this connector is exactly one GET per market
# per day, so a second attempt caps at +15s and prevents a transient read
# timeout starving a market's chart for the whole day (20 Jul KE flat-line).
FETCH_ATTEMPTS = 2
RETRY_PAUSE_SECONDS = 3.0
SUPPORTED_MARKETS: frozenset[str] = frozenset({"za", "ng", "ke"})


class AppleMusicConnector(BaseConnector):
    """Fetches Apple Music RSS chart per market."""

    SOURCE_NAME = "apple_music"
    PLATFORM = "apple_music"
    BASE_URL = APPLE_MUSIC_RSS_BASE
    RATE_LIMIT_DELAY = 0.5

    def fetch(
        self,
        *,
        active: bool | None = None,
        tracks: int | None = None,
        **_: Any,
    ) -> pd.DataFrame:
        """Pull the daily Top 50 for this market via Apple RSS.

        Args:
            active: per-config global gate. When False, returns empty.
            tracks: how many rows to pull (max 50 documented).

        Returns DataFrame matching RAW_COLUMNS schema. Empty on any
        fetch failure (defensive — chart data is supplementary signal,
        cron should never block on it).
        """
        cfg = self._config()
        # active gates the WHOLE connector. Per-market market_active gates
        # one market.
        is_active = active if active is not None else cfg.get("active", True)
        if not is_active:
            return self._empty()

        if self.market not in SUPPORTED_MARKETS:
            self.logger.info("apple_music: unsupported market %s, returning empty", self.market)
            return self._empty()

        per_market = (cfg.get("markets") or {}).get(self.market) or {}
        if per_market.get("active") is False:
            return self._empty()

        n = int(tracks or per_market.get("tracks") or cfg.get("tracks") or DEFAULT_TRACKS)
        n = max(1, min(n, 50))  # cap at vendor max

        url = f"{APPLE_MUSIC_RSS_BASE}/{self.market}/music/most-played/{n}/songs.json"
        resp = None
        for attempt in range(1, FETCH_ATTEMPTS + 1):
            try:
                resp = self._session.get(url, timeout=15)
                break
            except Exception as exc:
                self.logger.warning(
                    "apple_music fetch failed (attempt %d/%d): %s", attempt, FETCH_ATTEMPTS, exc
                )
                if attempt < FETCH_ATTEMPTS:
                    time.sleep(RETRY_PAUSE_SECONDS)
        if resp is None:
            return self._empty()

        if resp.status_code != 200:
            self.logger.warning(
                "apple_music: status=%d for market=%s", resp.status_code, self.market
            )
            return self._empty()

        try:
            feed = (resp.json() or {}).get("feed") or {}
        except ValueError:
            self.logger.warning("apple_music: non-JSON response for market=%s", self.market)
            return self._empty()

        results = feed.get("results") or []
        if not results:
            self.logger.info("apple_music: empty results for market=%s", self.market)
            return self._empty()

        rows = [self._normalise_track(t, rank) for rank, t in enumerate(results, 1)]
        self.logger.info("apple_music: fetched %d chart rows for market=%s", len(rows), self.market)
        return pd.DataFrame(rows, columns=RAW_COLUMNS)

    def _config(self) -> dict[str, Any]:
        sources = load_sources()
        return sources.get("apple_music") or {}

    def _normalise_track(self, track: dict[str, Any], rank: int) -> dict[str, Any]:
        """Shape one chart entry into RAW_COLUMNS."""
        name = str(track.get("name") or "").strip()
        artist = str(track.get("artistName") or "").strip()
        url = str(track.get("url") or "").strip()
        genres = track.get("genres") or []
        primary_genre = str(genres[0].get("name")) if genres and isinstance(genres[0], dict) else ""
        release_date = str(track.get("releaseDate") or "").strip()
        published_at = self._parse_release(release_date)
        text = (
            f"{artist} - {name}: ranked #{rank} on Apple Music Top {len((track,)) and 50} "
            f"{self.market.upper()} on {datetime.now(UTC).date().isoformat()}. "
            f"Genre: {primary_genre or 'unknown'}. Release: {release_date or 'unknown'}."
        )
        return {
            "source": "apple_music",
            "platform": "apple_music",
            "market": self.market,
            "content_type": "chart_track",
            "query_group": "apple_music_top_50",
            "query_term": primary_genre or name,
            "author_name": artist[:300],
            "author_handle": "",
            "title": f"{name} - {artist}"[:300],
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

    def _parse_release(self, raw: str) -> str:
        """Apple returns YYYY-MM-DD; normalise to ISO with UTC. Falls back to now."""
        if raw and len(raw) >= 10:
            try:
                d = datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=UTC)
                return d.isoformat()
            except ValueError:
                pass
        return datetime.now(UTC).isoformat()

    def _empty(self) -> pd.DataFrame:
        return pd.DataFrame(columns=RAW_COLUMNS)


__all__ = ["APPLE_MUSIC_RSS_BASE", "DEFAULT_TRACKS", "AppleMusicConnector"]
