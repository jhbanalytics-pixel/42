"""Unit tests for SpotifyConnector.

Coverage:
- enabled=false short-circuits with zero HTTP calls
- happy path: fetches top N per market per chart, normalises to RAW schema
- market filter honoured (KE config without `ng` block leaves NG empty)
- playlist response parses rank/track/artist/url
- 404 from a playlist returns empty (and does not abort the run)
- normalisation handles missing fields (no streams column, no album)
- position_change formatter handles "+5", "-3", "NEW", 0
- OAuth client_credentials path: no token endpoint hit when client id
  missing (sanity that "no auth = no fetch", and the inverse)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import responses as responses_lib
from src.ingestion.connectors.base import RAW_COLUMNS
from src.ingestion.connectors.spotify import (
    SPOTIFY_API_BASE,
    SPOTIFY_TOKEN_URL,
    SpotifyConnector,
)

FAKE_CLIENT_ID = "test-spotify-client-id"
FAKE_CLIENT_SECRET = "test-spotify-client-secret"
FAKE_ACCESS_TOKEN = "BQ-test-access-token-do-not-log"
ZA_REGIONAL_PLAYLIST = "37i9dQZEVXbMH2jvi6jeGq"
ZA_VIRAL_PLAYLIST = "37i9dQZEVXbLiRSasKsNU9"
NG_REGIONAL_PLAYLIST = "37i9dQZEVXbKY7jLzlJ11V"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    SpotifyConnector.reset_caches()
    yield
    SpotifyConnector.reset_caches()


def _sources(
    enabled: bool = True,
    active: bool = True,
    za_charts: dict | None = None,
    ng_charts: dict | None = None,
    ke_charts: dict | None = None,
    tracks_per_chart: int = 50,
) -> dict:
    markets = {}
    if za_charts is not None:
        markets["za"] = {"charts": za_charts}
    if ng_charts is not None:
        markets["ng"] = {"charts": ng_charts}
    if ke_charts is not None:
        markets["ke"] = {"charts": ke_charts}
    return {
        "spotify": {
            "enabled": enabled,
            "active": active,
            "tracks_per_chart": tracks_per_chart,
            "markets": markets,
        }
    }


def _track(track_id: str, name: str, artist: str) -> dict:
    return {
        "track": {
            "id": track_id,
            "name": name,
            "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
            "artists": [{"name": artist}],
            "album": {"name": "An Album"},
        }
    }


def _playlist_response(tracks: list[dict]) -> dict:
    return {"items": tracks}


def _mock_token(rsps: responses_lib.RequestsMock) -> None:
    rsps.add(
        responses_lib.POST,
        SPOTIFY_TOKEN_URL,
        json={"access_token": FAKE_ACCESS_TOKEN, "expires_in": 3600, "token_type": "Bearer"},
        status=200,
    )


def _mock_playlist(
    rsps: responses_lib.RequestsMock,
    playlist_id: str,
    tracks: list[dict],
    status: int = 200,
) -> None:
    body = _playlist_response(tracks) if status == 200 else {"error": {"status": status}}
    rsps.add(
        responses_lib.GET,
        f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/tracks",
        json=body,
        status=status,
    )


def _mk_connector(market: str = "za") -> SpotifyConnector:
    c = SpotifyConnector(market=market)
    c.RATE_LIMIT_DELAY = 0
    # Swap the per-instance logger for a MagicMock. Known Windows +
    # Python 3.13 native crash inside the stdlib logging module fires
    # whenever a connector logs from inside a `responses`-patched HTTP
    # context. CI on Ubuntu runs the real logger fine; locally the
    # MagicMock keeps the test green without changing behaviour.
    c.logger = MagicMock()
    return c


def _secret_resolver(name: str, default: str = "") -> str:
    """Mimic get_secret(name, default=...) returning Spotify creds."""
    return {
        "SPOTIFY_CLIENT_ID": FAKE_CLIENT_ID,
        "SPOTIFY_CLIENT_SECRET": FAKE_CLIENT_SECRET,
    }.get(name, default)


# ---------------------------------------------------------------------------
# Disabled / unconfigured short-circuits
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.spotify.get_secret", side_effect=_secret_resolver)
@patch("src.ingestion.connectors.spotify.load_sources")
def test_disabled_short_circuits(mock_load_sources, _mock_secret):
    """enabled=false means zero HTTP calls and empty 20-col DataFrame."""
    mock_load_sources.return_value = _sources(
        enabled=False, za_charts={"regional": ZA_REGIONAL_PLAYLIST}
    )
    df = _mk_connector("za").fetch()
    assert df.empty
    # All RAW_COLUMNS present even when empty
    assert set(RAW_COLUMNS).issubset(set(df.columns))


@responses_lib.activate
@patch("src.ingestion.connectors.spotify.get_secret", side_effect=_secret_resolver)
@patch("src.ingestion.connectors.spotify.load_sources")
def test_no_auth_required_for_disabled_path(mock_load_sources, _mock_secret):
    """When disabled, the OAuth token endpoint is never hit (sanity)."""
    mock_load_sources.return_value = _sources(
        enabled=False, za_charts={"regional": ZA_REGIONAL_PLAYLIST}
    )
    # No mocked endpoints -> any HTTP call would 1) connection-fail or
    # 2) raise responses' ConnectionError. The fetch must return cleanly.
    df = _mk_connector("za").fetch()
    assert df.empty


@responses_lib.activate
@patch("src.ingestion.connectors.spotify.get_secret", return_value="")
@patch("src.ingestion.connectors.spotify.load_sources")
def test_missing_client_credentials_returns_empty(mock_load_sources, _mock_secret):
    """No SPOTIFY_CLIENT_ID means no fetch, even if enabled."""
    mock_load_sources.return_value = _sources(za_charts={"regional": ZA_REGIONAL_PLAYLIST})
    df = _mk_connector("za").fetch()
    assert df.empty


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.spotify.get_secret", side_effect=_secret_resolver)
@patch("src.ingestion.connectors.spotify.load_sources")
def test_fetches_top_50_per_market_per_chart(mock_load_sources, _mock_secret):
    """Happy path: ZA regional + ZA viral fetched, rows normalised."""
    mock_load_sources.return_value = _sources(
        za_charts={"regional": ZA_REGIONAL_PLAYLIST, "viral": ZA_VIRAL_PLAYLIST}
    )
    _mock_token(responses_lib)
    _mock_playlist(
        responses_lib,
        ZA_REGIONAL_PLAYLIST,
        [
            _track("t1", "Stimela", "Tyla"),
            _track("t2", "Mnike", "Tyler ICU"),
        ],
    )
    _mock_playlist(
        responses_lib,
        ZA_VIRAL_PLAYLIST,
        [_track("v1", "Imithandazo", "Kabza De Small")],
    )

    df = _mk_connector("za").fetch()

    assert len(df) == 3
    assert set(df["source"].unique()) == {"spotify"}
    assert set(df["platform"].unique()) == {"spotify"}
    assert set(df["market"].unique()) == {"za"}
    assert set(df["content_type"].unique()) == {"spotify_chart_track"}
    assert {"spotify_regional_top50", "spotify_viral_top50"} == set(df["query_group"].unique())


@responses_lib.activate
@patch("src.ingestion.connectors.spotify.get_secret", side_effect=_secret_resolver)
@patch("src.ingestion.connectors.spotify.load_sources")
def test_market_filter_honoured(mock_load_sources, _mock_secret):
    """Only the connector's market block is read; other markets ignored."""
    mock_load_sources.return_value = _sources(
        za_charts={"regional": ZA_REGIONAL_PLAYLIST},
        ng_charts={"regional": NG_REGIONAL_PLAYLIST},
    )
    _mock_token(responses_lib)
    # Only mock the NG playlist. If the ZA connector reads NG config it
    # would hit the unmocked ZA URL and 1) explode or 2) return nothing.
    _mock_playlist(responses_lib, NG_REGIONAL_PLAYLIST, [_track("n1", "Cast", "Shallipopi")])
    # Mock ZA so the ZA-targeted call succeeds for its own market.
    _mock_playlist(responses_lib, ZA_REGIONAL_PLAYLIST, [_track("t1", "Stimela", "Tyla")])

    df = _mk_connector("za").fetch()

    assert len(df) == 1
    assert df["market"].iloc[0] == "za"
    assert df["query_term"].iloc[0] == "Stimela"


