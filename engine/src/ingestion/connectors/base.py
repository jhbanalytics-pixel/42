"""Base connector with retry logic, rate limiting, and structured logging."""

import time
from abc import ABC, abstractmethod

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.utils.log_redactor import get_logger

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
