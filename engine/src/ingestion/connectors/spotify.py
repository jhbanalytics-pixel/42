"""Spotify Charts connector.

Pulls per-market chart data so the topic classifier sees independent
verification of music_amapiano / music_afrobeats / music_gengetone
scoring. A track holding #1 on the ZA Top 200 for four days is hard
evidence that the amapiano topic deserves a Trending tier bump
regardless of social-signal noise.

Vendor surface (live-probed 28 May 2026):
    GET https://charts.spotify.com/charts/view/regional-za-daily/latest
        -> 200 text/html, empty SPA shell, data loaded via authenticated
           internal API. No public unauthenticated extract.
    GET https://charts.spotify.com/csv/regional-za-daily/<date>/download
        -> 404. The legacy spotifycharts.com CSV download was deprecated
           when the dashboard moved to charts.spotify.com.
    GET https://spotifycharts.com/regional/za/daily/latest/download
        -> 302 redirect to charts.spotify.com/charts/overview/global (same
           SPA shell, no CSV).

Conclusion: the documented "open the CSV path" route does not exist any
more. Public chart access goes through the Spotify Web API with OAuth
client credentials flow. This connector implements that path:

    1. POST https://accounts.spotify.com/api/token with client_credentials
       grant. Returns a short-lived bearer token.
    2. GET https://api.spotify.com/v1/playlists/<playlist_id>/tracks for
       each configured chart playlist. Spotify maintains official "Top 50
       <Country>" and viral playlists with stable IDs that mirror the
       chart pages.

Default-off via sources.yaml `spotify.enabled` so the connector is wired
into the pipeline but produces zero rows until Albert provisions
SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET and flips the flag. Net cost
to Day 0 pipeline: zero new external calls, zero new $/mo.

Required env / Secret Manager values when activated:
    SPOTIFY_CLIENT_ID
    SPOTIFY_CLIENT_SECRET

YAML block to add to configs/sources.yaml (not touched by this commit):

    spotify:
      enabled: false              # flip to true after secrets provisioned
      active: true                # per-spec name retained for symmetry
      tracks_per_chart: 50
      markets:
        za:
          charts:
            regional: 37i9dQZEVXbMH2jvi6jeGq   # Top 50 - South Africa
            viral: 37i9dQZEVXbLiRSasKsNU9      # Viral 50 - South Africa
        ng:
          charts:
            regional: 37i9dQZEVXbKY7jLzlJ11V   # Top 50 - Nigeria
            viral: 37i9dQZEVXbLQoEbCY3eRy      # Viral 50 - Nigeria
        ke:
          charts:
            regional: 37i9dQZEVXbKqiTGXuCOsB   # Top 50 - Kenya
            viral: 37i9dQZEVXbJOu05PtUcCH      # Viral 50 - Kenya

Playlist IDs above are Spotify's editorial chart playlists for each
market; verify with the operator before flipping the flag because
Spotify occasionally renames or retires regional playlists.
"""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime
from typing import Any, ClassVar

import pandas as pd
import requests

from src.ingestion.connectors.base import BaseConnector
from src.utils.config_loader import load_sources
from src.utils.secrets import get_secret

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"  # nosec B105
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
DEFAULT_TRACKS_PER_CHART = 50
DEFAULT_CHART_TYPES: tuple[str, ...] = ("regional", "viral")


