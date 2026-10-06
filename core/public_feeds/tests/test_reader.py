from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from core.collect.ids import post_id
from core.public_feeds import reader
from core.public_feeds.catalog import FEEDS, confirmed_feeds


NOW = datetime(2026, 9, 30, 10, 30, tzinfo=timezone.utc)
NEWS_FEED = next(feed for feed in confirmed_feeds() if feed.kind == "news")
SAMPLES = Path(__file__).resolve().parent / "samples"
SAVED_NEWS_SAMPLES = (
    ("za_sabc_news", "za_sabc_news_20260930.html",
     "ActionSA calls for probe into eThekwini contract awarded to Moriel"),
    ("za_groundup", "za_groundup_20260930.html",
     "Woodstock land occupiers lose bid to appeal eviction"),
    ("za_enca", "za_enca_20260930.html",
     "GBV cases must move from reports to convictions, Commission says"),
    ("ng_channels_television", "ng_channels_tv_20260930.html",
     "NSCDC Deploys 3,000 Personnel Across Abuja For Independence Anniversary"),
    ("ng_punch", "ng_punch_20260930.html",
     "EPL: Everything Guardiola said about Man City’s 115 charges - Report"),
    ("ke_the_standard", "ke_the_standard_20260930.html",
     "Iran says received US response to its proposal to end war"),
)


class StubResponse:
    def __init__(self, status_code, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body
        self.closed = False
        self.bytes_yielded = 0

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            chunk = self.body[start:start + chunk_size]
            self.bytes_yielded += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


class StubSession:
    def __init__(self, response):
        self.trust_env = True
        self.response = response
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


def test_read_feed_rejects_a_caller_supplied_url_before_transport():
    feed = replace(NEWS_FEED, url="https://example.org/private")
    calls = []

    def transport(url, **kwargs):
        calls.append((url, kwargs))
        return 200, {"Content-Type": "text/html"}, b"<html></html>"

    with pytest.raises(reader.FeedReadError):
        reader.read_feed(feed, fetched_at=NOW, transport=transport)

    assert calls == []


def test_read_feed_rejects_an_unsupported_catalog_row_before_transport():
    feed = next(feed for feed in FEEDS if not feed.supported)
    calls = []

    with pytest.raises(reader.FeedReadError):
        reader.read_feed(feed, fetched_at=NOW, transport=lambda *args, **kwargs: calls.append(args))

    assert calls == []


def test_herald_and_citizen_digital_read_their_apex_hosts():
    # Their www hosts answer 301 to the apex since 3 October 2026; the reader and the robots.txt check follow
    # no redirect, so the catalog names the host that answers 200.
    urls = {feed.feed_id: feed.url for feed in confirmed_feeds()}
    assert urls["za_the_herald"] == "https://theherald.co.za/"
    assert urls["ke_citizen_digital"] == "https://citizen.digital/"


def test_parse_feed_rejects_empty_html():
    with pytest.raises(reader.FeedReadError):
        reader.parse_feed(NEWS_FEED, "", NOW)


@pytest.mark.parametrize("feed_id,sample_name,title", SAVED_NEWS_SAMPLES)
def test_saved_news_html_yields_visible_article_entries(feed_id, sample_name, title):
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == feed_id)
    html = (SAMPLES / sample_name).read_text(encoding="utf-8")

    entries = reader.parse_feed(feed, html, NOW)

    entry = next(entry for entry in entries if entry["text"] == title)
    assert entry["platform"] == "news"
    assert entry["url"].startswith("https://")
    assert entry["post_id"] is not None
    assert entry["source_market"] == feed.market
    assert entry["source_feed_id"] == feed.feed_id
    assert entry["source_url"] == feed.url
    assert entry["observed_at"] == "2026-09-30T10:30:00+00:00"
    assert entry["post_id"] == post_id("news", url=entry["url"])
    assert "rank" not in entry
    assert "views" not in entry
    assert not {"geo_market", "geo_confidence", "geo_source"} & entry.keys()


def test_news_published_at_uses_the_actual_sabc_time_and_timezone():
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "za_sabc_news")
    html = (SAMPLES / "za_sabc_news_20260930.html").read_text(encoding="utf-8")

    entry = next(
        entry for entry in reader.parse_feed(feed, html, NOW)
        if entry["text"] == "ActionSA calls for probe into eThekwini contract awarded to Moriel"
    )

    assert entry["published_at"] == "2026-09-30T12:16:00+02:00"


