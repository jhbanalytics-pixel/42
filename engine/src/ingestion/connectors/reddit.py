"""Reddit connector via EnsembleData /reddit/* endpoints.

Reddit's official Data API free tier is non-commercial-only post-2023
policy change; commercial use (TEV2 is a client deliverable) requires
an enterprise contract at undisclosed pricing. EnsembleData on the
Bronze plan we already pay covers Reddit scraping and handles ToS on
the vendor side, so this connector routes through EnsembleData rather
than calling Reddit directly.

Budget discipline:
  - Shares the EnsembleConnector class-level `_global_units_spent` and
    `_global_quota_exhausted` flags so the combined daily spend stays
    under the Bronze 5000-unit cap. The pipeline calls EnsembleConnector
    first per market, then RedditConnector; if Ensemble exhausted the
    quota, Reddit short-circuits and returns an empty DataFrame.
  - Per-run target ~1475 units across all three markets (hot+rising+top
    posts on ~45 subreddits, top-5 post comments per sub, ~25 slang
    keyword searches across r/all). Leaves ~1855 units of Bronze cap
    headroom for spikes and future expansion.

Default-off via sources.yaml `reddit.enabled` so the connector is wired
in but produces zero rows until the per-market configuration is
populated and Albert flips the flag. Lets us land the code path
without changing pipeline behaviour on Day 0.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse

import pandas as pd
import requests

from src.ingestion.connectors.base import BaseConnector
from src.ingestion.connectors.ensemble import (
    DEFAULT_ESTIMATED_UNITS_PER_CALL,
    ENSEMBLE_AUTH_STATUS,
    ENSEMBLE_QUOTA_STATUS,
    EnsembleConnector,
)
from src.utils.config_loader import load_sources
from src.utils.secrets import get_secret

# Default tuning. Every value is overrideable from the `reddit:` block in
# sources.yaml so Albert can rebalance the spend without a code change.
DEFAULT_SORT_MODES: tuple[str, ...] = ("hot", "rising", "top")
DEFAULT_TOP_PERIOD = "day"
DEFAULT_POSTS_PER_SORT = 25
DEFAULT_COMMENT_POSTS_PER_SUB = 5
DEFAULT_COMMENTS_PER_POST = 20
DEFAULT_KEYWORD_SEARCH_LIMIT = 25
DEFAULT_RUN_BUDGET_UNITS = 1500

# Circuit-breaker threshold. After this many CONSECUTIVE non-404 4xx responses
# on the vendor (a systemic contract break, not a single dead sub), the fetch
# aborts the remaining phases instead of burning one estimated unit per call
# for the whole sub + keyword pool. 404 (dead sub) and 495/493 (quota/auth,
# already handled) do not count toward this; a single good response resets it.
DEFAULT_CONSECUTIVE_4XX_LIMIT = 5

# Foreign subreddits whose bare community name collides with an SSA topic
# keyword. The keyword search runs across r/all, so a search for "ankara" (the
# Nigerian fabric) returns posts from r/ankara (the Turkish capital's city sub).
# Those carry no Turkish-specific token the geo-blocklist can catch (e.g. an
# r/ankara geography meme leaked into the 8-Jun NG ankara card), so they are
# dropped here BY SOURCE at ingestion before they can be tagged with the
# collision-prone NG topic. Conservative: only unambiguously-foreign subs,
# never a real NG/SSA community. Overrideable via
# reddit.keyword_search_exclude_subreddits in sources.yaml.
DEFAULT_KEYWORD_SUBREDDIT_EXCLUDE: frozenset[str] = frozenset(
    {
        "ankara",  # Turkish capital city sub (collides with the NG ankara fabric)
        "askturkey",
        "askmiddleeast",
        "turkey",
        "istanbul",
    }
)

# Reddit endpoint paths under the EnsembleData base URL. Centralised here
# so a vendor path change is a single-line edit. Each path is exposed
# under `reddit.endpoints.<name>` in sources.yaml; the YAML wins when
# present so the connector survives a vendor rename without redeploy.
DEFAULT_ENDPOINTS: dict[str, str] = {
    "subreddit_posts": "/reddit/subreddit/posts",
    "post_comments": "/reddit/post/comments",
    "keyword_search": "/reddit/keyword/search",
}


class RedditConnector(BaseConnector):
    """Fetches Reddit posts and comments via EnsembleData."""

    SOURCE_NAME = "reddit"
    PLATFORM = "reddit"
    BASE_URL = "https://ensembledata.com/apis"

    # Matches EnsembleConnector's lowered delay (Bronze allows 60 rpm
    # documented). 0.25s keeps the connector well under the rate ceiling
    # even when bursting comment fetches.
    RATE_LIMIT_DELAY = 0.25

    # Process-wide cache of (subreddit, sort_mode) tuples already fetched
    # by ANY market in this run. Subreddits like r/Africa appear in
    # multiple per-market pools; without dedup the same /subreddit/posts
    # call would fire 3 times. Cleared at process exit.
    _fetched_subreddit_sorts: ClassVar[set[tuple[str, str]]] = set()
    # Subreddits that returned 4xx/404 once; subsequent calls in the same
    # process skip them to avoid wasting units. Surfaced to logs so the
    # operator can prune the YAML.
    _dead_subreddits: ClassVar[set[str]] = set()

    @classmethod
    def reset_caches(cls) -> None:
        """Reset the per-process dedup caches. Intended for tests."""
        cls._fetched_subreddit_sorts.clear()
        cls._dead_subreddits.clear()

    def _reddit_budget_reached(self, ceiling: int) -> bool:
        """True when the next call would push the SHARED Reddit ledger past the
        global ceiling.

        Reads ``EnsembleConnector._global_reddit_units_spent`` (the class-level
        tally charged incrementally by every market in this process) rather than
        the local per-fetch counter, so the ceiling bounds TOTAL daily Reddit
        spend across all markets instead of resetting each fetch(). Mirrors
        ``EnsembleConnector._budget_reached``.
        """
        est = DEFAULT_ESTIMATED_UNITS_PER_CALL
        return EnsembleConnector._global_reddit_units_spent + est > ceiling

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(self, **kwargs: Any) -> pd.DataFrame:
        """Fetch posts + comments for this connector's market.

        Pipeline call shape mirrors the other connectors: one instance per
        market, one fetch() per pipeline run. Pan-African subreddits are
        listed in every market pool but the process-wide dedup cache
        ensures each (subreddit, sort) is hit only once per run.
        """
        # Config gate before the secret lookup. The gate itself was always
        # correct, but reading the token first meant a disabled connector still
        # called Secret Manager once per market, and those calls now fail: the
        # 2026-08-20 cron logged three ERROR lines for ENSEMBLEDATA_API_TOKEN
        # from this path alone, on a vendor cancelled 23 Jul 2026. Errors nobody
        # can act on are what hide the errors somebody can.
        sources = load_sources()
        config = sources.get("reddit", {}) or {}
        if not config.get("enabled", False):
            self.logger.info("Reddit connector disabled in sources.yaml")
            return self.empty_dataframe()

        token = get_secret("ENSEMBLEDATA_API_TOKEN")
        if not token:
            self.logger.warning("ENSEMBLEDATA_API_TOKEN not configured; skipping reddit fetch")
            return self.empty_dataframe()

        endpoints = self._resolve_endpoints(config)
        subreddits = self._resolve_subreddits(config)
        slang_keywords = list((config.get("slang_keywords", {}) or {}).get(self.market, []))

        if not subreddits and not slang_keywords:
            self.logger.warning(
                "No subreddits or slang keywords configured for market=%s",
                self.market,
            )
            return self.empty_dataframe()

        # Share ledger with EnsembleConnector. Pipeline calls Ensemble
        # before Reddit per market; if Ensemble blew the quota, Reddit
        # short-circuits immediately.
        if EnsembleConnector._global_quota_exhausted:
            self.logger.warning(
                "EnsembleData budget exhausted upstream; skipping reddit market=%s",
                self.market,
            )
            return self.empty_dataframe()

        # run_budget doubles as the GLOBAL ceiling on the shared Reddit ledger
        # (EnsembleConnector._global_reddit_units_spent), checked via
        # _reddit_budget_reached before every call. Because that ledger is
        # class-level and charged incrementally below, total daily Reddit spend
        # stays near this value across all markets instead of being markets x
        # budget (the per-market reset over-spend).
        run_budget = int(config.get("budget_units_per_run", DEFAULT_RUN_BUDGET_UNITS))
        # Per-fetch circuit breaker. _safe_request increments _consecutive_4xx on
        # each non-404 4xx and resets it on a good response; 404 (dead sub) is
        # ignored either way. Once it reaches the limit _safe_request sets
        # _circuit_open and the phase loops below stop.
        self._consecutive_4xx = 0
        self._circuit_open = False
        run_units_spent = 0
        all_rows: list[dict[str, Any]] = []
        post_id_seen: set[str] = set()
        # Per-sub list of (post_id, score) so the comment phase pulls the
        # highest-engagement posts regardless of which sort mode found them.
        top_posts_per_sub: dict[str, list[tuple[str, int]]] = {}

        # Phase 1: subreddit posts across multiple sort modes
        sort_modes = tuple(config.get("sort_modes", list(DEFAULT_SORT_MODES))) or DEFAULT_SORT_MODES
        top_period = str(config.get("top_period", DEFAULT_TOP_PERIOD))
        posts_per_sort = int(config.get("posts_per_sort", DEFAULT_POSTS_PER_SORT))

        for sub in subreddits:
            if sub in self._dead_subreddits:
                continue
            if self._circuit_open or EnsembleConnector._global_quota_exhausted:
                break
            if self._reddit_budget_reached(run_budget):
                self.logger.info(
                    "Reddit ledger ceiling %d reached for market=%s", run_budget, self.market
                )
                break
            for sort_mode in sort_modes:
                if self._circuit_open or EnsembleConnector._global_quota_exhausted:
                    break
                if self._reddit_budget_reached(run_budget):
                    break
                cache_key = (sub.lower(), sort_mode)
                if cache_key in self._fetched_subreddit_sorts:
                    self.logger.debug(
                        "Skip already-fetched %s/%s (cross-market dedup)", sub, sort_mode
                    )
                    continue
                self._fetched_subreddit_sorts.add(cache_key)
                rows, units, posts_for_comments = self._fetch_subreddit_posts(
                    endpoints["subreddit_posts"],
                    sub,
                    sort_mode,
                    top_period,
                    posts_per_sort,
                    token,
                )
                run_units_spent += units
                EnsembleConnector._global_reddit_units_spent += units
                for r in rows:
                    pid = r.pop("_reddit_post_id", "") or ""
                    if pid and pid not in post_id_seen:
                        post_id_seen.add(pid)
                        all_rows.append(r)
                if sub not in self._dead_subreddits and posts_for_comments:
                    top_posts_per_sub.setdefault(sub, []).extend(posts_for_comments)

        # Phase 2: comment threads on the top-engagement posts per sub
        comment_posts_per_sub = int(
            config.get("comment_posts_per_sub", DEFAULT_COMMENT_POSTS_PER_SUB)
        )
        comments_per_post = int(config.get("comments_per_post", DEFAULT_COMMENTS_PER_POST))
        if comment_posts_per_sub > 0 and comments_per_post > 0:
            for sub, posts in top_posts_per_sub.items():
                if self._circuit_open or EnsembleConnector._global_quota_exhausted:
                    break
                if self._reddit_budget_reached(run_budget):
                    break
                seen_permalinks: set[str] = set()
                ranked = sorted(posts, key=lambda p: p[1], reverse=True)
                for permalink, _score in ranked:
                    if len(seen_permalinks) >= comment_posts_per_sub:
                        break
                    if permalink in seen_permalinks:
                        continue
                    seen_permalinks.add(permalink)
                    if self._circuit_open or EnsembleConnector._global_quota_exhausted:
                        break
                    if self._reddit_budget_reached(run_budget):
                        break
                    rows, units = self._fetch_post_comments(
                        endpoints["post_comments"],
                        permalink,
                        sub,
                        comments_per_post,
                        token,
                    )
                    run_units_spent += units
                    EnsembleConnector._global_reddit_units_spent += units
                    all_rows.extend(rows)

        # Phase 3: slang keyword search across Reddit. The vendor's
        # /reddit/keyword/search endpoint requires the same sort+period
        # contract as /subreddit/posts (validated 27 May 2026); use the
        # configured top_period for the time window so the keyword search
        # returns the same recency band as the subreddit pulls.
        keyword_search_max = int(config.get("keyword_search_max", DEFAULT_KEYWORD_SEARCH_LIMIT))
        keyword_sort = str(config.get("keyword_sort", "hot"))
        kw_exclude = self._resolve_keyword_exclude(config)
        for keyword in slang_keywords[:keyword_search_max]:
            if self._circuit_open or EnsembleConnector._global_quota_exhausted:
                break
            if self._reddit_budget_reached(run_budget):
                break
            rows, units = self._fetch_keyword_search(
                endpoints["keyword_search"],
                keyword,
                keyword_sort,
                top_period,
                posts_per_sort,
                token,
                kw_exclude,
            )
            run_units_spent += units
            EnsembleConnector._global_reddit_units_spent += units
            for r in rows:
                pid = r.pop("_reddit_post_id", "") or ""
                if pid and pid not in post_id_seen:
                    post_id_seen.add(pid)
                    all_rows.append(r)

        # Spend is charged to Reddit's OWN ledger (_global_reddit_units_spent)
        # incrementally inside the phase loops above, NOT to EnsembleConnector's
        # _global_units_spent. Reddit gates itself against this ledger via
        # _reddit_budget_reached and honours the shared _global_quota_exhausted
        # (HTTP 495) flag, so the EnsembleData daily quota stays protected.
        # Charging the shared _global_units_spent would inflate the ledger that
        # ensemble's per-run budget gate reads, so after ZA Reddit the NG/KE
        # ensemble passes would fire zero calls and lose all TikTok/IG/Threads
        # ingestion. Incremental charging (vs one add at fetch end) is what lets
        # the ceiling pre-check trip mid-run and bounds total spend across
        # markets.
        self.logger.info(
            "Reddit market=%s rows=%d units=%d reddit_run_total=%d",
            self.market,
            len(all_rows),
            run_units_spent,
            EnsembleConnector._global_reddit_units_spent,
        )
        return self._finalise(all_rows)

    # ------------------------------------------------------------------
    # Config resolution
    # ------------------------------------------------------------------

    def _resolve_endpoints(self, config: dict) -> dict[str, str]:
        """Merge YAML endpoint overrides on top of DEFAULT_ENDPOINTS.

        Centralising the paths lets Albert hot-patch a vendor rename via
        sources.yaml without redeploying the connector.
        """
        yaml_endpoints = config.get("endpoints", {}) or {}
        return {
            "subreddit_posts": str(
                yaml_endpoints.get("subreddit_posts") or DEFAULT_ENDPOINTS["subreddit_posts"]
            ),
            "post_comments": str(
                yaml_endpoints.get("post_comments") or DEFAULT_ENDPOINTS["post_comments"]
            ),
            "keyword_search": str(
                yaml_endpoints.get("keyword_search") or DEFAULT_ENDPOINTS["keyword_search"]
            ),
        }

    def _resolve_keyword_exclude(self, config: dict) -> frozenset[str]:
        """Foreign subreddits to drop from keyword-search results.

        Defaults to ``DEFAULT_KEYWORD_SUBREDDIT_EXCLUDE``. A sources.yaml
        ``reddit.keyword_search_exclude_subreddits`` list REPLACES the default
        when present (lower-cased, r/ prefix stripped) so the team can extend
        coverage for a new collision without a code change.
        """
        override = config.get("keyword_search_exclude_subreddits")
        if override:
            return frozenset(str(s).strip().lower().removeprefix("r/") for s in override)
        return DEFAULT_KEYWORD_SUBREDDIT_EXCLUDE

    def _resolve_subreddits(self, config: dict) -> list[str]:
        """Return the deduped subreddit pool for this connector's market.

        Pulls the per-market list plus the pan_african shared list. Order
        preserved (per-market first, then pan-African). Process-level
        dedup happens later via _fetched_subreddit_sorts.
        """
        subs_block = config.get("subreddits", {}) or {}
        per_market = list(subs_block.get(self.market, []) or [])
        pan_african = list(subs_block.get("pan_african", []) or [])
        seen: set[str] = set()
        out: list[str] = []
        for s in per_market + pan_african:
            key = str(s).strip().lower().removeprefix("r/")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(str(s).strip().removeprefix("r/"))
        return out

    # ------------------------------------------------------------------
    # Single endpoint helpers
    # ------------------------------------------------------------------

    def _fetch_subreddit_posts(
        self,
        path: str,
        subreddit: str,
        sort_mode: str,
        top_period: str,
        limit: int,
        token: str,
    ) -> tuple[list[dict[str, Any]], int, list[tuple[str, int]]]:
        """Pull one (subreddit, sort) page. Returns (rows, units, [(permalink, score)]).

        EnsembleData requires `period` for all sort modes (validated against
        the live API 27 May 2026 - `hot` and `rising` both 422 without it
        despite Reddit's native API treating period as top-only). Pass
        top_period for every call; the vendor ignores it for non-top sorts.

        The third tuple element feeds the Phase 2 comment fetcher: top
        posts (by score) get their top comments pulled. We carry permalink
        not post_id since /reddit/post/comments takes `permalink`.
        """
        params: dict[str, Any] = {
            "name": subreddit,
            "sort": sort_mode,
            "period": top_period,
            "limit": limit,
            "token": token,
        }
        payload = self._safe_request(path, params, context=f"subreddit/{subreddit}/{sort_mode}")
        if payload is None:
            return [], DEFAULT_ESTIMATED_UNITS_PER_CALL, []
        units = self._extract_units_charged(payload) or DEFAULT_ESTIMATED_UNITS_PER_CALL
        items = self._extract_items(payload)
        rows: list[dict[str, Any]] = []
        post_pairs: list[tuple[str, int]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            row = self._normalise_post(item, subreddit, query_term=f"{sort_mode}")
            if row is None:
                continue
            rows.append(row)
            permalink = self._safe_str(item.get("permalink") or "")
            try:
                score = int(row.get("likes") or 0)
            except (TypeError, ValueError):
                score = 0
            if permalink:
                post_pairs.append((permalink, score))
        return rows, units, post_pairs

    def _fetch_post_comments(
        self,
        path: str,
        permalink: str,
        subreddit: str,
        limit: int,
        token: str,
    ) -> tuple[list[dict[str, Any]], int]:
        """Pull top-level comments for one post. Returns (rows, units).

        EnsembleData /reddit/post/comments takes `permalink` (not id, url,
        or post_id) - validated 27 May 2026. The permalink shape is
        ``/r/<sub>/comments/<post_id>/<slug>/`` carried verbatim from the
        post item, no transformation needed. The `limit` param is honoured.
        """
        # Extract post_id from the permalink for traceability in the
        # query_term column. permalink looks like /r/sub/comments/<id>/slug/
        post_id = ""
        parts = [p for p in (permalink or "").split("/") if p]
        if "comments" in parts:
            i = parts.index("comments")
            if i + 1 < len(parts):
                post_id = parts[i + 1]

        params: dict[str, Any] = {
            "permalink": permalink,
            "limit": limit,
            "token": token,
        }
        payload = self._safe_request(path, params, context=f"post_comments/{post_id or permalink}")
        if payload is None:
            return [], DEFAULT_ESTIMATED_UNITS_PER_CALL
        units = self._extract_units_charged(payload) or DEFAULT_ESTIMATED_UNITS_PER_CALL
        items = self._extract_items(payload)
        rows: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            row = self._normalise_comment(item, subreddit, post_id)
            if row is not None:
                rows.append(row)
        return rows, units

    def _fetch_keyword_search(
        self,
        path: str,
        keyword: str,
        sort_mode: str,
        period: str,
        limit: int,
        token: str,
        exclude_subs: frozenset[str],
    ) -> tuple[list[dict[str, Any]], int]:
        """Pull keyword-search results across r/all. Returns (rows, units).

        EnsembleData /reddit/keyword/search params: `name` (the search term),
        `sort`, `period`, `cursor` (REQUIRED, empty string for first page).
        Validated 27 May 2026 - the endpoint searches Reddit-wide for posts
        mentioning the term and returns cross-subreddit hits in classic
        Reddit shape. Confirms by returning a r/berlinsocialclub post for
        keyword=amapiano.
        """
        params: dict[str, Any] = {
            "name": keyword,
            "sort": sort_mode,
            "period": period,
            "cursor": "",
            "limit": limit,
            "token": token,
        }
        payload = self._safe_request(path, params, context=f"keyword/{keyword}")
        if payload is None:
            return [], DEFAULT_ESTIMATED_UNITS_PER_CALL
        units = self._extract_units_charged(payload) or DEFAULT_ESTIMATED_UNITS_PER_CALL
        items = self._extract_items(payload)
        rows: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # Each item carries its own subreddit (cross-sub search); pull
            # so the topic classifier still gets the community hint in the
            # text token.
            sub = self._safe_str(item.get("subreddit") or item.get("subreddit_name") or "")
            # Source-context guard: a keyword search for "ankara" (the NG fabric)
            # returns posts from r/ankara (the Turkish city). Drop foreign-geo
            # subs here, by source, before they get the collision-prone NG topic.
            # The geo-blocklist cannot catch these because the post often has no
            # Turkish-specific token, only the legitimate word "ankara".
            if sub.strip().lower().removeprefix("r/") in exclude_subs:
                self.logger.debug(
                    "Reddit keyword '%s': dropped r/%s (foreign-geo sub, source-context guard)",
                    keyword,
                    sub,
                )
                continue
            row = self._normalise_post(item, sub, query_term=f"search:{keyword}")
            if row is not None:
                rows.append(row)
        return rows, units

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _safe_request(self, path: str, params: dict, context: str) -> dict | list | None:
        """HTTP GET wrapper that handles EnsembleData status sentinels.

        Returns the parsed JSON payload on success, None on any 4xx/5xx
        the connector should treat as "skip this call". Flips the shared
        Ensemble quota flag on 493 (auth) and 495 (quota) so the rest of
        the pipeline (Reddit + Ensemble) stops calling the vendor for
        the run. A run of non-404 4xx responses (a systemic contract break)
        trips ``self._circuit_open`` so the caller's phase loops stop.
        """
        url = f"{self.BASE_URL}{path}"
        safe_url = self._safe_url(url)
        self.logger.info("Reddit request: %s context=%s", safe_url, context)
        try:
            resp = self._request("GET", url, params=params)
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == ENSEMBLE_QUOTA_STATUS:
                self.logger.error("EnsembleData quota exhausted (HTTP 495) on reddit %s", safe_url)
                EnsembleConnector._global_quota_exhausted = True
                return None
            if status == ENSEMBLE_AUTH_STATUS:
                self.logger.error(
                    "EnsembleData auth/billing error (HTTP 493) on reddit %s", safe_url
                )
                EnsembleConnector._global_quota_exhausted = True
                return None
            if status == 404:
                # Dead subreddit / dead post id. Mark and skip future calls.
                # 404 is expected (deleted/misspelled sub), not a contract
                # break, so it does NOT count toward the consecutive-4xx breaker.
                marker = params.get("name") or params.get("id") or ""
                if marker:
                    self._dead_subreddits.add(str(marker))
                self.logger.info("Reddit 404 on %s context=%s; marking as dead", safe_url, context)
                return None
            if status is not None and 400 <= status < 500:
                # A systemic vendor contract break (e.g. 422 on every call after
                # a param-shape change) would otherwise burn one estimated unit
                # per sub for the whole pool. Count consecutive non-404 4xx and
                # trip the breaker after the threshold, logging a single error.
                self._record_4xx(status, context)
                return None
            # 5xx (after retry-adapter exhaustion) or an unknown status: skip the
            # call but do not count it toward the 4xx breaker.
            self.logger.warning(
                "Reddit HTTP %s on %s context=%s; skipping", status, safe_url, context
            )
            return None
        except requests.exceptions.RequestException as exc:
            self.logger.warning("Reddit network error on %s: %s", safe_url, str(exc)[:200])
            return None
        # Successful HTTP call: reset the consecutive-4xx counter so a single
        # transient 4xx between good calls never trips the breaker.
        self._consecutive_4xx = 0
        try:
            return resp.json()
        except ValueError:
            self.logger.warning("Reddit non-JSON response from %s", safe_url)
            return None

    def _record_4xx(self, status: int, context: str) -> None:
        """Tally a non-404 4xx and open the circuit once the run hits the limit.

        Logs one ``error`` when the breaker trips (so a systemic break surfaces
        as a single line, not 60+ warnings) and a ``warning`` per skipped call
        below the threshold.
        """
        self._consecutive_4xx += 1
        if self._consecutive_4xx >= DEFAULT_CONSECUTIVE_4XX_LIMIT:
            if not self._circuit_open:
                self.logger.error(
                    "Reddit circuit breaker open: %d consecutive non-404 4xx "
                    "(last HTTP %s context=%s); aborting remaining reddit phases "
                    "for market=%s",
                    self._consecutive_4xx,
                    status,
                    context,
                    self.market,
                )
            self._circuit_open = True
            return
        self.logger.warning(
            "Reddit HTTP %s context=%s; skipping (consecutive 4xx=%d)",
            status,
            context,
            self._consecutive_4xx,
        )

    @staticmethod
    def _safe_url(url: str) -> str:
        """Strip querystring from URL so the API token never lands in logs."""
        try:
            return urlparse(url)._replace(query="").geturl()
        except Exception:
            return url.split("?", 1)[0]

    # ------------------------------------------------------------------
    # Payload normalisation
    # ------------------------------------------------------------------

    @classmethod
    def _extract_items(cls, payload: Any) -> list:
        """Return the list of post/comment dicts from a vendor response.

        Mirrors the same priority order EnsembleConnector uses. Handles
        both flat-list payloads and the more common {data: [...]} shape.
        """
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ("data", "posts", "results", "items", "children", "comments"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                return value
        # Some Reddit-style wrappers nest under {data: {children: [...]}} or
        # {data: {comments: [...]}} (EnsembleData /reddit/post/comments).
        nested = payload.get("data")
        if isinstance(nested, dict):
            for key in ("children", "posts", "comments", "results", "items"):
                value = nested.get(key)
                if isinstance(value, list) and value:
                    # Reddit's classic API wraps each item in {kind, data}.
                    out = []
                    for v in value:
                        if isinstance(v, dict) and "data" in v and isinstance(v["data"], dict):
                            out.append(v["data"])
                        else:
                            out.append(v)
                    return out
        return []

    @staticmethod
    def _extract_units_charged(payload: Any) -> int:
        if not isinstance(payload, dict):
            return 0
        for key in ("units_charged", "cost_units", "units"):
            value = payload.get(key)
            if isinstance(value, int | float):
                return int(value)
        return 0

    @staticmethod
    def _safe_str(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    @staticmethod
    def _safe_int(value: Any) -> int:
        if value is None or value == "":
            return 0
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _parse_created(raw: Any):
        """Reddit posts carry created_utc as a unix epoch float. Return a
        tz-aware UTC datetime or None so the raw_content TIMESTAMP load
        never sees a mixed-type Series.
        """
        if raw is None or raw == "":
            return None
        try:
            return datetime.fromtimestamp(int(float(raw)), tz=UTC)
        except (OSError, ValueError, OverflowError, TypeError):
            return None

    def _normalise_post(self, item: dict, subreddit: str, query_term: str) -> dict[str, Any] | None:
        """Map a Reddit post dict to the 16-column RAW schema.

        Special handling:
        - subreddit name prepended to text as [r/<sub>] so the topic
          classifier picks up the community hint without a schema change.
        - score (upvotes) -> likes, num_comments -> comments, views=0,
          shares=0 (Reddit does not expose either).
        - permalink resolved to full https URL when absent.
        - _reddit_post_id is a scratch column dropped by the caller after
          cross-sort dedup.
        """
        post_id = self._safe_str(item.get("id") or item.get("post_id") or "")
        if not post_id:
            return None
        title = self._safe_str(item.get("title") or "")
        body = self._safe_str(item.get("selftext") or item.get("body") or "")
        sub = self._safe_str(
            subreddit
            or item.get("subreddit")
            or item.get("subreddit_name_prefixed", "").removeprefix("r/")
        )
        url = self._safe_str(item.get("url") or "")
        permalink = self._safe_str(item.get("permalink") or "")
        if not url and permalink:
            url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
        if not url and post_id:
            url = f"https://www.reddit.com/comments/{post_id}"
        # Author may be "[deleted]" or a username. Keep as plain str.
        author = self._safe_str(
            item.get("author")
            or item.get("author_name")
            or (item.get("author_data", {}) or {}).get("name", "")
        )
        text_parts = []
        if sub:
            text_parts.append(f"[r/{sub}]")
        if body:
            text_parts.append(body)
        text = " ".join(text_parts).strip()
        return {
            "source": "Reddit",
            "platform": self.PLATFORM,
            "market": self.market,
            "content_type": "reddit_post",
            "query_group": f"reddit_{sub}" if sub else "reddit",
            "query_term": query_term,
            "author_name": author,
            "author_handle": author,
            "title": title,
            "text": text,
            "url": url,
            "published_at": self._parse_created(item.get("created_utc") or item.get("created")),
            "views": 0.0,
            "likes": float(self._safe_int(item.get("score") or item.get("ups") or 0)),
            "comments": float(
                self._safe_int(item.get("num_comments") or item.get("comment_count") or 0)
            ),
            "shares": 0.0,
            "_reddit_post_id": post_id,
        }

    def _normalise_comment(
        self, item: dict, subreddit: str, parent_post_id: str
    ) -> dict[str, Any] | None:
        """Map a Reddit comment dict to the 16-column RAW schema.

        Comments share the same row shape as posts so downstream scoring
        does not need to branch. content_type=reddit_comment distinguishes
        them; query_group encodes the parent post for traceability.
        """
        cid = self._safe_str(item.get("id") or item.get("comment_id") or "")
        body = self._safe_str(item.get("body") or item.get("text") or "")
        if not body:
            return None
        author = self._safe_str(item.get("author") or item.get("author_name") or "")
        permalink = self._safe_str(item.get("permalink") or "")
        if permalink.startswith("/"):
            url = f"https://www.reddit.com{permalink}"
        elif permalink:
            url = permalink
        elif parent_post_id and cid:
            url = f"https://www.reddit.com/comments/{parent_post_id}/_/{cid}"
        else:
            url = ""
        text_parts = []
        if subreddit:
            text_parts.append(f"[r/{subreddit}]")
        text_parts.append(body)
        text = " ".join(text_parts).strip()
        return {
            "source": "Reddit",
            "platform": self.PLATFORM,
            "market": self.market,
            "content_type": "reddit_comment",
            "query_group": f"reddit_{subreddit}" if subreddit else "reddit",
            "query_term": f"comments_on:{parent_post_id}",
            "author_name": author,
            "author_handle": author,
            "title": "",
            "text": text,
            "url": url,
            "published_at": self._parse_created(item.get("created_utc") or item.get("created")),
            "views": 0.0,
            "likes": float(self._safe_int(item.get("score") or item.get("ups") or 0)),
            "comments": 0.0,
            "shares": 0.0,
        }

    # ------------------------------------------------------------------
    # Finalisation
    # ------------------------------------------------------------------

    def _finalise(self, rows: list[dict[str, Any]]) -> pd.DataFrame:
        if not rows:
            return self.empty_dataframe()
        df = pd.DataFrame(rows)
        # URL-level dedup catches the same post showing up under both
        # /subreddit/posts and /keyword/search.
        if "url" in df.columns:
            non_empty = df["url"].fillna("").astype(str) != ""
            if non_empty.any():
                df = pd.concat(
                    [df[~non_empty], df[non_empty].drop_duplicates(subset=["url"])],
                    ignore_index=True,
                )
        # reindex to RAW_COLUMNS so non-applicable fields (v2tone, etc.)
        # get filled instead of raising KeyError on a future schema bump.
        return df.reindex(columns=list(self.empty_dataframe().columns), fill_value="")
