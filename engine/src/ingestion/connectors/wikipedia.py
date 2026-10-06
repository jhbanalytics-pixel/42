"""Wikipedia pageviews connector (Wave 2).

Pulls Wikimedia's free public "top viewed articles per country" pageviews
metric. No auth, no key, no rate-limit token. Surfaces what a whole market
is actually reading on Wikipedia on a given day, which is a slow but
high-signal complement to the social feed.

Docs-confirmed (live probe 19 Jun 2026):
    GET https://wikimedia.org/api/rest_v1/metrics/pageviews/top-per-country/
        {country}/{access}/{year}/{month}/{day}

Response envelope (verified live):
    {
      "items": [
        {
          "country": "ZA",
          "access": "all-access",
          "year": "2026", "month": "06", "day": "18",
          "articles": [
            {"article": "Main_Page", "project": "en.wikipedia",
             "views_ceil": 34100, "rank": 1},
            ...
          ]
        }
      ]
    }

Notes that shaped the parser:
- The country endpoint exposes views_ceil (a privacy-rounded ceiling), not a
  plain views integer. We map it onto the views column.
- Data lags one to two days. We default to a 1-day offset and walk back up to
  max_lookback_days until an endpoint returns 200 with items.
- Articles can repeat across projects (en.wikipedia, simple.wikipedia, ...).
  We keep the per-(article, project) rows as the API returns them.
- Main_Page, Special:* and similar chrome titles carry no topic signal; we
  drop a small skip-set.

Dark on ship: sources.yaml `wikipedia.enabled` defaults false, so the
connector is wired into the registry but fetches nothing until the operator
flips the flag.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from src.ingestion.connectors.base import RAW_COLUMNS, BaseConnector
from src.utils.config_loader import load_sources

WIKIPEDIA_REST_BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews"
DEFAULT_LIMIT = 40
DEFAULT_MAX_LOOKBACK_DAYS = 3
SUPPORTED_MARKETS: frozenset[str] = frozenset({"za", "ng", "ke"})

# ISO 3166-1 alpha-2 country codes the Wikimedia endpoint expects.
_MARKET_TO_COUNTRY: dict[str, str] = {"za": "ZA", "ng": "NG", "ke": "KE"}

# Chrome / navigation titles that carry no topic signal.
_SKIP_PREFIXES: tuple[str, ...] = (
    "Special:",
    "Portal:",
    "Wikipedia:",
    "Help:",
    "Category:",
    "Template:",
    "File:",
)
# Software-path artifacts the per-country top-articles feed surfaces alongside
# real articles. A live probe (20 Jun) showed wiki.phtml topping ZA and KE; they
# carry no topic signal so they join the chrome skip-set.
_SKIP_EXACT: frozenset[str] = frozenset(
    {"Main_Page", "-", "Hauptseite", "wiki.phtml", "index.php", "index.html"}
)


class WikipediaConnector(BaseConnector):
    """Fetches Wikimedia top-per-country pageviews for one market."""

    SOURCE_NAME = "wikipedia"
    PLATFORM = "wikipedia"
    BASE_URL = WIKIPEDIA_REST_BASE
    RATE_LIMIT_DELAY = 0.5

    def fetch(
        self,
        *,
        active: bool | None = None,
        limit: int | None = None,
        **_: Any,
    ) -> pd.DataFrame:
        """Pull the top articles for this market's country.

        Args:
            active: per-config global gate. When False, returns empty.
            limit: how many top articles to keep (after skip-set filter).

        Returns a DataFrame matching RAW_COLUMNS. Empty on any failure or
        when the flag is off (chart data is supplementary; the cron must
        never block on it).
        """
        cfg = self._config()
        is_enabled = active if active is not None else cfg.get("enabled", False)
        if not is_enabled:
            return self._empty()

        if self.market not in SUPPORTED_MARKETS:
            self.logger.info("wikipedia: unsupported market %s, returning empty", self.market)
            return self._empty()

        country = _MARKET_TO_COUNTRY.get(self.market)
        if not country:
            return self._empty()

        n = int(limit or cfg.get("limit") or DEFAULT_LIMIT)
        n = max(1, min(n, 100))
        max_lookback = int(cfg.get("days") or DEFAULT_MAX_LOOKBACK_DAYS)
        max_lookback = max(1, min(max_lookback, 10))

        articles = self._fetch_articles(country, max_lookback)
        if not articles:
            self.logger.info("wikipedia: no articles for market=%s", self.market)
            return self._empty()

        rows = []
        for art in articles:
            row = self._normalise_article(art)
            if row is not None:
                rows.append(row)
            if len(rows) >= n:
                break

        if not rows:
            return self._empty()

        self.logger.info("wikipedia: fetched %d rows for market=%s", len(rows), self.market)
        return pd.DataFrame(rows, columns=RAW_COLUMNS)

    def _fetch_articles(self, country: str, max_lookback: int) -> list[dict[str, Any]]:
        """Walk back day by day until an endpoint returns items.

        Wikimedia pageview data lags one to two days, so the most recent day
        often 404s. We try yesterday first and walk back up to max_lookback.
        """
        today = datetime.now(UTC).date()
        for back in range(1, max_lookback + 1):
            target = today - timedelta(days=back)
            url = (
                f"{WIKIPEDIA_REST_BASE}/top-per-country/"
                f"{country}/all-access/"
                f"{target.year:04d}/{target.month:02d}/{target.day:02d}"
            )
            try:
                resp = self._session.get(
                    url,
                    timeout=15,
                    headers={"User-Agent": "TEV2-trends/1.0 (Ogilvy SSA trends engine)"},
                )
            except Exception as exc:
                self.logger.warning("wikipedia fetch failed for %s: %s", url, exc)
                continue

            if resp.status_code == 404:
                # Data not yet published for this day; walk back further.
                continue
            if resp.status_code != 200:
                self.logger.warning(
                    "wikipedia: status=%d for market=%s day=%s",
                    resp.status_code,
                    self.market,
                    target.isoformat(),
                )
                continue

            try:
                items = (resp.json() or {}).get("items") or []
            except ValueError:
                self.logger.warning("wikipedia: non-JSON response for market=%s", self.market)
                continue

            for item in items:
                articles = item.get("articles") or []
                if articles:
                    self._snapshot_day = target.isoformat()
                    return articles
        return []

    def _config(self) -> dict[str, Any]:
        sources = load_sources()
        return sources.get("wikipedia") or {}

    def _normalise_article(self, art: dict[str, Any]) -> dict[str, Any] | None:
        """Shape one article into RAW_COLUMNS. Returns None for skip-set titles."""
        raw_title = str(art.get("article") or "").strip()
        if not raw_title:
            return None
        if raw_title in _SKIP_EXACT or raw_title.startswith(_SKIP_PREFIXES):
            return None

        project = str(art.get("project") or "en.wikipedia").strip()
        rank = art.get("rank")
        # Country endpoint exposes views_ceil (privacy-rounded). Fall back to
        # views for forward-compat if the API ever adds it.
        views = art.get("views_ceil")
        if views is None:
            views = art.get("views") or 0

        # Article titles are underscore-joined; humanise for the classifier.
        display_title = raw_title.replace("_", " ")
        day = getattr(self, "_snapshot_day", datetime.now(UTC).date().isoformat())
        text = (
            f"{display_title}: a top-read Wikipedia article in "
            f"{self.market.upper()} on {day}"
            + (f", ranked #{int(rank)}" if isinstance(rank, int) else "")
            + f". Project: {project}."
        )
        # Build the canonical article URL from the project host.
        lang_host = project if "." in project else f"{project}.wikipedia"
        url = f"https://{lang_host}.org/wiki/{raw_title}"

        return {
            "source": "wikipedia",
            "platform": "wikipedia",
            "market": self.market,
            "content_type": "encyclopedia_article",
            "query_group": "wikipedia_top_per_country",
            "query_term": display_title[:300],
            "author_name": "",
            "author_handle": "",
            "title": display_title[:300],
            "text": text[:4000],
            "url": url,
            "published_at": f"{day}T00:00:00+00:00",
            "views": int(views) if isinstance(views, (int, float)) else 0,
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "v2tone": "",
            "v2persons": "",
            "v2orgs": "",
            "v2locations": "",
            "v2gcam": "",
        }

    def _empty(self) -> pd.DataFrame:
        return pd.DataFrame(columns=RAW_COLUMNS)


__all__ = [
    "DEFAULT_LIMIT",
    "WIKIPEDIA_REST_BASE",
    "WikipediaConnector",
]