@responses_lib.activate
@patch("src.ingestion.connectors.spotify.get_secret", side_effect=_secret_resolver)
@patch("src.ingestion.connectors.spotify.load_sources")
def test_csv_parsing_extracts_rank_track_artist_url(mock_load_sources, _mock_secret):
    """Rank, track_name, artist, url surface on the normalised row.

    Named for spec symmetry (`test_csv_parsing_...`); the live vendor
    surface is JSON not CSV, but the per-row extraction contract is the
    same: rank from item position, track_name from `track.name`, artist
    from joined `track.artists[].name`, url from `external_urls.spotify`.
    """
    mock_load_sources.return_value = _sources(za_charts={"regional": ZA_REGIONAL_PLAYLIST})
    _mock_token(responses_lib)
    _mock_playlist(
        responses_lib,
        ZA_REGIONAL_PLAYLIST,
        [
            _track("t1", "Stimela", "Tyla"),
            _track("t2", "Mnike", "Tyler ICU"),
            _track("t3", "Imithandazo", "Kabza De Small"),
        ],
    )

    df = _mk_connector("za").fetch()
    df = df.sort_values("text").reset_index(drop=True)

    # rank shows up in title sentence ("#1", "#2", "#3")
    texts = sorted(df["text"].tolist())
    assert any("#1" in t for t in texts)
    assert any("#2" in t for t in texts)
    assert any("#3" in t for t in texts)
    # artist + track_name on every row
    assert set(df["query_term"]) == {"Stimela", "Mnike", "Imithandazo"}
    assert "Tyla" in set(df["author_name"])
    # url shape
    assert df["url"].str.startswith("https://open.spotify.com/track/").all()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.spotify.get_secret", side_effect=_secret_resolver)
