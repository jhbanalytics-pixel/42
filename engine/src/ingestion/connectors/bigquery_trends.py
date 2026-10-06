"""Google Trends connector via the BigQuery public dataset.

Queries two sibling tables for complementary search signal:

1. bigquery-public-data.google_trends.international_top_rising_terms — spiking
   terms (content_type="search_term", query_group="search_intent"). Always on.
2. bigquery-public-data.google_trends.international_top_terms — steady-state
   top-chart terms (content_type="top_term", query_group="google_trends_top").
   Off by default per-market; flip via sources.yaml bigquery_trends.top_terms.

Engagement fields are zero (search terms have no likes/views/comments/shares).

The percent_gain field on the rising-terms feed is normalised into
search_velocity_score upstream in infra/bigquery_queries/search_velocity_terms.sql.
The top_terms feed exposes `rank` and `score` from the public dataset; the
score is preserved in the title text and is not stored as a separate column
(the 16-column raw contract has no slot for it).

Cost note: both sibling tables are DAY-partitioned on `refresh_date`. Queries
filter on `refresh_date` (not `week`) so partition pruning fires. Each call
scans ~2 GB at days=7. Three markets daily on top_terms = ~6 GB/day = ~$0.03/day
at the on-demand $5/TB rate (~$0.90/month).
"""

import hashlib
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd
from google.api_core.exceptions import BadRequest, NotFound

from src.ingestion.connectors.base import BaseConnector
from src.ingestion.enrichment import _search_velocity_on
from src.utils import bigquery as bq_utils
from src.utils.config_loader import load_sources

_SQL_PATH = (
    Path(__file__).resolve().parents[3] / "infra" / "bigquery_queries" / "search_velocity_terms.sql"
)
_TOP_TERMS_SQL_PATH = (
    Path(__file__).resolve().parents[3] / "infra" / "bigquery_queries" / "search_top_terms.sql"
)