def test_news_date_only_label_does_not_become_a_midnight_timestamp():
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "za_enca")
    html = (SAMPLES / "za_enca_20260930.html").read_text(encoding="utf-8")

    entry = next(
        entry for entry in reader.parse_feed(feed, html, NOW)
        if entry["text"] == "GBV cases must move from reports to convictions, Commission says"
    )

    assert entry["published_at"] is None


def test_news_parser_drops_section_links_and_deduplicates_repeated_cards():
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "za_enca")
    html = (SAMPLES / "za_enca_20260930.html").read_text(encoding="utf-8")
    repeated_title = "GBV cases must move from reports to convictions, Commission says"

    titles = [entry["text"] for entry in reader.parse_feed(feed, html, NOW)]

    assert titles.count(repeated_title) == 1
    assert "Top News" not in titles
    assert "Latest News" not in titles


@pytest.mark.parametrize(
    "query",
    (
        "X-Amz-Signature=placeholder",
        "X-Goog-Credential=placeholder",
        "access_token=placeholder",
        "id=123",
    ),
    ids=["amazon-signature", "google-credential", "access-token", "benign-id"],
)
def test_news_parser_rejects_article_links_with_any_query(query):
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "za_groundup")
    html = (SAMPLES / "za_groundup_20260930.html").read_text(encoding="utf-8")
    html += f'<h2><a href="https://groundup.news/article/query-case/?{query}">Query Case</a></h2>'

    entries = reader.parse_feed(feed, html, NOW)

    assert any(entry["text"] == "Woodstock land occupiers lose bid to appeal eviction" for entry in entries)
    assert all(entry["text"] != "Query Case" for entry in entries)
    assert all("?" not in (entry["url"] or "") for entry in entries)
    assert all("placeholder" not in str(entry).casefold() for entry in entries)


def test_chart_parser_does_not_return_or_hash_a_query_bearing_track_link():
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "ng_turntable_top_100")
    html = (SAMPLES / "ng_turntable_ng_top_100_20260930.html").read_text(encoding="utf-8")
    html += (
        '<tr><td><p class="rank">99</p></td><td>'
        '<a href="https://www.turntablecharts.com/song/query-case?X-Amz-Signature=placeholder">'
        '<p class="title">Query Track</p><p class="artist">Sample Artist</p></a></td></tr>'
    )

    entry = next(entry for entry in reader.parse_feed(feed, html, NOW) if entry["text"] == "Query Track")

    assert entry["url"] is None
    assert entry["post_id"] is None
    assert "placeholder" not in str(entry).casefold()


def test_news_parser_rejects_userinfo_and_strips_fragment_from_article_identity():
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "za_groundup")
    html = (SAMPLES / "za_groundup_20260930.html").read_text(encoding="utf-8")
    html += '<h2><a href="https://user:placeholder@groundup.news/article/userinfo/">Userinfo Case</a></h2>'
    html += '<h2><a href="https://groundup.news/article/fragment/#section">Fragment Case</a></h2>'

    entries = reader.parse_feed(feed, html, NOW)
    entry = next(entry for entry in entries if entry["text"] == "Fragment Case")

    assert all(item["text"] != "Userinfo Case" for item in entries)
    assert entry["url"] == "https://groundup.news/article/fragment/"
    assert entry["post_id"] == post_id("news", url="https://groundup.news/article/fragment/")
    assert "placeholder" not in str(entries).casefold()


def test_chart_sample_keeps_source_rank_and_does_not_make_a_track_url():
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "ng_turntable_top_100")
    html = (SAMPLES / "ng_turntable_ng_top_100_20260930.html").read_text(encoding="utf-8")

    entry = next(entry for entry in reader.parse_feed(feed, html, NOW) if entry["text"] == "Volume")

    assert entry["kind"] == "chart"
    assert entry["artist"] == "Seyi Vibez"
    assert entry["item_key"] == "Seyi Vibez - Volume"
    assert entry["rank"] == 1
    assert entry["url"] is None
    assert entry["post_id"] is None
    assert entry["source_market"] == "NG"
    assert entry["source_feed_id"] == feed.feed_id
    assert "views" not in entry


