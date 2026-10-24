"""Base connector with retry logic, rate limiting, and structured logging."""

import time
from abc import ABC, abstractmethod
from urllib.parse import urljoin, urlsplit

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.utils.log_redactor import get_logger

# A feed may move (http to https, a new path), so its reader follows a few redirects, but only on the same host and
# never from https down to http: another host was never checked, and a feed host must not be able to point us at
# another origin or an internal address.
MAX_FEED_REDIRECTS = 5


class RedirectRefused(requests.exceptions.RequestException):
    """A redirect a reader will not follow: any redirect while carrying a key, or a feed redirect off its host."""


def refuse_redirect(response) -> None:
    """Raise RedirectRefused when response is a redirect. For keyed calls made with allow_redirects=False: requests
    strips Authorization on a cross-host redirect but copies any other header, so an x-api-key would go to wherever
    the Location points."""
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool) and 300 <= status < 400:
        close = getattr(response, "close", None)
        if callable(close):
            close()
        raise RedirectRefused(f"redirect refused (HTTP {status})")


def same_site_redirect(source: str, target: str) -> bool:
    """Whether a redirect from source to target stays on the same host and is not a downgrade from https to http."""
    try:
        before, after = urlsplit(source), urlsplit(target)
        same_host = (after.hostname or "").rstrip(".").lower() == (before.hostname or "").rstrip(".").lower()
        same_port = after.port == before.port or (after.port is None and before.port is None)
    except ValueError:
        return False
    if not after.hostname or after.scheme not in ("http", "https"):
        return False
    if before.scheme == "https" and after.scheme != "https":
        return False
    port_ok = same_port or (before.scheme == "http" and after.scheme == "https" and after.port is None)
    return same_host and port_ok


class FeedTooLarge(requests.exceptions.RequestException):
    """A feed body over MAX_FEED_BYTES, refused before it is parsed."""


# A feed's body is read up to this many bytes; a larger one is a failed feed, never parsed (audit F7D-COL-003).
MAX_FEED_BYTES = 4 * 1024 * 1024


def _bounded(response, max_bytes: int):
    """response with its body read into it, or FeedTooLarge when the body is over max_bytes."""
    declared = str(response.headers.get("content-length", ""))
    if declared.isdigit() and int(declared) > max_bytes:
        response.close()
        raise FeedTooLarge(f"feed body of {declared} bytes is over the {max_bytes} byte limit")
    body = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk or b"")
            if len(body) > max_bytes:
                raise FeedTooLarge(f"feed body is over the {max_bytes} byte limit")
    finally:
        response.close()
    # requests keeps a read body in _content; content and text then read it as for an unstreamed response.
    response._content = bytes(body)
    return response


def get_feed(session, url: str, *, max_bytes: int = MAX_FEED_BYTES, **kwargs):
    """GET an unauthenticated feed, following at most MAX_FEED_REDIRECTS redirects and only on the same host without
    a downgrade (same_site_redirect), with its body bounded by max_bytes. Raises RedirectRefused or FeedTooLarge
    otherwise."""
    for _ in range(MAX_FEED_REDIRECTS + 1):
        response = session.get(url, allow_redirects=False, stream=True, **kwargs)
        if getattr(response, "is_redirect", False) is not True:
            # Only a real requests response streams; a stand-in session's response is returned as it came.
            return _bounded(response, max_bytes) if isinstance(response, requests.Response) else response
        target = urljoin(url, response.headers.get("location", ""))
        response.close()
        if not same_site_redirect(url, target):
            raise RedirectRefused(f"redirect off the feed's host refused: {urlsplit(url).hostname} to "
                                  f"{urlsplit(target).hostname or target[:80]}")
        url = target
    raise RedirectRefused(f"more than {MAX_FEED_REDIRECTS} redirects")


RAW_COLUMNS: list[str] = [
    "source",
    "platform",
    "market",
    "content_type",
    "query_group",
    "query_term",
    "author_name",
    "author_handle",
    "title",
    "text",
    "url",
    "published_at",
    "views",
    "likes",
    "comments",
    "shares",
    # GDELT GKG 2.0 fields. Empty string for non-GDELT connectors.
    "v2tone",
    "v2persons",
    "v2orgs",
    "v2locations",
    # GCAM emotional-cognitive dimensions (Wave 0.2). Holds the raw GCAM
    # "code:value" string when gdelt.gcam_enabled is true; empty otherwise.
    # Dark on ship: the column carries the load schema so the value persists
    # once the flag flips. Empty string for non-GDELT connectors.
    "v2gcam",
]

