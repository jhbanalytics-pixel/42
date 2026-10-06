"""Unit tests for RSSConnector."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import responses as responses_lib
from src.ingestion.connectors.base import RAW_COLUMNS
from src.ingestion.connectors.rss import RSSConnector


def _feedparser_entry(
    title="Headline",
    link="https://example.com/1",
    summary="Body text",
    author="Reporter",
    published="Mon, 14 Apr 2026 10:00:00 +0200",
):
    """Return a MagicMock feedparser entry with .get() support."""
    entry = MagicMock()
    entry.get.side_effect = lambda key, default="": {
        "title": title,
        "link": link,
        "summary": summary,
        "author": author,
        "published": published,
    }.get(key, default)
    return entry


def _parsed_feed(entries, bozo=False):
    """Build a fake feedparser result."""
    result = MagicMock()
    result.bozo = bozo
    result.bozo_exception = None
    result.entries = entries
    return result


SABC_URL = "https://sabc.co.za/feed/"
ALLAFRICA_URL = "https://allafrica.com/feed/"

SOURCES_ZA = {
    "rss_feeds": {
        "za": [
            {"url": SABC_URL, "name": "SABC News"},
            {"url": ALLAFRICA_URL, "name": "AllAfrica"},
        ],
    }
}

SOURCES_EMPTY = {"rss_feeds": {}}


def make_connector(market="za"):
    return RSSConnector(market=market)


def _mock_http_ok(url: str, body: bytes = b"<rss></rss>"):
    """Register a 200 response for a feed URL so _session.get() succeeds."""
    responses_lib.add(responses_lib.GET, url, body=body, status=200)


SOURCES_ZA_ONE_MISSING_URL = {
    "rss_feeds": {
        "za": [
            {"name": "Malformed entry, no url"},
            {"url": ALLAFRICA_URL, "name": "AllAfrica"},
        ],
    }
}


@responses_lib.activate
@patch("src.ingestion.connectors.rss.load_sources", return_value=SOURCES_ZA_ONE_MISSING_URL)
@patch("src.ingestion.connectors.rss.feedparser.parse")
def test_fetch_skips_entry_missing_url_keeps_market(mock_parse, _mock_sources):
    """A config entry without a url is skipped, the rest of the market still ingests."""
    _mock_http_ok(ALLAFRICA_URL)
    mock_parse.return_value = _parsed_feed(
        [_feedparser_entry(title="Africa Story", link="https://allafrica.com/1")]
    )

    connector = make_connector(market="za")
    connector.RATE_LIMIT_DELAY = 0
    df = connector.fetch()

    assert len(df) == 1
    assert df.iloc[0]["title"] == "Africa Story"


@responses_lib.activate
@patch("src.ingestion.connectors.rss.load_sources", return_value=SOURCES_ZA)
@patch("src.ingestion.connectors.rss.feedparser.parse")
def test_fetch_returns_articles_from_configured_feeds(mock_parse, _mock_sources):
    """fetch() returns one row per entry across all configured feeds for the market."""
    _mock_http_ok(SABC_URL)
    _mock_http_ok(ALLAFRICA_URL)

    entry1 = _feedparser_entry(title="ZA Story", link="https://sabc.co.za/1")
    entry2 = _feedparser_entry(title="Africa Story", link="https://allafrica.com/1")
    mock_parse.side_effect = [
        _parsed_feed([entry1]),
        _parsed_feed([entry2]),
    ]

    connector = make_connector(market="za")
    connector.RATE_LIMIT_DELAY = 0
    df = connector.fetch()

    assert len(df) == 2
    assert set(df["title"]) == {"ZA Story", "Africa Story"}
    assert list(df.columns) == list(RAW_COLUMNS)


@patch("src.ingestion.connectors.rss.load_sources", return_value=SOURCES_EMPTY)
def test_fetch_returns_empty_dataframe_when_no_feeds_configured(_mock_sources):
    """fetch() returns an empty DataFrame on the full RAW_COLUMNS schema when no feeds are in the config."""
    connector = make_connector(market="ng")
    df = connector.fetch()

    assert len(df) == 0
    assert "source" in df.columns
    assert len(df.columns) == len(RAW_COLUMNS)


@responses_lib.activate
@patch("src.ingestion.connectors.rss.load_sources", return_value=SOURCES_ZA)
@patch("src.ingestion.connectors.rss.feedparser.parse")
def test_fetch_skips_bozo_feed_with_no_entries(mock_parse, _mock_sources):
    """fetch() skips malformed feeds that have bozo=True and no entries."""
    _mock_http_ok(SABC_URL)
    _mock_http_ok(ALLAFRICA_URL)

    mock_parse.side_effect = [
        _parsed_feed([], bozo=True),
        _parsed_feed([_feedparser_entry(link="https://allafrica.com/1")]),
    ]

    connector = make_connector(market="za")
    connector.RATE_LIMIT_DELAY = 0
    df = connector.fetch()

    assert len(df) == 1
    assert df.iloc[0]["url"] == "https://allafrica.com/1"


@responses_lib.activate
@patch("src.ingestion.connectors.rss.load_sources", return_value=SOURCES_ZA)
@patch("src.ingestion.connectors.rss.feedparser.parse")
def test_fetch_assigns_market_from_connector_not_feed(mock_parse, _mock_sources):
    """market column comes from the connector, not from the feed content."""
    # Register HTTP responses for both feeds, for each of the three market runs.
    for _ in range(3):
        _mock_http_ok(SABC_URL)
        _mock_http_ok(ALLAFRICA_URL)

    mock_parse.return_value = _parsed_feed([_feedparser_entry(link="https://sabc.co.za/1")])

    for market in ("za", "ng", "ke"):
        connector = RSSConnector(market=market)
        connector.RATE_LIMIT_DELAY = 0
        df = connector.fetch()
        if market == "za":
            assert (df["market"] == market).all(), f"Expected market={market}"
        else:
            # ng and ke are not in SOURCES_ZA, so they produce empty frames.
            assert df.empty


@responses_lib.activate
@patch("src.ingestion.connectors.rss.load_sources", return_value=SOURCES_ZA)
@patch("src.ingestion.connectors.rss.feedparser.parse")
def test_fetch_deduplicates_on_url(mock_parse, _mock_sources):
    """fetch() drops duplicate URLs that appear in multiple feeds for the market."""
    _mock_http_ok(SABC_URL)
    _mock_http_ok(ALLAFRICA_URL)

    shared_url = "https://shared.example.com/article"
    entry = _feedparser_entry(link=shared_url)

    mock_parse.side_effect = [
        _parsed_feed([entry]),
        _parsed_feed([entry]),
    ]

    connector = make_connector(market="za")
    connector.RATE_LIMIT_DELAY = 0
    df = connector.fetch()

    assert len(df) == 1
    assert df.iloc[0]["url"] == shared_url


@responses_lib.activate
@patch(
    "src.ingestion.connectors.rss.load_sources",
    return_value={"rss_feeds": {"za": [{"url": SABC_URL, "name": "Test"}]}},
)
def test_rss_connector_parses_real_xml_via_fixture(_mock_sources, sample_rss_feed_xml):
    """Pass fixture XML through the real feedparser. No parse mocking."""
    # The connector fetches feed content via its session, then parses it for real.
    responses_lib.add(
        responses_lib.GET,
        SABC_URL,
        body=sample_rss_feed_xml.encode("utf-8"),
        status=200,
        content_type="application/rss+xml",
    )

    connector = make_connector(market="za")
    connector.RATE_LIMIT_DELAY = 0
    df = connector.fetch()

    assert len(df) == 2
    assert list(df.columns) == list(RAW_COLUMNS)
    assert set(df["title"]) == {
        "AI trends in South Africa",
        "Tech news from Nigeria",
    }
    row_with_tz = df[df["title"] == "AI trends in South Africa"].iloc[0]
    assert isinstance(row_with_tz["published_at"], datetime)
    assert row_with_tz["published_at"].tzinfo is not None
    assert row_with_tz["published_at"].utcoffset() == UTC.utcoffset(datetime.now())
    assert row_with_tz["author_name"] == "test.author@example.com"
    assert (df["market"] == "za").all()
    assert (df["platform"] == "web").all()


# ---------------------------------------------------------------------------
# Category + author extraction (added 27 May 2026 for Phase 1 utilization)
# ---------------------------------------------------------------------------


def test_extract_categories_pulls_term_field():
    """RSS / Atom <category term="..."/> labels surface as deduped strings."""

    class Tag:
        def __init__(self, term):
            self.term = term

    entry = {"tags": [Tag("Amapiano"), Tag("music"), Tag("amapiano")]}
    terms = RSSConnector._extract_categories(entry)
    # Order preserved, case-insensitive dedup keeps the first-seen casing.
    assert terms == ["Amapiano", "music"]


def test_extract_categories_handles_dict_tag_shape():
    """feedparser sometimes returns FeedParserDict objects already keyed by .get."""
    entry = {"tags": [{"term": "Afrobeats"}, {"label": "Nigeria"}, {"term": ""}]}
    terms = RSSConnector._extract_categories(entry)
    assert terms == ["Afrobeats", "Nigeria"]


def test_resolve_author_falls_through_to_dc_creator():
    """When entry.author is empty, dc:creator wins (TechCentral, Premium Times)."""
    entry = {"author": "", "dc_creator": "Joanna Smith"}
    assert RSSConnector._resolve_author(entry) == "Joanna Smith"


def test_resolve_author_prefers_explicit_author():
    """entry.author beats every fallback."""
    entry = {
        "author": "Real Byline",
        "dc_creator": "DC Creator",
        "author_detail": {"name": "Atom Author"},
    }
    assert RSSConnector._resolve_author(entry) == "Real Byline"


@responses_lib.activate
@patch(
    "src.ingestion.connectors.rss.load_sources",
    return_value={
        "rss_feeds": {
            "za": [{"url": "https://feed.example.com/rss", "name": "FeedX"}],
        }
    },
)
@patch("src.ingestion.connectors.rss.feedparser.parse")
def test_fetch_appends_category_terms_to_text(mock_parse, _mock_sources):
    """Category labels join the summary so the topic classifier sees them downstream."""
    responses_lib.add(responses_lib.GET, "https://feed.example.com/rss", body=b"<rss/>", status=200)

    entry = MagicMock()
    entry.get.side_effect = lambda key, default="": {
        "title": "Tyla drops new amapiano single",
        "link": "https://feed.example.com/post/1",
        "summary": "She has been recording in Joburg for months.",
        "author": "Sipho Ndlovu",
        "published": "Mon, 26 May 2026 09:00:00 +0200",
        "tags": [{"term": "amapiano"}, {"term": "Tyla"}],
    }.get(key, default)
    mock_parse.side_effect = [_parsed_feed([entry])]

    connector = RSSConnector(market="za")
    connector.RATE_LIMIT_DELAY = 0
    df = connector.fetch()

    assert len(df) == 1
    text = df.iloc[0]["text"]
    assert "She has been recording" in text
    assert "amapiano" in text
    assert "Tyla" in text
    assert df.iloc[0]["author_name"] == "Sipho Ndlovu"


# ----------------------------------------------------------------------------
# User-Agent header tests (Wave 2: unblock Cloudflare-fronted publisher feeds).
# Some publishers (News24, Premium Times, IOL, Guardian Nigeria) reject the
# default feedparser User-Agent with HTTP 403. A polite-bot UA that declares
# the project and a contact URL recovers most of them while staying within
# bot netiquette (no browser impersonation).
# ----------------------------------------------------------------------------


@responses_lib.activate
@patch("src.ingestion.connectors.rss.load_sources", return_value=SOURCES_ZA)
@patch("src.ingestion.connectors.rss.feedparser.parse")
def test_fetch_sends_user_agent_header(mock_parse, _mock_sources):
    """fetch() sends the polite-bot User-Agent on every feed request."""
    _mock_http_ok(SABC_URL)
    _mock_http_ok(ALLAFRICA_URL)
    mock_parse.return_value = _parsed_feed([_feedparser_entry(link="https://sabc.co.za/1")])

    connector = make_connector(market="za")
    connector.RATE_LIMIT_DELAY = 0
    connector.fetch()

    # Every recorded HTTP call should carry the project User-Agent header.
    assert len(responses_lib.calls) == 2
    for call in responses_lib.calls:
        ua = call.request.headers.get("User-Agent", "")
        assert ua == RSSConnector.USER_AGENT, f"missing/wrong UA on {call.request.url}: {ua!r}"

    # feedparser.parse should also receive the agent kwarg so the contract
    # is self-consistent if a future caller passes a URL instead of bytes.
    assert mock_parse.called
    for _, kwargs in mock_parse.call_args_list:
        assert kwargs.get("agent") == RSSConnector.USER_AGENT


def test_user_agent_includes_project_identifier():
    """USER_AGENT string identifies the project and points to the source repo."""
    ua = RSSConnector.USER_AGENT
    assert "OgilvyTrendsEngineV2" in ua, ua
    # A traceable contact URL so publishers can reach us / block us cleanly.
    assert "https://github.com/jhbanalytics-pixel/trends-engine-v2" in ua, ua


def test_user_agent_does_not_pretend_to_be_real_browser():
    """USER_AGENT discloses bot status per RFC 9110 conventions.

    The Mozilla/5.0 prefix is conventional and accepted, but the parenthetical
    MUST start with 'compatible;' and name the bot so it is not confused with
    a real Chrome / Firefox / Safari UA. We also forbid embedding signature
    tokens of real browsers (Chrome/, Firefox/, Safari/, Edg/) that publishers
    use to gate human-only access.
    """
    ua = RSSConnector.USER_AGENT
    assert "compatible;" in ua, f"UA must declare 'compatible;' bot status: {ua!r}"
    assert "OgilvyTrendsEngineV2" in ua

    forbidden_tokens = ("Chrome/", "Firefox/", "Safari/", "Edg/", "AppleWebKit/")
    for token in forbidden_tokens:
        assert token not in ua, f"UA must not impersonate a real browser ({token}): {ua!r}"