def test_chart_sample_emits_only_its_eight_ranked_data_rows():
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "ng_turntable_top_100")
    html = (SAMPLES / "ng_turntable_ng_top_100_20260930.html").read_text(encoding="utf-8")

    entries = reader.parse_feed(feed, html, NOW)

    assert len(entries) == 8
    assert [entry["rank"] for entry in entries] == list(range(1, 9))
    assert "Entry" not in [entry["text"] for entry in entries]


def test_chart_parser_keeps_unranked_data_rows_with_visible_title_and_artist():
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "ng_turntable_top_100")
    html = (SAMPLES / "ng_turntable_ng_top_100_20260930.html").read_text(encoding="utf-8")
    html += '<tr><td></td><td><p class="title">Unranked Item</p><p class="artist">Source Artist</p></td></tr>'

    entry = next(entry for entry in reader.parse_feed(feed, html, NOW) if entry["text"] == "Unranked Item")

    assert entry["artist"] == "Source Artist"
    assert entry["item_key"] == "Source Artist - Unranked Item"
    assert "rank" not in entry


def test_mdundo_parser_scopes_native_kenya_chart_cards_and_keeps_printed_ranks():
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "ke_mdundo_top_songs")
    html = (SAMPLES / "ke_mdundo_native_20260930.html").read_text(encoding="utf-8")
    html += (SAMPLES / "ke_mdundo_new_releases_outgroup_20260930.html").read_text(encoding="utf-8")

    entries = reader.parse_feed(feed, html, NOW)

    assert len(entries) == 25
    assert [entry["rank"] for entry in entries] == list(range(1, 26))
    assert entries[0]["text"] == "ONLY YOU JESUS"
    assert entries[0]["artist"] == "Ada Ehi"
    assert entries[0]["item_key"] == "Ada Ehi - ONLY YOU JESUS"
    assert all(entry["kind"] == "chart" for entry in entries)
    assert all(entry["source_market"] == "KE" for entry in entries)
    assert all(entry["source_feed_id"] == feed.feed_id for entry in entries)
    assert all(entry["url"] is None and entry["post_id"] is None for entry in entries)
    assert not {"geo_market", "geo_confidence", "geo_source"} & set().union(*(entry.keys() for entry in entries))
    assert "My Darling" not in [entry["text"] for entry in entries]
    assert "Nivumilie. Rolex ft Mape Gold" not in [entry["text"] for entry in entries]


def test_mdundo_parser_ignores_hidden_card_text_and_keeps_visible_values():
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "ke_mdundo_top_songs")
    html = (SAMPLES / "ke_mdundo_native_20260930.html").read_text(encoding="utf-8")
    replacements = (
        (
            '<span class="md-playlist-song-item-number inline-block">1</span>',
            '<span class="md-playlist-song-item-number inline-block">1'
            '<script>PUBLIC_TEST_MARKER</script><span hidden>PUBLIC_TEST_MARKER</span></span>',
        ),
        (
            '<div class="md-player-song-active-color pt-1 px-1 h-[26px] overflow-hidden">'
            'ONLY YOU JESUS</div><div class="pb-1 px-1 text-xs text-slate-400 h-[20px] overflow-hidden">'
            'Ada Ehi</div>',
            '<div class="md-player-song-active-color pt-1 px-1 h-[26px] overflow-hidden">'
            'ONLY YOU JESUS<style>PUBLIC_TEST_MARKER</style>'
            '<span aria-hidden="true">PUBLIC_TEST_MARKER</span></div>'
            '<div class="pb-1 px-1 text-xs text-slate-400 h-[20px] overflow-hidden">'
            'Ada Ehi<span style="display: none">PUBLIC_TEST_MARKER</span>'
            '<span class="sr-only">PUBLIC_TEST_MARKER</span></div>',
        ),
    )
    for original, mutated in replacements:
        assert html.count(original) == 1
        html = html.replace(original, mutated, 1)

    entry = reader.parse_feed(feed, html, NOW)[0]

    assert entry["rank"] == 1
    assert entry["text"] == "ONLY YOU JESUS"
    assert entry["artist"] == "Ada Ehi"
    assert entry["item_key"] == "Ada Ehi - ONLY YOU JESUS"
    assert "PUBLIC_TEST_MARKER" not in str(entry)


