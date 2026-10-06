"""Pulsar connector: read-only social listening via the TRAC GraphQL API.

Evaluation channel (added 14 Jul 2026), DARK by default. Pulsar is an
enterprise social-listening platform. Its GraphQL data endpoint returns posts
from an indexed, boolean-defined "search" with a deep facet layer (sentiment,
8-emotion, image tags, city-level geo, audience communities). This connector
pulls recent posts from configured search hashes into enriched_content as
source=pulsar, to evaluate the signal alongside the existing connectors.

SAFE DISCONNECT is the whole design point (trial token, expires 24 Jul 2026):
- Gated by sources.yaml pulsar.enabled. Flag off -> empty_dataframe, ZERO
  network calls, zero effect on any other connector or the run. A hung or
  expired Pulsar API cannot touch the pipeline while the flag is off.
- Fully removable: delete the CONNECTORS entry + the config block + this file.
  No schema migration is required to disable (rows just stop landing).
- Read-only: this connector issues GraphQL QUERIES only, never a mutation.
  The Pulsar metadata/pusher endpoints can create/edit searches and push data;
  this connector deliberately does not import or expose any of that.

Two transport facts learned in the 14 Jul recon, baked in here:
1. The Pulsar edge (Cloudflare) 403s any request without a browser-like
   User-Agent, BEFORE auth. _PULSAR_UA is mandatory; without it every call
   dies with an unparseable HTML 403.
2. Auth is a bearer token (PULSAR_API_TOKEN), one token per subdomain.

Bounded like the other paid connectors: a per-run request budget (shared across
the three market instances) caps how many GraphQL calls a single run makes, so
the connector cannot run away against the API. Any HTTP/parse/GraphQL error is a
non-fatal _fetch_failure (a degradation, not a market failure).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pandas as pd
import requests

from src.ingestion.connectors.base import RAW_COLUMNS, BaseConnector
from src.utils.config_loader import load_sources
from src.utils.secrets import get_secret

# TRAC data endpoint (v2). The /trac/graphql form 404s; this is the live path.
DEFAULT_ENDPOINT = "https://data.pulsarplatform.com/graphql/trac"

# Mandatory. A default library User-Agent is blocked by the Pulsar WAF with a
# plain 403 before auth; any browser-like UA passes.
_PULSAR_UA = "Mozilla/5.0 (compatible; OgilvyTrendsEngineV2/1.0)"

DEFAULT_MAX_POSTS_PER_SEARCH = 100  # one page; the API hard-caps limit at 100.
DEFAULT_BUDGET_REQUESTS_PER_RUN = 12
DEFAULT_TIMEOUT = 30

PULSAR_QUERY_GROUP = "pulsar"

# Minimal post projection. Field names are the real schema keys (…Count
# suffixes), verified live 14 Jul. `results` requires a FilterInput with a date
# window, so the connector always passes dateFrom/dateTo. sentiment/city/region/
# countryName carry the Pulsar-specific value (per-post sentiment + fine geo)
# for a later scoring hook.
DEFAULT_LOOKBACK_DAYS = 3
_POSTS_QUERY = """
query Posts($filter: FilterInput!, $limit: Int!, $cursor: String) {
  results(filter: $filter, options: {limit: $limit, cursor: $cursor, sortBy: TIME, sort: DESC}) {
    total
    nextCursor
    results {
      url
      content
      source
      publishedAt
      userName
      userScreenName
      sentiment
      engagement
      likesCount
      sharesCount
      commentsCount
      viewsCount
      countryName
      city
      region
      language
    }
  }
}
""".strip()


class PulsarConnector(BaseConnector):
    """Bounded read-only post pull from configured Pulsar search(es).

    Reads sources.yaml pulsar (enabled, endpoint, budget_requests_per_run,
    max_posts) and the per-market block pulsar.markets.<market> (search_hashes).
    Off by default; returns an empty frame and makes no network call when
    disabled or unconfigured for a market.
    """

    SOURCE_NAME = "pulsar"
    PLATFORM = "pulsar"
    RATE_LIMIT_DELAY = 0.25  # under the documented 5 req/s.

    # Shared across the three market instances so the request budget bounds the
    # whole run, not each market. Reset only in tests.
    _global_requests_made: ClassVar[int] = 0

    @classmethod
    def reset_requests(cls) -> None:
        cls._global_requests_made = 0

    def fetch(self, **kwargs: Any) -> pd.DataFrame:
        sources = load_sources()
        config = sources.get("pulsar", {}) or {}
        if not config.get("enabled", False):
            # Flag off: the safe-disconnect path. No token read, no network.
            return self.empty_dataframe()

        token = (get_secret("PULSAR_API_TOKEN") or os.environ.get("PULSAR_API_TOKEN", "")).strip()
        if not token:
            self.logger.warning("PULSAR_API_TOKEN missing, pulsar skipped for %s", self.market)
            self._fetch_failures.append("pulsar: PULSAR_API_TOKEN missing")
            return self.empty_dataframe()

        market_cfg = (config.get("markets", {}) or {}).get(self.market, {}) or {}
        search_hashes = [str(h).strip() for h in (market_cfg.get("search_hashes") or []) if h]
        if not search_hashes:
            return self.empty_dataframe()

        endpoint = str(config.get("endpoint", DEFAULT_ENDPOINT))
        max_posts = int(config.get("max_posts", DEFAULT_MAX_POSTS_PER_SEARCH))
        budget = int(config.get("budget_requests_per_run", DEFAULT_BUDGET_REQUESTS_PER_RUN))
        timeout = int(config.get("timeout", DEFAULT_TIMEOUT))
        lookback_days = int(config.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
        now = datetime.now(UTC)
        date_from = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%dT00:00:00Z")
        date_to = now.strftime("%Y-%m-%dT23:59:59Z")

        session = requests.Session()
        session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": _PULSAR_UA,
                "Content-Type": "application/json",
            }
        )

        rows: list[dict[str, Any]] = []
        for search_hash in search_hashes:
            if PulsarConnector._global_requests_made >= budget:
                self.logger.info("pulsar request budget %d reached, stopping", budget)
                break
            variables = {
                # FilterInput.searchIds keys on the search HASH, not the
                # numeric id, and requires a date window.
                "filter": {
                    "searchIds": [search_hash],
                    "dateFrom": date_from,
                    "dateTo": date_to,
                },
                "limit": min(max_posts, 100),
                "cursor": None,
            }
            payload = self._graphql(session, endpoint, variables, timeout, search_hash)
            if payload is None:
                continue
            for post in self._extract_posts(payload):
                row = self._to_row(post, search_hash)
                if row is not None:
                    rows.append(row)

        if not rows:
            return self.empty_dataframe()
        df = pd.DataFrame(rows)
        df["market"] = self.market
        return df.reindex(columns=list(RAW_COLUMNS), fill_value="")

    def _graphql(
        self,
        session: requests.Session,
        endpoint: str,
        variables: dict[str, Any],
        timeout: int,
        search_hash: str,
    ) -> dict[str, Any] | None:
        """One POST. Returns the data payload, or None on any failure (logged
        + recorded as a non-fatal fetch failure). Charges the request budget."""
        PulsarConnector._global_requests_made += 1
        try:
            resp = session.post(
                endpoint,
                json={"query": _POSTS_QUERY, "variables": variables},
                timeout=timeout,
            )
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            self.logger.warning("pulsar %s failed: %s", search_hash, str(exc)[:200])
            self._fetch_failures.append(f"pulsar {search_hash}: {str(exc)[:150]}")
            return None
        # A GraphQL 200 can still carry errors[]; treat as a soft failure.
        if isinstance(body, dict) and body.get("errors"):
            msg = str(body["errors"])[:150]
            self.logger.warning("pulsar %s GraphQL error: %s", search_hash, msg)
            self._fetch_failures.append(f"pulsar {search_hash}: {msg}")
            return None
        return body.get("data") if isinstance(body, dict) else None

    @staticmethod
    def _extract_posts(data: dict[str, Any]) -> list[dict[str, Any]]:
        results = data.get("results") if isinstance(data, dict) else None
        if isinstance(results, dict) and isinstance(results.get("results"), list):
            return results["results"]
        return []

    def _to_row(self, post: dict[str, Any], search_hash: str) -> dict[str, Any] | None:
        if not isinstance(post, dict):
            return None
        text = str(post.get("content") or "")
        # Pulsar's `source` is the channel (X, REDDIT, YOUTUBE...). Surface it
        # as the real platform so downstream diversity scoring is correct,
        # keeping source=pulsar as the connector tag.
        platform = str(post.get("source") or "pulsar").lower()
        return {
            "source": self.SOURCE_NAME,
            "platform": platform,
            "market": self.market,
            "content_type": f"{platform}/pulsar",
            "query_group": PULSAR_QUERY_GROUP,
            "query_term": search_hash,
            "author_name": str(post.get("userName") or post.get("userScreenName") or ""),
            "author_handle": str(post.get("userScreenName") or ""),
            "title": text[:200],
            "text": text,
            "url": str(post.get("url") or ""),
            "published_at": str(post.get("publishedAt") or ""),
            "views": self._as_float(post.get("viewsCount")),
            "likes": self._as_float(post.get("likesCount")),
            "comments": self._as_float(post.get("commentsCount")),
            "shares": self._as_float(post.get("sharesCount")),
            "v2tone": "",
            "v2persons": "",
            "v2orgs": "",
            "v2locations": "",
            "v2gcam": "",
        }

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0
