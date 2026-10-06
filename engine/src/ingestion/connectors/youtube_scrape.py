"""YouTube via yt-dlp: uncapped, no API key, free search-result ingestion.

The API connector (youtube.py) returns clean structured stats but is bounded by
the 10K/day quota (search.list costs 100 units, so ~100 searches/day total).
This connector uses yt-dlp's flat search extraction to pull many more videos per
term at zero quota, which widens the YouTube tap for free. It SUPPLEMENTS the API
connector, it does not replace it: flat extraction returns title, view count,
channel and url (enough for the topic classifier and the engagement signal),
while the API path keeps the deep per-video fields and comment enrichment.

Safe by construction. A per-term result cap and a per-run video ceiling bound
the wall-clock (yt-dlp scrapes, so it is slower than the API). Any per-term
yt-dlp error is recorded as a non-fatal _fetch_failure, so one dead term or a
transient block does not abort the market (see the fatal-vs-degradation split in
run_rss_now.log_pipeline_run). yt-dlp is imported lazily so a missing dependency
degrades to an empty frame plus a warning rather than an import crash.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.ingestion.connectors.base import RAW_COLUMNS, BaseConnector
from src.utils.config_loader import load_sources

# Videos requested per search term. Flat extraction is one network round trip per
# term regardless of this count, so a higher number is nearly free in wall-clock.
DEFAULT_SCRAPE_MAX_RESULTS = 30
# Terms processed per run. Bounds total wall-clock (one round trip per term).
DEFAULT_SCRAPE_MAX_TERMS = 25
# Hard ceiling on rows emitted per run so a runaway search cannot balloon memory.
DEFAULT_SCRAPE_MAX_ROWS = 3000
# Fallback chain when one player client returns empty entries on a datacenter IP.
DEFAULT_PLAYER_CLIENTS = ("android", "ios", "mweb")

SCRAPE_QUERY_GROUP = "youtube_scrape"
SCRAPE_CONTENT_TYPE = "video/scrape"


class YouTubeScrapeConnector(BaseConnector):
    """Uncapped YouTube search via yt-dlp. Zero quota, no API key.

    Reads the same query terms as the API connector
    (youtube_queries.<market>.queries) and is gated by
    youtube_queries.<market>.scrape_enabled, which is true on all three markets.
    Result depth and breadth come from scrape_max_results / scrape_max_terms per
    market.
    """

    SOURCE_NAME = "youtube_scrape"
    PLATFORM = "youtube"
    RATE_LIMIT_DELAY = 0.0

    def fetch(self, **kwargs: Any) -> pd.DataFrame:
        sources = load_sources()
        yt_config = sources.get("youtube_queries", {}).get(self.market, {}) or {}
        if not bool(yt_config.get("scrape_enabled", False)):
            return self.empty_dataframe()

        query_terms = list(yt_config.get("queries", []) or [])
        if not query_terms:
            return self.empty_dataframe()
        max_terms = int(yt_config.get("scrape_max_terms", DEFAULT_SCRAPE_MAX_TERMS))
        max_results = int(yt_config.get("scrape_max_results", DEFAULT_SCRAPE_MAX_RESULTS))
        max_rows = int(yt_config.get("scrape_max_rows", DEFAULT_SCRAPE_MAX_ROWS))
        configured_client = str(yt_config.get("scrape_player_client", "android"))
        player_clients = tuple(dict.fromkeys([configured_client, *DEFAULT_PLAYER_CLIENTS]))

        try:
            import yt_dlp
        except ImportError:
            self.logger.warning("yt_dlp not installed, youtube_scrape skipped for %s", self.market)
            self._fetch_failures.append("youtube_scrape: yt_dlp not installed")
            return self.empty_dataframe()

        ydl_opts_base = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "socket_timeout": 20,
            "ignoreerrors": True,
        }

        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for term in query_terms[:max_terms]:
            if len(rows) >= max_rows:
                break
            entries, term_err = self._search_term_entries(
                yt_dlp, ydl_opts_base, term, max_results, player_clients
            )
            if term_err is not None and not entries:
                self.logger.warning("youtube_scrape term %r failed: %s", term, str(term_err)[:200])
                self._fetch_failures.append(f"youtube_scrape term {term}: {str(term_err)[:200]}")
                continue
            if not entries:
                self._fetch_failures.append(
                    f"youtube_scrape term {term}: 0 entries after player_client fallback"
                )
                continue
            for entry in entries:
                if not entry or len(rows) >= max_rows:
                    continue
                vid = str(entry.get("id") or "")
                if vid and vid in seen_ids:
                    continue
                if vid:
                    seen_ids.add(vid)
                rows.append(self._to_row(entry, term))

        if not rows:
            return self.empty_dataframe()
        df = pd.DataFrame(rows)
        df["market"] = self.market
        return df.reindex(columns=list(RAW_COLUMNS), fill_value="")

    @staticmethod
    def _search_term_entries(
        yt_dlp: Any,
        ydl_opts_base: dict[str, Any],
        term: str,
        max_results: int,
        player_clients: tuple[str, ...],
    ) -> tuple[list[dict[str, Any]], Exception | None]:
        """Try each YouTube player client until one returns usable flat entries."""
        last_exc: Exception | None = None
        for client in player_clients:
            ydl_opts = {
                **ydl_opts_base,
                "extractor_args": {"youtube": {"player_client": [client]}},
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(f"ytsearch{max_results}:{term}", download=False)
                raw = (info or {}).get("entries", []) or []
                entries = [
                    e for e in raw if isinstance(e, dict) and (e.get("id") or e.get("title"))
                ]
                if entries:
                    return entries, None
            except Exception as exc:
                last_exc = exc
        return [], last_exc

    def _to_row(self, entry: dict[str, Any], term: str) -> dict[str, Any]:
        title = str(entry.get("title") or "")
        channel = str(entry.get("uploader") or entry.get("channel") or "")
        vid = str(entry.get("id") or "")
        url = str(entry.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else ""))
        return {
            # SOURCE_NAME, not channelTitle: run_rss_now and morning-check count
            # rows with source='youtube_scrape'. Channel stays on author_name.
            "source": self.SOURCE_NAME,
            "platform": self.PLATFORM,
            "market": self.market,
            "content_type": SCRAPE_CONTENT_TYPE,
            "query_group": SCRAPE_QUERY_GROUP,
            "query_term": term,
            "author_name": channel,
            "author_handle": str(entry.get("channel_id") or ""),
            "title": title,
            # Flat search has no description, so the title carries the classifiable
            # text. Views feed engagement; the API connector supplies the deep fields.
            "text": title,
            "url": url,
            # Flat search does not carry a reliable upload date; leave empty so the
            # published_at coercion downstream treats it as unknown, not epoch 0.
            "published_at": "",
            "views": self._as_float(entry.get("view_count")),
            "likes": self._as_float(entry.get("like_count")),
            "comments": self._as_float(entry.get("comment_count")),
            "shares": 0.0,
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
