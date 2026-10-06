"""Semrush keyword metrics connector.

Pulls search demand signal from the Semrush v4 Keywords API for configured
terms per market. One API call per keyword (20 units each on standard plans).

Auth: ``Authorization: Apikey <SEMRUSH_API_KEY>`` header. Key from env or
Secret Manager via ``get_secret("semrush-api-key")``.

Dark on ship: ``sources.yaml`` ``semrush.enabled`` defaults false. Flip after
live probe with ``python scripts/verify_live.py semrush za mpesa``.

Budget guard: stops the keyword loop on ERROR 132 (unit balance exhausted)
without retrying. Respects ``budget_units_per_run`` per market per cron run.

Reference: https://developer.semrush.com/api/v3/analytics/keyword-reports/
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import requests

from src.ingestion.connectors.base import RAW_COLUMNS, BaseConnector
from src.ingestion.enrichment import _search_velocity_on
from src.utils.config_loader import load_sources
from src.utils.secrets import get_secret

SEMRUSH_METRICS_URL = "https://api.semrush.com/apis/v4/keywords/v1/metrics"
DEFAULT_UNITS_PER_KEYWORD = 20
DEFAULT_BUDGET_UNITS_PER_RUN = 60
REQUEST_TIMEOUT_SECONDS = 30
SUPPORTED_MARKETS: frozenset[str] = frozenset({"za", "ng", "ke"})
_MARKET_TO_COUNTRY: dict[str, str] = {"za": "ZA", "ng": "NG", "ke": "KE"}
_ERROR_132_RE = re.compile(r"ERROR\s+132\b", re.IGNORECASE)


def _parse_keyword_entry(entry: Any) -> tuple[str, str] | None:
    """Normalise a sources.yaml keyword entry to (term, query_group)."""
    if isinstance(entry, str):
        term = entry.strip()
        return (term, "search_intent") if term else None
    if isinstance(entry, dict):
        term = str(entry.get("term") or entry.get("keyword") or "").strip()
        if not term:
            return None
        group = str(entry.get("query_group") or "search_intent").strip() or "search_intent"
        return term, group
    return None


def _trends_to_velocity(trends: Any) -> float:
    """Map Semrush monthly trend index (0-100) to a 0-1 velocity score."""
    if not isinstance(trends, list) or len(trends) < 2:
        return 0.0
    nums: list[float] = []
    for item in trends:
        try:
            nums.append(float(item))
        except (TypeError, ValueError):
            continue
    if len(nums) < 2:
        return 0.0
    lo, hi = min(nums), max(nums)
    if hi <= 0:
        return 0.0
    return round(min(1.0, max(0.0, (hi - lo) / hi)), 4)


def _metrics_summary(keyword: str, country: str, data: dict[str, Any]) -> str:
    """Human-readable summary for the raw row text field."""
    volume = data.get("search_volume", "")
    difficulty = data.get("keyword_difficulty", "")
    cpc = data.get("cpc", "")
    intents = data.get("intents") or []
    intent_str = ", ".join(str(i) for i in intents) if isinstance(intents, list) else str(intents)
    serp = data.get("serp_features") or []
    serp_str = ", ".join(str(s) for s in serp[:6]) if isinstance(serp, list) else str(serp)
    return (
        f"Semrush keyword metrics for {keyword!r} in {country}: "
        f"search_volume={volume}, keyword_difficulty={difficulty}, cpc={cpc}, "
        f"intents=[{intent_str}], serp_features=[{serp_str}]"
    )


def _response_indicates_budget_exhausted(body: str) -> bool:
    return bool(_ERROR_132_RE.search(body))


class SemrushConnector(BaseConnector):
    """Fetches Semrush keyword metrics for one market's configured terms."""

    SOURCE_NAME = "semrush"
    PLATFORM = "semrush_search"
    RATE_LIMIT_DELAY = 0.2

    def __init__(self, market: str = ""):
        super().__init__(market=market)
        self._units_spent = 0
        self._budget_exhausted = False

    def fetch(self, **kwargs: Any) -> pd.DataFrame:
        cfg = load_sources().get("semrush", {}) or {}
        if cfg.get("enabled", False) is not True:
            self.logger.info("semrush inactive in sources.yaml, skipping (market=%s)", self.market)
            return self.empty_dataframe()

        if self.market not in SUPPORTED_MARKETS:
            self.logger.info("semrush: unsupported market %s", self.market)
            return self.empty_dataframe()

        api_key = (
            kwargs.get("api_key")
            or get_secret("semrush-api-key")
            or os.environ.get("SEMRUSH_API_KEY", "")
        ).strip()
        if not api_key:
            self.logger.warning(
                "SEMRUSH_API_KEY not set; semrush connector returning empty (market=%s)",
                self.market,
            )
            return self.empty_dataframe()

        country = _MARKET_TO_COUNTRY.get(self.market, "")
        if not country:
            return self.empty_dataframe()

        keywords = self._market_keywords(cfg)
        if not keywords:
            self.logger.info("semrush: no keywords configured for market=%s", self.market)
            return self.empty_dataframe()

        units_per_kw = int(cfg.get("units_per_keyword") or DEFAULT_UNITS_PER_KEYWORD)
        budget = int(cfg.get("budget_units_per_run") or DEFAULT_BUDGET_UNITS_PER_RUN)
        budget = max(units_per_kw, budget)

        rows: list[dict[str, Any]] = []
        for term, query_group in keywords:
            if self._budget_exhausted:
                break
            if self._units_spent + units_per_kw > budget:
                self.logger.info(
                    "semrush: budget_units_per_run=%d reached for market=%s",
                    budget,
                    self.market,
                )
                break

            payload = self._fetch_keyword_metrics(api_key, term, country)
            self._units_spent += units_per_kw

            if payload is None:
                if self._budget_exhausted:
                    break
                continue

            row = self._metrics_to_row(term, query_group, country, payload)
            if row:
                rows.append(row)

        if not rows:
            return self.empty_dataframe()

        columns = list(RAW_COLUMNS)
        if _search_velocity_on():
            columns = [*columns, "search_velocity_score"]
        return pd.DataFrame(rows, columns=columns)

    def _market_keywords(self, cfg: dict[str, Any]) -> list[tuple[str, str]]:
        markets_cfg = cfg.get("markets") or {}
        market_cfg = markets_cfg.get(self.market) or {}
        raw = market_cfg.get("keywords") or []
        out: list[tuple[str, str]] = []
        for entry in raw:
            parsed = _parse_keyword_entry(entry)
            if parsed:
                out.append(parsed)
        return out

    def _fetch_keyword_metrics(
        self, api_key: str, keyword: str, country: str
    ) -> dict[str, Any] | None:
        if self._request_count > 0:
            time.sleep(self.RATE_LIMIT_DELAY)
        self._request_count += 1

        params = {"keyword": keyword, "country": country}
        headers = {"Authorization": f"Apikey {api_key}"}
        self.logger.info(
            "Request #%d: GET %s (keyword=%r, country=%s)",
            self._request_count,
            SEMRUSH_METRICS_URL,
            keyword,
            country,
        )

        try:
            resp = self._session.get(
                SEMRUSH_METRICS_URL,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException as exc:
            self.logger.error(
                "semrush request failed for keyword=%r market=%s: %s",
                keyword,
                self.market,
                str(exc)[:200],
            )
            return None

        body = resp.text or ""
        if _response_indicates_budget_exhausted(body):
            self._budget_exhausted = True
            self.logger.critical(
                "semrush ERROR 132 unit balance exhausted; halting semrush ingestion "
                "(market=%s, keyword=%r)",
                self.market,
                keyword,
            )
            return None

        if resp.status_code != 200:
            self.logger.error(
                "semrush HTTP %s for keyword=%r market=%s: %s",
                resp.status_code,
                keyword,
                self.market,
                body[:300],
            )
            return None

        try:
            envelope = resp.json()
        except json.JSONDecodeError:
            if _response_indicates_budget_exhausted(body):
                self._budget_exhausted = True
                self.logger.critical("semrush ERROR 132 in non-JSON body; halting")
            else:
                self.logger.error("semrush invalid JSON for keyword=%r: %s", keyword, body[:200])
            return None

        meta = envelope.get("meta") or {}
        if meta.get("success") is False or int(meta.get("status_code") or 0) >= 400:
            err_msg = json.dumps(meta)[:300]
            if _response_indicates_budget_exhausted(
                err_msg
            ) or _response_indicates_budget_exhausted(body):
                self._budget_exhausted = True
                self.logger.critical("semrush budget error in meta; halting")
            else:
                self.logger.error("semrush API error for keyword=%r: %s", keyword, err_msg)
            return None

        data = envelope.get("data")
        if not isinstance(data, dict):
            self.logger.warning("semrush empty data for keyword=%r market=%s", keyword, self.market)
            return None
        return data

    def _metrics_to_row(
        self,
        keyword: str,
        query_group: str,
        country: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        # search_volume and keyword_difficulty stay out of the engagement
        # fields on purpose: mapping them to views/likes would inflate
        # engagement_total on flip. Semrush contributes through
        # search_velocity_score only; the raw metrics live in the text summary.
        row: dict[str, Any] = {
            "source": self.SOURCE_NAME,
            "platform": self.PLATFORM,
            "market": self.market,
            "content_type": "keyword_metric",
            "query_group": query_group,
            "query_term": keyword,
            "author_name": "",
            "author_handle": "",
            "title": keyword,
            "text": _metrics_summary(keyword, country, data),
            "url": "",
            "published_at": datetime.now(UTC),
            "views": 0.0,
            "likes": 0.0,
            "comments": 0.0,
            "shares": 0.0,
        }
        if _search_velocity_on():
            row["search_velocity_score"] = _trends_to_velocity(data.get("trends"))
        return row