class BigQueryTrendsConnector(BaseConnector):
    """Fetches rising search terms from the Google Trends BigQuery public dataset."""

    SOURCE_NAME = "bigquery_trends"
    PLATFORM = "google_search"

    # ISO-3166 alpha-2 codes as used by the public dataset.
    COUNTRY_CODE_MAP: ClassVar[dict[str, str]] = {"za": "ZA", "ng": "NG", "ke": "KE"}

    # Which markets are expected to return non-zero rows from the public dataset.
    # KE is intentionally False: bigquery-public-data.google_trends has zero KE rows all-time
    # (verified 28 May 2026; only KR present in K-prefix codes). If a True market returns 0,
    # we ERROR — that signals upstream dataset deprecation or a connector regression.
    # Override per-market via sources.yaml bigquery_trends.expected_per_market.{za,ng,ke}: bool.
    DEFAULT_EXPECTED_PER_MARKET: ClassVar[dict[str, bool]] = {"za": True, "ng": True, "ke": False}

    # Mirror of the above for the top_terms feed. Verified 28 May 2026 via live
    # probe against bigquery-public-data.google_trends.international_top_terms:
    # ZA + NG each return 25 rows over 7 days, KE returns 0 (no upstream coverage).
    # Override per-market via sources.yaml bigquery_trends.top_terms.expected_per_market.
    DEFAULT_EXPECTED_PER_MARKET_TOP_TERMS: ClassVar[dict[str, bool]] = {
        "za": True,
        "ng": True,
        "ke": False,
    }

    DEFAULT_DAYS = 7
    DEFAULT_LIMIT = 100
    DEFAULT_TOP_TERMS_LIMIT = 25

    def fetch(self, **kwargs: Any) -> pd.DataFrame:
        """Fetch rising + (optionally) top search terms for this connector's market.

        Reads SQL from infra/bigquery_queries/search_velocity_terms.sql
        (rising terms, always on) and infra/bigquery_queries/search_top_terms.sql
        (steady-state top terms, off by default, gated by
        bigquery_trends.top_terms.enabled). Binds @country_code, @days, and the
        limit parameter from the sources.yaml bigquery_trends block (or class
        defaults).

        Returns an empty 16-column DataFrame when the config is inactive,
        when the market has no country code mapping, or when both feeds fail.
        Returns just one feed's rows when the other is empty or disabled.
        """
        config = load_sources().get("bigquery_trends", {}) or {}

        if config.get("active", True) is False:
            self.logger.info(
                "bigquery_trends inactive in sources.yaml, skipping (market=%s)",
                self.market,
            )
            return self.empty_dataframe()

        country_code = self.COUNTRY_CODE_MAP.get(self.market.lower())
        if not country_code:
            self.logger.warning("No country code mapping for market=%s, skipping", self.market)
            return self.empty_dataframe()

        rising_df = self._fetch_rising_terms(config, country_code)
        top_df = self._fetch_top_terms(config, country_code)

        if rising_df.empty and top_df.empty:
            return self.empty_dataframe()
        if top_df.empty:
            return rising_df
        if rising_df.empty:
            return top_df
        return pd.concat([rising_df, top_df], ignore_index=True)

    def _fetch_rising_terms(self, config: dict[str, Any], country_code: str) -> pd.DataFrame:
        """Fetch rising/spiking search terms (the original always-on feed)."""
        days = int(config.get("days", self.DEFAULT_DAYS))
        limit = int(config.get("limit", self.DEFAULT_LIMIT))

        try:
            sql = _SQL_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.logger.error("SQL file missing at %s", _SQL_PATH)
            return self.empty_dataframe()

        params = {
            "country_code": country_code,
            "days": days,
            "limit": limit,
        }

        try:
            results = bq_utils.run_query(sql, params=params)
        except (BadRequest, NotFound) as e:
            self.logger.error(
                "BigQuery query failed for market=%s: %s",
                self.market,
                str(e)[:300],
            )
            return self.empty_dataframe()
        except Exception as e:
            self.logger.error(
                "Unexpected error running BigQuery Trends query for market=%s: %s",
                self.market,
                str(e)[:300],
            )
            return self.empty_dataframe()

        if results is None or results.empty:
            expected_map = config.get("expected_per_market") or {}
            market_key = self.market.lower()
            if market_key in expected_map:
                expected = bool(expected_map[market_key])
            else:
                expected = self.DEFAULT_EXPECTED_PER_MARKET.get(market_key, True)
            if expected:
                # A market we expect to receive rows for returned zero. Loud alert —
                # either the public dataset stopped publishing this country, the SQL
                # broke, or the country_code mapping drifted. Surface at ERROR so the
                # pipeline-health alerting hook picks it up.
                self.logger.error(
                    "bigquery_trends returned 0 rows for market=%s (country_code=%s) "
                    "but rows were expected. Check upstream dataset coverage and SQL.",
                    self.market,
                    country_code,
                )
            else:
                # Known expected-zero market (currently KE). Log at INFO so the
                # daily summary is honest about coverage without raising an alert.
                self.logger.info(
                    "bigquery_trends returned 0 rows for market=%s (country_code=%s); "
                    "expected zero per public dataset coverage.",
                    self.market,
                    country_code,
                )
            return self.empty_dataframe()

        rows = [self._normalise_row(row) for row in results.to_dict(orient="records")]
        # search_velocity_score is not in RAW_COLUMNS, so the standard reindex
        # below drops it. Carry it through only when the revive flag is on
        # (dark by default); when off, behave exactly as before so the column
        # never appears and the live cron is byte-identical.
        columns = self.empty_dataframe().columns.tolist()
        if _search_velocity_on():
            columns = [*columns, "search_velocity_score"]
        df = pd.DataFrame(rows, columns=columns)
        df = df.drop_duplicates(subset=["url", "query_term", "published_at"])
        # _normalise_row omits the four GDELT v2 fields, so the forced columns
        # arrive as NaN; run_rss_now._s would stringify NaN to the literal
        # "nan" in raw_content. Empty-string them to honour the "empty string
        # elsewhere" contract for non-GDELT rows.
        for _col in ("v2tone", "v2persons", "v2orgs", "v2locations", "v2gcam"):
            df[_col] = df[_col].fillna("")
        self.logger.info(
            "bigquery_trends fetched %d rows for market=%s (country_code=%s)",
            len(df),
            self.market,
            country_code,
        )
        return df

    def _fetch_top_terms(self, config: dict[str, Any], country_code: str) -> pd.DataFrame:
        """Fetch steady-state top search terms from international_top_terms.

        Disabled by default. Activate per market via:

            bigquery_trends:
              top_terms:
                enabled: true            # global on/off (default false)
                days: 7                  # rolling window
                limit: 25                # rows per market per run
                expected_per_market:     # optional overrides
                  za: true
                  ng: true
                  ke: false              # KE has zero coverage in the public dataset

        Returns an empty 16-column DataFrame when disabled, when the query
        fails, or when the public dataset has no rows for the market.
        """
        top_cfg = (config.get("top_terms") or {}) if isinstance(config, dict) else {}
        if not top_cfg.get("enabled", False):
            self.logger.info(
                "bigquery_trends.top_terms disabled, skipping (market=%s)", self.market
            )
            return self.empty_dataframe()

        days = int(top_cfg.get("days", self.DEFAULT_DAYS))
        limit = int(top_cfg.get("limit", self.DEFAULT_TOP_TERMS_LIMIT))

        try:
            sql = _TOP_TERMS_SQL_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.logger.error("Top-terms SQL file missing at %s", _TOP_TERMS_SQL_PATH)
            return self.empty_dataframe()

        params = {"country_code": country_code, "days": days, "lim": limit}

        try:
            results = bq_utils.run_query(sql, params=params)
        except (BadRequest, NotFound) as e:
            self.logger.error(
                "BigQuery top_terms query failed for market=%s: %s",
                self.market,
                str(e)[:300],
            )
            return self.empty_dataframe()
        except Exception as e:
            self.logger.error(
                "Unexpected error running BigQuery top_terms query for market=%s: %s",
                self.market,
                str(e)[:300],
            )
            return self.empty_dataframe()

        if results is None or results.empty:
            expected_map = top_cfg.get("expected_per_market") or {}
            market_key = self.market.lower()
            if market_key in expected_map:
                expected = bool(expected_map[market_key])
            else:
                expected = self.DEFAULT_EXPECTED_PER_MARKET_TOP_TERMS.get(market_key, True)
            if expected:
                self.logger.error(
                    "bigquery_trends.top_terms returned 0 rows for market=%s "
                    "(country_code=%s) but rows were expected.",
                    self.market,
                    country_code,
                )
            else:
                self.logger.info(
                    "bigquery_trends.top_terms returned 0 rows for market=%s "
                    "(country_code=%s); expected zero per public dataset coverage.",
                    self.market,
                    country_code,
                )
            return self.empty_dataframe()

        rows = [
            self._normalise_top_term_row(row, limit) for row in results.to_dict(orient="records")
        ]
        df = pd.DataFrame(rows, columns=self.empty_dataframe().columns.tolist())
        df = df.drop_duplicates(subset=["query_term", "published_at"])
        # As above: top-term rows omit the GDELT v2/GCAM fields; empty-string them
        # so they never land as the literal "nan" in raw_content.
        for _col in ("v2tone", "v2persons", "v2orgs", "v2locations", "v2gcam"):
            df[_col] = df[_col].fillna("")
        self.logger.info(
            "bigquery_trends.top_terms fetched %d rows for market=%s (country_code=%s)",
            len(df),
            self.market,
            country_code,
        )
        return df

    def _normalise_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Map one SQL result row to the 16-column raw schema."""
        term = str(row.get("term") or "")
        week = row.get("week")
        refresh_date = row.get("refresh_date")
        published_at = self._to_timestamp(week) or self._to_timestamp(refresh_date)

        # id is not part of RAW_COLUMNS, so it is not returned from fetch().
        # The ingestion loader assigns a fresh uuid4 to each row per run, so
        # ids are not deterministic and there is no cross-run dedup on id.
        normalised = {
            "source": "Google Trends",
            "platform": self.PLATFORM,
            "market": self.market,
            "content_type": "search_term",
            "query_group": "search_intent",
            "query_term": term,
            "author_name": "",
            "author_handle": "",
            "title": term,
            "text": "",
            "url": "",
            "published_at": published_at,
            "views": 0.0,
            "likes": 0.0,
            "comments": 0.0,
            "shares": 0.0,
        }
        # Carry the SQL-computed search_velocity_score (percent_gain normalised in
        # search_velocity_terms.sql) only when the revive flag is on. When off,
        # the key is omitted and the value is dropped exactly as before, keeping
        # the live cron byte-identical. The downstream column list in
        # _fetch_rising_terms is widened to keep this slot under the same flag.
        if _search_velocity_on():
            try:
                normalised["search_velocity_score"] = float(row.get("search_velocity_score") or 0.0)
            except (TypeError, ValueError):
                normalised["search_velocity_score"] = 0.0
        return normalised

    def _normalise_top_term_row(self, row: dict[str, Any], limit: int) -> dict[str, Any]:
        """Map one top_terms SQL row to the 16-column raw schema.

        Notes on the field choices:
        - source="bigquery_trends" (vs the rising feed's "Google Trends" string)
          so downstream classifiers can tell the two streams apart cleanly.
        - content_type="top_term" distinguishes from the rising "search_term".
        - query_group="google_trends_top" gives scoring a stable bucket key.
        - views encodes inverse rank (rank 1 -> limit, rank limit -> 1) so the
          existing engagement-weighted scoring path treats higher-ranked terms
          as stronger signal without changing any scoring weights. The offset is
          derived from the configured limit so ranks past the default window
          still encode a positive view count. likes/comments/shares=0.
        """
        term = str(row.get("term") or "")
        rank = row.get("rank")
        score = row.get("score")
        week = row.get("week")
        refresh_date = row.get("refresh_date")
        published_at = self._to_timestamp(week) or self._to_timestamp(refresh_date)

        try:
            rank_int = int(rank) if rank is not None else 0
        except (TypeError, ValueError):
            rank_int = 0
        views = float(max(0, limit + 1 - rank_int)) if rank_int else 0.0

        week_str = week.isoformat() if hasattr(week, "isoformat") else str(week or "")
        score_str = f" score {score}" if score is not None else ""
        text = (
            f"{term} ranked #{rank_int} in {self.market.upper()} Google Trends top terms "
            f"(week of {week_str}){score_str}"
        )

        return {
            "source": "bigquery_trends",
            "platform": self.PLATFORM,
            "market": self.market,
            "content_type": "top_term",
            "query_group": "google_trends_top",
            "query_term": term,
            "author_name": "",
            "author_handle": "",
            "title": f"Top search: {term}",
            "text": text,
            "url": "",
            "published_at": published_at,
            "views": views,
            "likes": 0.0,
            "comments": 0.0,
            "shares": 0.0,
        }

    @staticmethod
    def _to_timestamp(value: Any) -> Any:
        """Convert a BigQuery DATE/TIMESTAMP cell to a pandas Timestamp.

        Returns None if the value is missing or unparseable.
        """
        if value is None:
            return None
        try:
            ts = pd.to_datetime(value, utc=True, errors="coerce")
        except Exception:
            return None
        if pd.isna(ts):
            return None
        return ts

    @staticmethod
    def build_row_id(market: str, term: str, date_key: Any) -> str:
        """Deterministic hash for (market, term, date). Exposed for ingestion use."""
        seed = f"{market}|{term}|{date_key}"
        # Non-cryptographic deterministic id for dedup; not used for security.
        return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()