ENRICHED_COLUMNS: list[str] = [
    *RAW_COLUMNS,
    "slang_score",
    "slang_terms",
    "regional_score",
    "genz_score",
    "creator_watchlist_tier",
    "creator_watchlist_score",
    "search_velocity_score",
    "pipeline_run_id",
    # Step 4: GDELT V2Tone parsed into normalised floats. NaN for non-GDELT rows.
    "tone_avg",
    "tone_polarity",
    # Topic clustering refactor: per-row list of topic_group names matched
    # via keyword classifier. Empty list means unclassified.
    "topic_groups",
]


class BaseConnector(ABC):
    """Abstract base for all data source connectors."""

    # Override in subclasses
    SOURCE_NAME: str = "unknown"
    PLATFORM: str = "unknown"
    MAX_RETRIES: int = 3
    BACKOFF_FACTOR: float = 1.5
    RATE_LIMIT_DELAY: float = 0.5  # seconds between requests
    RETRY_STATUS_CODES: tuple = (429, 500, 502, 503, 504)
    # (connect, read) seconds. A vendor hang costs one read timeout, not a
    # retried multiple of it (read retries are disabled in _build_session).
    REQUEST_TIMEOUT: tuple = (10, 30)

    def __init__(self, market: str = ""):
        self.market = market
        self.logger = get_logger(f"connector.{self.SOURCE_NAME}")
        self._session = self._build_session()
        self._request_count = 0
        # Per-fetch failure strings surfaced to pipeline_runs.errors via run_rss_now.
        self._fetch_failures: list[str] = []

    def _build_session(self) -> requests.Session:
        """Build a requests session with retry logic."""
        session = requests.Session()
        retry_strategy = Retry(
            total=self.MAX_RETRIES,
            # A read timeout means the vendor accepted the request and hung;
            # retrying it burns 4x the wall clock (the 13/14 Jul dead night).
            # Fail fast and let the caller's error ladder degrade instead.
            read=0,
            connect=2,
            backoff_factor=self.BACKOFF_FACTOR,
            status_forcelist=self.RETRY_STATUS_CODES,
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Make an HTTP request with rate limiting and logging."""
        # Rate limiting
        if self._request_count > 0:
            time.sleep(self.RATE_LIMIT_DELAY)

        self._request_count += 1
        self.logger.info(
            "Request #%d: %s %s",
            self._request_count,
            method.upper(),
            url.split("?")[0],  # Log URL without query params (may contain tokens)
        )

        try:
            resp = self._session.request(method, url, timeout=self.REQUEST_TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            # Redact URL in error message (may contain API tokens)
            safe_url = url.split("?")[0]
            self.logger.error(
                "HTTP %s for %s: %s",
                getattr(e.response, "status_code", "???"),
                safe_url,
                str(e)[:200],
            )
            raise
        except requests.exceptions.RequestException as e:
            self.logger.error("Request failed for %s: %s", url.split("?")[0], str(e)[:200])
            raise

    @abstractmethod
    def fetch(self, **kwargs) -> pd.DataFrame:
        """Fetch data and return a normalized DataFrame.

        Must return columns:
            source, platform, market, content_type, query_group, query_term,
            author_name, author_handle, title, text, url,
            published_at, views, likes, comments, shares
        """
        ...

    @staticmethod
    def empty_dataframe() -> pd.DataFrame:
        """Return an empty DataFrame with the standard schema."""
        return pd.DataFrame(columns=list(RAW_COLUMNS))

    def safe_fetch(self, **kwargs) -> pd.DataFrame:
        """Fetch with error handling ,  returns empty DataFrame on failure."""
        self._fetch_failures = []
        try:
            df = self.fetch(**kwargs)
            self.logger.info(
                "Fetched %d rows from %s (market=%s)",
                len(df),
                self.SOURCE_NAME,
                self.market,
            )
            return df
        except Exception as e:
            msg = f"{self.SOURCE_NAME}: {str(e)[:300]}"
            self._fetch_failures.append(msg)
            self.logger.error(
                "Failed to fetch from %s (market=%s): %s",
                self.SOURCE_NAME,
                self.market,
                str(e)[:300],
            )
            return self.empty_dataframe()
