"""YouTube Data API v3 connector.

Port source: trends-mvp/trends-free-mvp/src/connectors/youtube_connector.py.
Uses raw REST calls (requests) instead of googleapiclient so that
HTTP responses can be mocked in tests via the `responses` library.

Quota accounting (YouTube Data API v3 daily budget = 10000 units):
  - search.list  = 100 units per call (one call per query term)
  - videos.list  = 1 unit per call (one batched call per market)

Daily budget math (3 markets, one run per day):
  3 markets x 4 search calls = 12 search.list + 3 videos.list
  = 12 x 100 + 3 x 1 = 1203 units per run.
  Headroom of ~8800 units/day for retries or an extra ad-hoc run.

Do NOT raise DEFAULT_MAX_CALLS_PER_RUN above 4 or DEFAULT_MAX_RESULTS
above 25 without recalculating the budget. search.list cost is fixed
at 100 units regardless of maxResults, so raising maxResults is free
quota-wise but returns diminishing signal past ~25 videos per term.

An in-memory cache (_SEARCH_CACHE) dedupes identical
(market, term, region_code) calls within a single process so a retry
or re-entrant pipeline run will not double-charge quota for the same
query. Cache is per-process and dies with the interpreter.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import requests

from src.ingestion.connectors.base import BaseConnector
from src.utils.config_loader import load_sources
from src.utils.secrets import get_secret

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
COMMENT_THREADS_URL = "https://www.googleapis.com/youtube/v3/commentThreads"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"

# commentThreads.list cost: 1 unit per call. Pulls top-level comments
# (snippet.topLevelComment.snippet.textDisplay) for a videoId. Capped at
# DEFAULT_COMMENT_VIDEOS_PER_RUN videos per market so 3 markets x 5 calls
# = 15 units/day against 8800 headroom. Comments are concatenated into
# the parent row's text so the topic classifier + slang detector pick up
# audience reactions (a major Gen Z signal vs. just title + description).
DEFAULT_COMMENT_VIDEOS_PER_RUN = 5
DEFAULT_COMMENTS_PER_VIDEO = 20

REGION_CODE_MAP = {"za": "ZA", "ng": "NG", "ke": "KE"}

DEFAULT_MAX_CALLS_PER_RUN = 4
DEFAULT_MAX_RESULTS = 25
PUBLISHED_AFTER_DAYS = 7

# videos.list with chart=mostPopular returns the platform's organic
# regional trending feed. 1 unit per call regardless of maxResults (capped
# at 50 by the API). 3 markets x 1 unit = 3 units/day, trivial against
# 10K quota. Endpoint label baked into query_group so the orchestrator
# can route these distinctly from search.list rows downstream.
TRENDING_QUERY_GROUP = "youtube_trending"
TRENDING_MAX_RESULTS = 50
# search.list rows carry this provenance label. Previously the default was
# blank, which build_raw_row (run_rss_now.py) rewrites to "news", lumping
# YouTube keyword-search rows in with RSS news and mislabeling their source
# stream. A distinct label keeps them separable, matching the trending and
# playlist streams which already self-label.
SEARCH_QUERY_GROUP = "youtube_search"

# Wave 2 playlistItems channel-deep-dive endpoint. For each tier_1 creator
# with a declared youtube_handle, resolve @handle -> channel.uploads playlist
# (1 unit) and fetch latest DEFAULT_PLAYLIST_ITEMS_PER_CREATOR uploads from
# that playlist (1 unit). Budget per market = creators_count * 2 units; with
# ~4 tier_1 YouTube creators per market across 3 markets = ~24 units/day,
# trivial against 10K quota. Handle -> uploads playlist id is cached per
# process so repeat runs in the same interpreter skip the channels.list
# call entirely. Default truncation matches the description-text cap used
# elsewhere in the schema.
PLAYLIST_ITEMS_QUERY_GROUP = "youtube_playlist_items"
PLAYLIST_ITEMS_CONTENT_TYPE = "youtube_channel_upload"
DEFAULT_PLAYLIST_ITEMS_PER_CREATOR = 20
PLAYLIST_DESCRIPTION_MAX_CHARS = 4000
# commentThreads enrichment concatenates up to 20 comments (each up to ~10k
# chars) into a row's text. Uncapped, one video could produce a ~200k-char row
# that bloats raw_content and the topic-classifier / embedding haystack. Cap the
# enriched text the same way the playlist description path is capped.
COMMENT_ENRICHED_TEXT_MAX_CHARS = 4000

# YouTube Data API v3 videos.list rejects requests with more than 50 ids
# in the ``id`` param. Chunk batched stats calls accordingly.
VIDEOS_LIST_MAX_IDS = 50

# Per-process cache of search.list responses keyed by (market, term, region, published_after).
# Prevents double-charging quota when the same (market, term) is fetched twice in one
# interpreter (e.g. pipeline retry, pytest re-entry). Cleared at process exit. A
# daily cron sees at most ~12 entries per run and the process exits, so the
# cap is defensive for future long-lived deployments (Cloud Run, server mode)
# where an unbounded dict would leak memory across many runs.
_SEARCH_CACHE_MAX_ENTRIES = 256
_SEARCH_CACHE: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}

# Per-process cache of @handle -> uploads_playlist_id resolutions. Keyed by
# the normalised handle (lower-case, leading "@" preserved). Value None means
# the handle was probed and returned no items (so callers can short-circuit
# without re-charging quota for a confirmed-miss handle). Cleared via the
# same conftest hook as _SEARCH_CACHE.
_HANDLE_CACHE: dict[str, str | None] = {}


def _cache_put(key: tuple[str, str, str, str], value: list[dict[str, Any]]) -> None:
    """Insert into _SEARCH_CACHE with a simple FIFO cap.

    When the cap is hit, drop the oldest entry (Python dicts preserve insertion
    order, so next(iter(...)) gives us the oldest key). Keeps the cache bounded
    without pulling in functools.lru_cache (which would wrap the method and
    lose the ability to clear via the existing `_SEARCH_CACHE.clear()` hook in
    tests/conftest.py).
    """
    if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX_ENTRIES:
        oldest = next(iter(_SEARCH_CACHE))
        del _SEARCH_CACHE[oldest]
    _SEARCH_CACHE[key] = value


class YouTubeConnector(BaseConnector):
    """Fetches YouTube search results for configured query terms per market."""

    SOURCE_NAME = "youtube"
    PLATFORM = "youtube"
    RATE_LIMIT_DELAY = 0.0  # YouTube quota is per-day, no per-second limit

    def fetch(self, query_group: str = SEARCH_QUERY_GROUP) -> pd.DataFrame:
        """Fetch YouTube search results for this connector's market.

        Reads query terms from configs/sources.yaml youtube_queries.<market>.
        Issues one search.list call per term (capped by max_calls_per_run)
        then one batched videos.list call for statistics.
        """
        api_key = get_secret("YOUTUBE_API_KEY")
        if not api_key:
            self.logger.warning(
                "YOUTUBE_API_KEY missing, returning empty DataFrame for market=%s",
                self.market,
            )
            return self.empty_dataframe()

        sources = load_sources()
        yt_config = sources.get("youtube_queries", {}).get(self.market, {})

        region_code = yt_config.get("region_code") or REGION_CODE_MAP.get(
            self.market, self.market.upper()
        )
        query_terms = list(yt_config.get("queries", []))
        max_calls = int(yt_config.get("max_calls_per_run", DEFAULT_MAX_CALLS_PER_RUN))
        # Gating flag for the regional mostPopular chart. Off by default so
        # legacy tests that only register one VIDEOS_URL stub keep passing.
        # Production sources.yaml flips trending_enabled: true per market.
        trending_enabled = bool(yt_config.get("trending_enabled", False))
        # Phase 2 (27 May 2026): opt-in commentThreads enrichment. When on,
        # the top DEFAULT_COMMENT_VIDEOS_PER_RUN search-result rows (ranked
        # by viewCount post-stats merge) each fire one commentThreads.list
        # call. Top comments are concatenated into the row's text so the
        # classifier + slang detector pick up audience reactions. 1 unit
        # per call; 5 videos x 3 markets = 15 units/day. Off by default
        # so legacy test stubs that don't mock COMMENT_THREADS_URL keep
        # passing.
        comment_threads_enabled = bool(yt_config.get("comment_threads_enabled", False))
        comment_videos_per_run = int(
            yt_config.get("comment_videos_per_run", DEFAULT_COMMENT_VIDEOS_PER_RUN)
        )
        # Wave 2 playlistItems channel-deep-dive. Off by default so legacy
        # tests that only register search + videos stubs keep passing.
        # Production sources.yaml flips playlist_items_enabled: true per
        # market once the creators YAML carries youtube_handle entries.
        playlist_items_enabled = bool(yt_config.get("playlist_items_enabled", False))
        playlist_items_per_creator = int(
            yt_config.get("playlist_items_per_creator", DEFAULT_PLAYLIST_ITEMS_PER_CREATOR)
        )
        playlist_creator_handles = list(yt_config.get("playlist_creator_handles", []))

        # No search terms is fine when playlistItems is enabled and creator
        # handles are configured. Without either, nothing to fetch.
        if not query_terms and not (playlist_items_enabled and playlist_creator_handles):
            self.logger.warning("No YouTube queries configured for market=%s", self.market)
            return self.empty_dataframe()

        capped_terms = query_terms[:max_calls]
        # Floor to midnight UTC before subtracting the lookback window so the
        # value is stable for the whole day. The cache key includes
        # published_after; computing it from datetime.now at second resolution
        # minted a fresh key on any re-entrant run more than one second later,
        # re-charging 100 quota units per term and breaking the
        # retry-without-double-charge contract. Day granularity keeps the
        # 7-day window intact while pinning the key within a calendar day.
        midnight_utc = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        published_after = (midnight_utc - timedelta(days=PUBLISHED_AFTER_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        rows: list[dict[str, Any]] = []
        quota_exceeded = False

        for term in capped_terms:
            if quota_exceeded:
                break
            try:
                items = self._search(api_key, term, region_code, published_after)
            except requests.exceptions.HTTPError as e:
                if self._is_quota_error(e):
                    self.logger.error(
                        "YouTube quota exceeded on search for term=%r market=%s",
                        term,
                        self.market,
                    )
                    quota_exceeded = True
                    break
                self.logger.error(
                    "YouTube search failed for term=%r market=%s: %s",
                    term,
                    self.market,
                    str(e)[:200],
                )
                continue
            except requests.exceptions.RequestException as e:
                self.logger.error(
                    "YouTube search request error for term=%r: %s",
                    term,
                    str(e)[:200],
                )
                continue

            for item in items:
                row = self._snippet_to_row(item, term, query_group)
                if row is not None:
                    rows.append(row)

        # When zero search rows landed and no other endpoints are enabled,
        # short-circuit. With trending or playlistItems enabled the run
        # should still produce rows from those endpoints.
        if not rows and not trending_enabled and not playlist_items_enabled:
            return self.empty_dataframe()

        # Deduplicate on videoId before the stats call.
        seen: set[str] = set()
        unique_rows: list[dict[str, Any]] = []
        for row in rows:
            vid = row["_video_id"]
            if vid in seen:
                continue
            seen.add(vid)
            unique_rows.append(row)

        # One batched stats call for all unique videoIds.
        if not quota_exceeded and unique_rows:
            video_ids = [r["_video_id"] for r in unique_rows]
            try:
                stats_by_id = self._fetch_statistics(api_key, video_ids)
            except requests.exceptions.HTTPError as e:
                if self._is_quota_error(e):
                    self.logger.error(
                        "YouTube quota exceeded on videos.list for market=%s",
                        self.market,
                    )
                    # Short-circuit the downstream endpoints (commentThreads,
                    # trending, channels, playlistItems). Without this flag they
                    # all fire and 403 once the daily quota is gone.
                    quota_exceeded = True
                else:
                    self.logger.error(
                        "YouTube videos.list failed for market=%s: %s",
                        self.market,
                        str(e)[:200],
                    )
                stats_by_id = {}
            except requests.exceptions.RequestException as e:
                self.logger.error(
                    "YouTube videos.list request error market=%s: %s",
                    self.market,
                    str(e)[:200],
                )
                stats_by_id = {}

            for row in unique_rows:
                stats = stats_by_id.get(row["_video_id"], {})
                row["views"] = self._as_float(stats.get("viewCount"))
                row["likes"] = self._as_float(stats.get("likeCount"))
                row["comments"] = self._as_float(stats.get("commentCount"))
                category_id = str(stats.get("categoryId") or "").strip()
                if category_id:
                    # Encode as content_type so scoring can look it up without
                    # a schema change. Example: "video/10" for Music.
                    row["content_type"] = f"video/{category_id}"

        # Phase 2 commentThreads enrichment. Pull top-level comments for the
        # highest-view search-result videos and append them to the row's
        # text so downstream classifier + slang detection see audience
        # reactions, not just creator-authored description. Errors swallow
        # per-row to avoid breaking the whole market on a single bad video.
        if comment_threads_enabled and not quota_exceeded and unique_rows:
            ranked = sorted(
                unique_rows,
                key=lambda r: float(r.get("views") or 0.0),
                reverse=True,
            )[:comment_videos_per_run]
            for row in ranked:
                vid = row.get("_video_id")
                if not vid:
                    continue
                try:
                    comments = self._fetch_comment_thread(api_key, vid)
                except requests.exceptions.HTTPError as e:
                    if self._is_quota_error(e):
                        self.logger.error(
                            "YouTube quota exceeded on commentThreads for market=%s",
                            self.market,
                        )
                        break
                    # 403 commentsDisabled / 404 videoNotFound -> skip this video.
                    self.logger.info(
                        "commentThreads skipped vid=%s market=%s: %s",
                        vid,
                        self.market,
                        str(e)[:120],
                    )
                    continue
                except requests.exceptions.RequestException as e:
                    self.logger.warning(
                        "commentThreads request error vid=%s: %s", vid, str(e)[:120]
                    )
                    continue
                if comments:
                    existing = str(row.get("text") or "")
                    row["text"] = (existing + "\n\n" + " ".join(comments)).strip()[
                        :COMMENT_ENRICHED_TEXT_MAX_CHARS
                    ]

        # Drop internal helper fields.
        for row in unique_rows:
            row.pop("_video_id", None)

        # Regional trending feed. Free of search-quota cost (1 unit), runs
        # after search.list rows are stats-enriched so the seen set already
        # holds those videoIds and we avoid duplicating any video that
        # appeared via both a keyword search and the mostPopular chart.
        if trending_enabled and not quota_exceeded:
            try:
                trending_rows = self._fetch_trending(api_key, region_code, seen)
            except requests.exceptions.HTTPError as e:
                if self._is_quota_error(e):
                    self.logger.error(
                        "YouTube quota exceeded on trending for market=%s", self.market
                    )
                else:
                    self.logger.error(
                        "YouTube trending failed for market=%s: %s",
                        self.market,
                        str(e)[:200],
                    )
                trending_rows = []
            except requests.exceptions.RequestException as e:
                self.logger.error(
                    "YouTube trending request error for market=%s: %s",
                    self.market,
                    str(e)[:200],
                )
                trending_rows = []
            unique_rows.extend(trending_rows)

        # Wave 2 playlistItems channel-deep-dive. For each configured creator
        # handle, resolve @handle -> uploads playlist id (1 unit, cached per
        # process) then fetch latest N uploads from that playlist (1 unit).
        # Dedup against `seen` so a video already surfaced via search or
        # mostPopular is not re-added from the creator's uploads feed.
        if playlist_items_enabled and not quota_exceeded and playlist_creator_handles:
            for handle in playlist_creator_handles:
                try:
                    uploads_playlist_id = self._fetch_channel_uploads_playlist(api_key, handle)
                except requests.exceptions.HTTPError as e:
                    if self._is_quota_error(e):
                        self.logger.error(
                            "YouTube quota exceeded on channels.list for handle=%s market=%s",
                            handle,
                            self.market,
                        )
                        quota_exceeded = True
                        break
                    self.logger.info(
                        "channels.list skipped handle=%s market=%s: %s",
                        handle,
                        self.market,
                        str(e)[:120],
                    )
                    continue
                except requests.exceptions.RequestException as e:
                    self.logger.warning(
                        "channels.list request error handle=%s: %s", handle, str(e)[:120]
                    )
                    continue

                if not uploads_playlist_id:
                    # Cached miss or vendor returned no items. Skip silently.
                    continue

                try:
                    playlist_rows = self._fetch_playlist_items(
                        api_key, uploads_playlist_id, handle, playlist_items_per_creator, seen
                    )
                except requests.exceptions.HTTPError as e:
                    if self._is_quota_error(e):
                        self.logger.error(
                            "YouTube quota exceeded on playlistItems for handle=%s market=%s",
                            handle,
                            self.market,
                        )
                        quota_exceeded = True
                        break
                    self.logger.info(
                        "playlistItems skipped handle=%s market=%s: %s",
                        handle,
                        self.market,
                        str(e)[:120],
                    )
                    continue
                except requests.exceptions.RequestException as e:
                    self.logger.warning(
                        "playlistItems request error handle=%s: %s", handle, str(e)[:120]
                    )
                    continue

                unique_rows.extend(playlist_rows)

        df = pd.DataFrame(unique_rows, columns=list(self.empty_dataframe().columns))
        # Row builders omit the five GDELT v2/GCAM fields, so the forced columns
        # arrive as NaN; run_rss_now._s would stringify NaN to the literal
        # "nan" in raw_content. Empty-string them to honour the raw_content
        # "empty string elsewhere" contract for non-GDELT rows.
        for _col in ("v2tone", "v2persons", "v2orgs", "v2locations", "v2gcam"):
            df[_col] = df[_col].fillna("")
        return df

    def _search(
        self,
        api_key: str,
        term: str,
        region_code: str,
        published_after: str,
    ) -> list[dict[str, Any]]:
        """Call search.list once. Costs 100 quota units.

        Hits the per-process _SEARCH_CACHE first so a repeat call with the
        same (market, term, region, published_after) inside one run returns
        cached items instead of burning another 100 units. published_after is
        floored to midnight UTC by the caller, so a re-entrant run within the
        same calendar day reuses the cache instead of re-charging quota, while
        a new day mints a fresh key and the cache never serves stale results.
        """
        cache_key = (self.market, term, region_code, published_after)
        cached = _SEARCH_CACHE.get(cache_key)
        if cached is not None:
            self.logger.debug("YouTube search cache hit for market=%s term=%r", self.market, term)
            return cached

        params = {
            "q": term,
            "part": "snippet",
            "type": "video",
            "maxResults": DEFAULT_MAX_RESULTS,
            "regionCode": region_code,
            "publishedAfter": published_after,
            "relevanceLanguage": "en",
            "key": api_key,
        }
        resp = self._request("GET", SEARCH_URL, params=params)
        data = resp.json()
        # nextPageToken intentionally ignored (quota discipline).
        items = list(data.get("items", []))
        _cache_put(cache_key, items)
        return items

    def _fetch_comment_thread(self, api_key: str, video_id: str) -> list[str]:
        """Call commentThreads.list for a videoId. 1 quota unit.

        Returns a list of top-level comment text strings (up to
        DEFAULT_COMMENTS_PER_VIDEO). Caller concatenates these into the
        parent row text. Videos with comments disabled return an HTTP 403
        with reason commentsDisabled; the call site catches HTTPError and
        skips. Videos that 404 (private/deleted) are handled the same way.
        """
        params = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": DEFAULT_COMMENTS_PER_VIDEO,
            "order": "relevance",
            "textFormat": "plainText",
            "key": api_key,
        }
        resp = self._request("GET", COMMENT_THREADS_URL, params=params)
        data = resp.json()
        comments: list[str] = []
        for item in data.get("items", []) or []:
            top_snippet = ((item.get("snippet") or {}).get("topLevelComment") or {}).get(
                "snippet"
            ) or {}
            text = str(top_snippet.get("textDisplay") or "").strip()
            if text:
                comments.append(text)
        return comments

    def _fetch_trending(
        self, api_key: str, region_code: str, seen_video_ids: set[str]
    ) -> list[dict[str, Any]]:
        """Fetch the regional mostPopular chart. 1 quota unit per call.

        Returns rows already populated with statistics (the videos.list
        response carries `snippet` + `statistics` in one call). Skips any
        videoId already present in `seen_video_ids` to avoid duplicating a
        video that surfaced via both keyword search and the chart.

        Note: chart=mostPopular is published per-region by YouTube and
        refreshes daily; for SSA markets the chart skews to viral music
        videos and creator-uploaded vlogs, both useful Gen Z trend signals
        that keyword-search misses (search filters on recency + relevance,
        the chart filters on raw view velocity).
        """
        params = {
            "part": "snippet,statistics",
            "chart": "mostPopular",
            "regionCode": region_code,
            "maxResults": TRENDING_MAX_RESULTS,
            "key": api_key,
        }
        resp = self._request("GET", VIDEOS_URL, params=params)
        data = resp.json()
        rows: list[dict[str, Any]] = []
        for item in data.get("items", []):
            vid = item.get("id")
            if not vid or vid in seen_video_ids:
                continue
            seen_video_ids.add(vid)
            snippet = item.get("snippet", {}) or {}
            stats = item.get("statistics", {}) or {}
            raw_published = snippet.get("publishedAt") or None
            published_at = (
                pd.to_datetime(raw_published, utc=True, errors="coerce") if raw_published else None
            )
            if published_at is not None and pd.isna(published_at):
                published_at = None
            category_id = str(snippet.get("categoryId") or "").strip()
            content_type = f"video/{category_id}" if category_id else "video"
            rows.append(
                {
                    "source": snippet.get("channelTitle", "") or "youtube",
                    "platform": self.PLATFORM,
                    "market": self.market,
                    "content_type": content_type,
                    "query_group": TRENDING_QUERY_GROUP,
                    "query_term": "mostPopular",
                    "author_name": snippet.get("channelTitle", ""),
                    "author_handle": snippet.get("channelId", ""),
                    "title": snippet.get("title", ""),
                    "text": snippet.get("description", ""),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "published_at": published_at,
                    "views": self._as_float(stats.get("viewCount")),
                    "likes": self._as_float(stats.get("likeCount")),
                    "comments": self._as_float(stats.get("commentCount")),
                    "shares": 0.0,
                }
            )
        return rows

    def _fetch_channel_uploads_playlist(self, api_key: str, handle: str) -> str | None:
        """Resolve a @handle to its channel's uploads playlist id. 1 quota unit.

        Hits a per-process cache so repeated calls for the same handle within
        the same interpreter return the cached id without burning quota. An
        empty `items` array (handle not found) is cached as None so a typo
        in the watchlist costs one wasted unit per process, not per call.

        The API is case-insensitive on the handle param; we normalise to
        lower case for the cache key so "@TYLAOfficial" and "@tylaofficial"
        share one cache slot.
        """
        cache_key = handle.strip().lower()
        if cache_key in _HANDLE_CACHE:
            self.logger.debug("YouTube handle cache hit for handle=%s", handle)
            return _HANDLE_CACHE[cache_key]

        params = {
            "part": "contentDetails",
            "forHandle": handle,
            "key": api_key,
        }
        resp = self._request("GET", CHANNELS_URL, params=params)
        data = resp.json()
        items = data.get("items") or []
        if not items:
            _HANDLE_CACHE[cache_key] = None
            self.logger.info(
                "YouTube handle resolved to no channel: handle=%s market=%s",
                handle,
                self.market,
            )
            return None
        uploads = (
            ((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        ) or None
        _HANDLE_CACHE[cache_key] = uploads
        return uploads

    def _fetch_playlist_items(
        self,
        api_key: str,
        playlist_id: str,
        handle: str,
        max_results: int,
        seen_video_ids: set[str],
    ) -> list[dict[str, Any]]:
        """Fetch latest uploads from a channel uploads playlist. 1 quota unit per call.

        Returns normalised rows in the raw_content schema. videoId is read
        from `contentDetails.videoId` (canonical) and the publish timestamp
        from `contentDetails.videoPublishedAt` (true upload time, not
        playlist-add time). Description is truncated to PLAYLIST_DESCRIPTION_MAX_CHARS
        to match the field cap used elsewhere in the schema. Videos already
        present in `seen_video_ids` are skipped to avoid duplicating a video
        that surfaced via keyword search or the mostPopular chart.

        Engagement counters (views/likes/comments) are zero on these rows;
        a follow-up videos.list enrichment pass would cost 1 unit per batch
        of 50 and can be wired separately if scoring needs raw view counts.
        """
        capped = min(max(int(max_results), 0), 50)
        params = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": capped,
            "key": api_key,
        }
        resp = self._request("GET", PLAYLIST_ITEMS_URL, params=params)
        data = resp.json()
        rows: list[dict[str, Any]] = []
        for item in data.get("items", []) or []:
            content_details = item.get("contentDetails") or {}
            snippet = item.get("snippet") or {}
            vid = content_details.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
            if not vid or vid in seen_video_ids:
                continue
            seen_video_ids.add(vid)
            raw_published = (
                content_details.get("videoPublishedAt") or snippet.get("publishedAt") or None
            )
            published_at = (
                pd.to_datetime(raw_published, utc=True, errors="coerce") if raw_published else None
            )
            if published_at is not None and pd.isna(published_at):
                published_at = None
            channel_title = (
                snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle") or ""
            )
            description = str(snippet.get("description") or "")[:PLAYLIST_DESCRIPTION_MAX_CHARS]
            rows.append(
                {
                    "source": channel_title or "youtube",
                    "platform": self.PLATFORM,
                    "market": self.market,
                    "content_type": PLAYLIST_ITEMS_CONTENT_TYPE,
                    "query_group": PLAYLIST_ITEMS_QUERY_GROUP,
                    "query_term": channel_title or handle,
                    "author_name": channel_title,
                    "author_handle": handle,
                    "title": snippet.get("title", ""),
                    "text": description,
                    "url": f"https://youtu.be/{vid}",
                    "published_at": published_at,
                    "views": 0.0,
                    "likes": 0.0,
                    "comments": 0.0,
                    "shares": 0.0,
                }
            )
        return rows

    def _fetch_statistics(self, api_key: str, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Call videos.list in batches of <=50 ids. Each call costs 1 quota unit.

        videos.list rejects requests with more than 50 ids in the ``id`` param
        with HTTP 400. Search runs that return 80-100 unique videoIds per
        market would otherwise drop all stats enrichment for the whole
        market. Chunk into batches of VIDEOS_LIST_MAX_IDS so each call stays
        under the limit. Per-call cost is still 1 unit so a 100-id market
        spends 2 units instead of saturating quota.

        Pulls statistics plus snippet.categoryId so the scoring layer can
        down-weight commercial-music / gaming engagement and distinguish
        vlogs + news (high trend relevance) from evergreen viral content.
        """
        out: dict[str, dict[str, Any]] = {}
        for batch_start in range(0, len(video_ids), VIDEOS_LIST_MAX_IDS):
            batch = video_ids[batch_start : batch_start + VIDEOS_LIST_MAX_IDS]
            params = {
                "part": "statistics,snippet",
                "id": ",".join(batch),
                "key": api_key,
            }
            resp = self._request("GET", VIDEOS_URL, params=params)
            data = resp.json()
            for item in data.get("items", []):
                vid = item.get("id")
                if vid:
                    merged = dict(item.get("statistics", {}))
                    merged["categoryId"] = (item.get("snippet", {}) or {}).get("categoryId", "")
                    out[vid] = merged
        return out

    def _snippet_to_row(
        self, item: dict[str, Any], query_term: str, query_group: str
    ) -> dict[str, Any] | None:
        """Map a search.list item to the 16-column schema (plus a scratch _video_id)."""
        vid = (item.get("id") or {}).get("videoId")
        if not vid:
            return None
        snippet = item.get("snippet", {})
        raw_published = snippet.get("publishedAt") or None
        published_at = (
            pd.to_datetime(raw_published, utc=True, errors="coerce") if raw_published else None
        )
        if published_at is not None and pd.isna(published_at):
            published_at = None
        return {
            "source": snippet.get("channelTitle", "") or "youtube",
            "platform": self.PLATFORM,
            "market": self.market,
            "content_type": "video",
            "query_group": query_group,
            "query_term": query_term,
            "author_name": snippet.get("channelTitle", ""),
            "author_handle": snippet.get("channelId", ""),
            "title": snippet.get("title", ""),
            "text": snippet.get("description", ""),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "published_at": published_at,
            "views": 0,
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "_video_id": vid,
        }

    @staticmethod
    def _is_quota_error(err: requests.exceptions.HTTPError) -> bool:
        """Detect YouTube Data API v3 quotaExceeded / dailyLimitExceeded errors."""
        resp = err.response
        if resp is None or resp.status_code != 403:
            return False
        try:
            body = resp.json()
        except ValueError:
            return False
        errors = (body.get("error", {}) or {}).get("errors", []) or []
        for e in errors:
            reason = (e.get("reason") or "").lower()
            if reason in ("quotaexceeded", "dailylimitexceeded", "ratelimitexceeded"):
                return True
        return False

    @staticmethod
    def _as_float(value: Any) -> float:
        """Coerce a YouTube statistic (string or missing) to float, defaulting to 0.0.

        Returns float to match the raw_content FLOAT64 engagement columns and
        to stay consistent with Ensemble/GDELT/RSS which also emit float. The
        YouTube API returns these as strings (e.g. "1234"), hence the explicit
        cast.
        """
        if value is None or value == "":
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