class SpotifyConnector(BaseConnector):
    """Fetches Spotify chart playlists for a specific market."""

    SOURCE_NAME = "spotify"
    PLATFORM = "spotify"
    BASE_URL = SPOTIFY_API_BASE
    RATE_LIMIT_DELAY = 0.2

    # Process-wide cache so the same chart playlist is not fetched twice
    # in a single pipeline run if it is mounted under more than one
    # market key in the config (rare, but defensive).
    _fetched_playlists: ClassVar[set[str]] = set()
    # Process-wide OAuth token cache. Spotify tokens last 3600s; one
    # token covers every market in a run.
    _cached_token: ClassVar[str] = ""
    _cached_token_expires_at: ClassVar[float] = 0.0

    @classmethod
    def reset_caches(cls) -> None:
        """Reset per-process caches. Intended for tests."""
        cls._fetched_playlists.clear()
        cls._cached_token = ""  # nosec B105
        cls._cached_token_expires_at = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(self, **_kwargs: Any) -> pd.DataFrame:
        """Fetch configured chart playlists for this connector's market."""
        sources = load_sources()
        config = sources.get("spotify", {}) or {}

        if not config.get("enabled", False):
            self.logger.info("Spotify connector disabled in sources.yaml")
            return self.empty_dataframe()
        if not config.get("active", True):
            self.logger.info("Spotify connector inactive in sources.yaml")
            return self.empty_dataframe()

        markets_cfg = (config.get("markets") or {}).get(self.market) or {}
        charts_cfg = markets_cfg.get("charts") or {}
        if not charts_cfg:
            self.logger.info("No Spotify chart playlists configured for market=%s", self.market)
            return self.empty_dataframe()

        client_id = get_secret("SPOTIFY_CLIENT_ID")
        client_secret = get_secret("SPOTIFY_CLIENT_SECRET")
        if not client_id or not client_secret:
            self.logger.warning("SPOTIFY_CLIENT_ID/SECRET not configured; skipping spotify fetch")
            return self.empty_dataframe()

        try:
            token = self._get_access_token(client_id, client_secret)
        except requests.exceptions.RequestException as e:
            self.logger.error("Spotify token fetch failed: %s", str(e)[:200])
            return self.empty_dataframe()
        if not token:
            return self.empty_dataframe()

        tracks_per_chart = int(config.get("tracks_per_chart", DEFAULT_TRACKS_PER_CHART))
        chart_date = datetime.now(UTC).date().isoformat()

        # Normalise chart config. Only the dict form
        # `charts: {regional: <id>, viral: <id>}` is supported, since each
        # chart type needs an explicit playlist id. The list form
        # `charts: [regional, viral]` carries no ids, so it is unsupported
        # and skipped with a warning.
        normalised: dict[str, str] = {}
        if isinstance(charts_cfg, dict):
            for chart_type, playlist_id in charts_cfg.items():
                if chart_type and playlist_id:
                    normalised[str(chart_type)] = str(playlist_id)
        elif isinstance(charts_cfg, list):
            # List form: only chart types named, no playlist IDs. Skip
            # without crashing; the operator must move to dict form to
            # actually fetch anything.
            self.logger.warning(
                "spotify.markets.%s.charts is a list (chart types only); "
                "use a dict mapping chart_type -> playlist_id",
                self.market,
            )

        rows: list[dict[str, Any]] = []
        for chart_type, playlist_id in normalised.items():
            if playlist_id in self._fetched_playlists:
                self.logger.debug("Skip already-fetched playlist %s", playlist_id)
                continue
            try:
                tracks = self._fetch_playlist_tracks(playlist_id, token, tracks_per_chart)
            except requests.exceptions.RequestException as e:
                status = getattr(getattr(e, "response", None), "status_code", "???")
                if status == 404:
                    self.logger.warning("Spotify playlist %s returned 404; skipping", playlist_id)
                    continue
                self.logger.error(
                    "Spotify playlist %s fetch failed: %s",
                    playlist_id,
                    str(e)[:200],
                )
                continue
            # Cache only after a successful fetch. Caching before the attempt
            # would mark a transient failure as fetched and skip the playlist on
            # any later fetch() in the same process.
            self._fetched_playlists.add(playlist_id)
            for rank, item in enumerate(tracks, start=1):
                normalised_row = self._normalise_track(item, rank, chart_type, chart_date)
                if normalised_row:
                    rows.append(normalised_row)

        if not rows:
            return self.empty_dataframe()

        df = pd.DataFrame(rows)
        df = df.drop_duplicates(subset=["url", "query_group"])
        return df

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get_access_token(self, client_id: str, client_secret: str) -> str:
        """Return a cached OAuth bearer token, refreshing on expiry.

        Spotify client_credentials tokens are bound to the app, not a
        user, and last 3600s. The cache is class-level so one token
        serves every market in a pipeline run.
        """
        now = time.time()
        if SpotifyConnector._cached_token and now < SpotifyConnector._cached_token_expires_at - 60:
            return SpotifyConnector._cached_token

        creds = f"{client_id}:{client_secret}".encode()
        auth_header = base64.b64encode(creds).decode("ascii")
        resp = self._session.post(
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {auth_header}"},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        token = str(payload.get("access_token") or "")
        expires_in = int(payload.get("expires_in", 3600))
        SpotifyConnector._cached_token = token
        SpotifyConnector._cached_token_expires_at = now + expires_in
        return token

    def _fetch_playlist_tracks(
        self, playlist_id: str, token: str, limit: int
    ) -> list[dict[str, Any]]:
        """Fetch the first `limit` items from a playlist.

        Spotify caps `limit` at 50 per call; for limit > 50 we would need
        to paginate via `offset`. The chart use case sits at 50 so the
        first page is enough.
        """
        url = f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/tracks"
        params = {
            "limit": max(1, min(int(limit), 50)),
            "fields": ("items(track(id,name,external_urls(spotify),artists(name),album(name)))"),
        }
        resp = self._session.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json() or {}
        items = payload.get("items") or []
        return [it for it in items if isinstance(it, dict)]

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------

    def _normalise_track(
        self,
        item: dict[str, Any],
        rank: int,
        chart_type: str,
        chart_date: str,
    ) -> dict[str, Any] | None:
        """Map one Spotify playlist item to the RAW_COLUMNS schema."""
        track = (item or {}).get("track") or {}
        if not track:
            return None
        track_id = str(track.get("id") or "")
        track_name = str(track.get("name") or "").strip()
        artists = track.get("artists") or []
        artist_names = [str(a.get("name") or "").strip() for a in artists if isinstance(a, dict)]
        artist = ", ".join(n for n in artist_names if n)
        track_url = str(((track.get("external_urls") or {}).get("spotify")) or "").strip()
        if not track_name or not artist or not track_url:
            # Without name + artist + url we cannot reliably match a
            # topic, so drop the row rather than emit a half-shaped one.
            return None

        position_change = self._format_position_change(item.get("position_change"))
        position_phrase = f"Position change {position_change}." if position_change else ""

        text = (
            f"{artist} - {track_name} ranked #{rank} on Spotify "
            f"{chart_type} {self.market.upper()} on {chart_date}."
            + (f" {position_phrase}" if position_phrase else "")
        ).strip()

        posted_at = datetime.fromisoformat(chart_date).replace(tzinfo=UTC)

        return {
            "source": self.SOURCE_NAME,
            "platform": self.PLATFORM,
            "market": self.market,
            "content_type": "spotify_chart_track",
            "query_group": f"spotify_{chart_type}_top{DEFAULT_TRACKS_PER_CHART}",
            "query_term": track_name,
            "author_name": artist,
            "author_handle": "",
            "title": f"{track_name} - {artist}",
            "text": text,
            "url": track_url,
            "published_at": posted_at,
            "views": float(self._extract_streams(item)),
            "likes": 0.0,
            "comments": 0.0,
            "shares": 0.0,
            # GDELT-only columns held empty for non-GDELT rows.
            "v2tone": "",
            "v2persons": "",
            "v2orgs": "",
            "v2locations": "",
            # Surface track_id for downstream dedup; harmless extra key
            # that pandas drops when the schema is enforced.
            "_spotify_track_id": track_id,
        }

    @staticmethod
    def _extract_streams(item: dict[str, Any]) -> int:
        """Return stream count if the vendor surfaced it, else 0.

        The Web API /playlists/<id>/tracks response does NOT carry stream
        counts (those are only on the chart-page-internal API). Returns
        0 here, kept as a hook so a future Spotify API change can light
        up engagement without touching the connector shape.
        """
        streams = item.get("streams")
        if streams is None:
            return 0
        try:
            return int(streams)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _format_position_change(raw: Any) -> str:
        """Normalise position_change to "+5", "-3", "NEW", or "0".

        Spotify's internal chart API uses ints (signed) plus a "NEW"
        sentinel for fresh entries. The Web API path does not return
        this field, so this helper exists for the eventual hybrid path
        where the connector merges chart-page metadata with playlist
        track listings.
        """
        if raw is None:
            return ""
        if isinstance(raw, str):
            value = raw.strip().upper()
            if not value:
                return ""
            if value in {"NEW", "RE-ENTRY", "RE_ENTRY"}:
                return "NEW"
            # Strings carrying signed numbers
            try:
                n = int(value)
            except ValueError:
                return value
            return SpotifyConnector._format_position_change(n)
        if isinstance(raw, (int, float)):
            n = int(raw)
            if n == 0:
                return "0"
            return f"+{n}" if n > 0 else str(n)
        return ""