def test_playlist_sample_preserves_track_and_relative_play_time_without_a_fake_post():
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "ke_kbc_english_playlist")
    html = (SAMPLES / "ke_kbc_english_service_playlist_20260930.html").read_text(encoding="utf-8")

    entries = reader.parse_feed(feed, html, NOW)
    entry = next(entry for entry in entries if entry["text"] == "Gotta Tell Someone")

    assert entry["kind"] == "playlist"
    assert entry["artist"] == "UB40"
    assert entry["item_key"] == "UB40 - Gotta Tell Someone"
    assert entry["time_text"] == "9 mins ago"
    assert entry["published_at"] is None
    assert entry["url"] is None
    assert entry["post_id"] is None
    assert "rank" not in entry
    assert sum(item["text"] == "Grazing In The Grass" for item in entries) == 1


def test_read_feed_returns_entries_from_the_injected_anonymous_response():
    html = (SAMPLES / "za_groundup_20260930.html").read_bytes()
    calls = []

    def transport(url, *, timeout, max_bytes):
        calls.append((url, timeout, max_bytes))
        return 200, {"Content-Type": "text/html; charset=utf-8"}, html

    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "za_groundup")
    entries = reader.read_feed(feed, fetched_at=NOW, transport=transport)

    assert calls == [(feed.url, 20, 2 * 1024 * 1024)]
    assert any(entry["text"] == "Woodstock land occupiers lose bid to appeal eviction" for entry in entries)


@pytest.mark.parametrize(
    ("status", "content_type", "body_kind"),
    [
        (403, "text/html", "small"),
        (200, "application/json", "small"),
        (200, "text/html", "empty"),
        (200, "text/html", "oversize"),
    ],
    ids=["blocked", "non-html", "empty", "oversize"],
)
def test_read_feed_rejects_blocked_non_html_empty_and_oversize_responses(status, content_type, body_kind):
    calls = []
    body = {"small": b"blocked", "empty": b"", "oversize": b"x" * (2 * 1024 * 1024 + 1)}[body_kind]

    def transport(url, *, timeout, max_bytes):
        calls.append((url, timeout, max_bytes))
        return status, {"Content-Type": content_type}, body

    with pytest.raises(reader.FeedReadError):
        reader.read_feed(NEWS_FEED, fetched_at=NOW, transport=transport)

    assert calls == [(NEWS_FEED.url, 20, 2 * 1024 * 1024)]


def test_default_transport_disables_environment_proxies_and_does_not_follow_redirects(monkeypatch):
    response = StubResponse(302, {"Location": "http://127.0.0.1/admin"})
    session = StubSession(response)
    monkeypatch.setattr(requests, "Session", lambda: session)

    with pytest.raises(reader.FeedReadError):
        reader.read_feed(NEWS_FEED, fetched_at=NOW)

    assert session.trust_env is False
    assert session.closed
    assert response.closed
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == NEWS_FEED.url
    assert kwargs["timeout"] == 20
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert set(kwargs["headers"]) == {"User-Agent"}


def test_default_transport_stops_before_buffering_more_than_the_body_limit(monkeypatch):
    response = StubResponse(200, {"Content-Type": "text/html"}, b"x" * (2 * 1024 * 1024 + 1))
    session = StubSession(response)
    monkeypatch.setattr(requests, "Session", lambda: session)

    with pytest.raises(reader.FeedReadError):
        reader.read_feed(NEWS_FEED, fetched_at=NOW)

    assert session.closed
    assert response.closed
    assert response.bytes_yielded == 2 * 1024 * 1024 + 1


def test_mdundo_parser_reads_the_chart_from_the_page_as_served():
    # The live page (play.mdundo.com/top-charts/ke, read 2 Oct 2026) is a Splade app: the chart markup is a
    # JSON string in div#app data-html, so the page's own elements hold no song rows. This sample keeps the
    # served shape with the first three real rows.
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "ke_mdundo_top_songs")
    html = (SAMPLES / "ke_mdundo_wire_20261002.html").read_text(encoding="utf-8")

    entries = reader.parse_feed(feed, html, NOW)

    assert [(entry["rank"], entry["text"], entry["artist"]) for entry in entries] == [
        (1, "ONLY YOU JESUS", "Ada Ehi"), (2, "Rapudo", "Prince Indah"), (3, "Rembo", "Prince Indah")]
    assert all(entry["source_market"] == "KE" and entry["kind"] == "chart" for entry in entries)