@patch("src.ingestion.connectors.spotify.load_sources")
def test_404_returns_empty(mock_load_sources, _mock_secret):
    """One 404 playlist does not abort the run; other charts still land."""
    mock_load_sources.return_value = _sources(
        za_charts={"regional": ZA_REGIONAL_PLAYLIST, "viral": ZA_VIRAL_PLAYLIST}
    )
    _mock_token(responses_lib)
    _mock_playlist(responses_lib, ZA_REGIONAL_PLAYLIST, [], status=404)
    _mock_playlist(responses_lib, ZA_VIRAL_PLAYLIST, [_track("v1", "Mnike", "Tyla")])

    df = _mk_connector("za").fetch()
    # Viral still landed even though regional 404'd
    assert len(df) == 1
    assert df["query_group"].iloc[0] == "spotify_viral_top50"


@patch("src.ingestion.connectors.spotify.get_secret", side_effect=_secret_resolver)
@patch("src.ingestion.connectors.spotify.load_sources")
def test_transient_failure_does_not_poison_playlist_cache(mock_load_sources, _mock_secret):
    """A transient (non-404) failure must not mark a playlist as fetched.

    The id is added to the process-wide cache only after a successful fetch, so
    a later fetch() in the same process retries the playlist instead of skipping.
    """
    import requests

    mock_load_sources.return_value = _sources(za_charts={"regional": ZA_REGIONAL_PLAYLIST})

    transient = requests.exceptions.RequestException("503 transient")
    transient.response = MagicMock(status_code=503)
    success_tracks = [{"track_id": "t1", "name": "Mnike", "artist": "Tyla", "rank": 1}]

    with (
        patch.object(
            SpotifyConnector,
            "_fetch_playlist_tracks",
            side_effect=[transient, success_tracks],
        ),
        patch.object(SpotifyConnector, "_get_access_token", return_value=FAKE_ACCESS_TOKEN),
        patch.object(
            SpotifyConnector,
            "_normalise_track",
            return_value=dict.fromkeys(RAW_COLUMNS, ""),
        ),
    ):
        first = _mk_connector("za").fetch()
        assert ZA_REGIONAL_PLAYLIST not in SpotifyConnector._fetched_playlists
        assert first.empty
        second = _mk_connector("za").fetch()

    assert len(second) == 1


@responses_lib.activate
@patch("src.ingestion.connectors.spotify.get_secret", side_effect=_secret_resolver)
@patch("src.ingestion.connectors.spotify.load_sources")
def test_normalise_handles_missing_streams_column(mock_load_sources, _mock_secret):
    """Web API does not return streams; views should fall back to 0."""
    mock_load_sources.return_value = _sources(za_charts={"regional": ZA_REGIONAL_PLAYLIST})
    _mock_token(responses_lib)
    _mock_playlist(responses_lib, ZA_REGIONAL_PLAYLIST, [_track("t1", "Stimela", "Tyla")])

    df = _mk_connector("za").fetch()
    assert df["views"].iloc[0] == 0.0
    assert df["likes"].iloc[0] == 0.0
    assert df["comments"].iloc[0] == 0.0
    assert df["shares"].iloc[0] == 0.0


def test_position_change_parsed_correctly():
    """Helper handles signed ints, the NEW sentinel, and zero."""
    fmt = SpotifyConnector._format_position_change
    assert fmt(5) == "+5"
    assert fmt(-3) == "-3"
    assert fmt(0) == "0"
    assert fmt("NEW") == "NEW"
    assert fmt("new") == "NEW"
    assert fmt("RE-ENTRY") == "NEW"
    assert fmt("+12") == "+12"
    assert fmt(None) == ""
    assert fmt("") == ""


def test_no_auth_required_for_charts_path():
    """Sanity: the Spotify token URL is the public OAuth endpoint and
    the playlist URL is under api.spotify.com. No customer-specific
    URLs leak into the constants. Guards against accidental refactor
    pointing the connector at a private vendor host.
    """
    assert SPOTIFY_TOKEN_URL == "https://accounts.spotify.com/api/token"
    assert SPOTIFY_API_BASE == "https://api.spotify.com/v1"
