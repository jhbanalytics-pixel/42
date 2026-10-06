"""RSS feed connector.

Port source: trends-mvp/trends-free-mvp/src/connectors/rss_connector.py (24 lines).
Reads feeds from configs/sources.yaml per market.
"""

import email.utils
from datetime import UTC, datetime

import feedparser
import pandas as pd
import requests

from src.ingestion.connectors.base import RAW_COLUMNS, BaseConnector
from src.utils.config_loader import load_sources


class RSSConnector(BaseConnector):
    """Fetches articles from RSS feeds for a specific market."""

    SOURCE_NAME = "rss"
    PLATFORM = "web"
    MAX_ENTRIES_PER_FEED = 50
    # Polite-bot UA: declares the project and a contact URL so publishers
    # can trace traffic back to us. Default feedparser/requests UAs get
    # 403'd by Cloudflare-fronted publisher feeds (News24, Premium Times,
    # IOL, Guardian Nigeria). A real-looking browser UA would work too
    # but pretending to be a browser is hostile to the publisher's bot
    # policy. This UA is bot-polite per RFC 9110 §10.1.5 conventions:
    # token/version + compatible parenthetical + URL.
    USER_AGENT = (
        "Mozilla/5.0 (compatible; OgilvyTrendsEngineV2/1.0; "
        "+https://github.com/jhbanalytics-pixel/trends-engine-v2)"
    )

    def fetch(self, query_group: str = "news") -> pd.DataFrame:
        """Fetch articles from all RSS feeds configured for this market.

        Reads market-specific feeds from configs/sources.yaml. Returns one
        row per article, normalised to the canonical RAW_COLUMNS schema.
        """
        sources = load_sources()
        rss_config = sources.get("rss_feeds", {})

        feeds = list(rss_config.get(self.market, []))

        if not feeds:
            self.logger.warning("No RSS feeds configured for market=%s", self.market)
            return self.empty_dataframe()

        rows = []
        headers = {"User-Agent": self.USER_AGENT}
        for feed_cfg in feeds:
            url = feed_cfg.get("url")
            name = feed_cfg.get("name", url)
            # A config entry missing url would raise KeyError here, outside the
            # per-feed try below, so safe_fetch would swallow it and the whole
            # market would return zero rows. Skip the malformed entry instead.
            if not url:
                self.logger.warning("RSS feed entry missing url, skipping (name=%s)", name)
                continue
            try:
                response = self._session.get(url, headers=headers, timeout=10)
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                msg = f"rss feed {name}: {str(e)[:200]}"
                self.logger.error("Failed to fetch feed %s: %s", name, str(e)[:200])
                self._fetch_failures.append(msg)
                continue
            try:
                # Pass agent through to feedparser too. feedparser itself
                # only uses it for a direct URL fetch (which we don't do
                # since the session call already happened), but threading
                # it through keeps the contract self-consistent if a
                # future caller passes a URL instead of bytes.
                parsed = feedparser.parse(response.content, agent=self.USER_AGENT)
                if parsed.bozo and not parsed.entries:
                    self.logger.warning("Feed parse error for %s: %s", name, parsed.bozo_exception)
                    continue
                for entry in parsed.entries[: self.MAX_ENTRIES_PER_FEED]:
                    rows.append(self._normalise_entry(entry, name, query_group))
            except Exception as e:
                self.logger.error("Failed to parse feed %s: %s", name, str(e)[:200])
                continue

        if not rows:
            return self.empty_dataframe()

        df = pd.DataFrame(rows)
        # Deduplicate by URL
        df = df.drop_duplicates(subset=["url"])
        # Reindex to the canonical RAW schema so the populated frame matches
        # the empty-path frame from empty_dataframe(). The GDELT GKG columns
        # (v2tone, v2persons, v2orgs, v2locations) fill as empty strings, the
        # same pattern brand24 and reddit use, keeping the column count stable
        # across runs for the downstream concat and BigQuery load.
        return df.reindex(columns=list(RAW_COLUMNS), fill_value="")

    def _normalise_entry(self, entry, feed_name: str, query_group: str) -> dict:
        """Normalise a feedparser entry to the core BaseConnector fields.

        Emits the 16 non-GDELT keys; fetch() reindexes the frame to the full
        RAW_COLUMNS schema, filling the GDELT v2* columns as empty strings.
        """
        raw_date = entry.get("published") or entry.get("updated") or ""
        published_at = self._parse_date(raw_date)

        # Pull category / tag labels so the topic classifier downstream can
        # match on them. feedparser exposes both <category> elements (RSS)
        # and atom:category (Atom) under `entry.tags`, each a dict with a
        # `term` key. Lifestyle RSS feeds (OkayAfrica, TimesLIVE Lifestyle,
        # Pulse Live KE) tag posts like {"term": "amapiano"} or
        # {"term": "Afrobeats"} which the regex classifier picks up as
        # market topic groups. Appending to text is cheaper than a schema
        # add and matches the existing slang detection pipeline.
        category_terms = self._extract_categories(entry)
        # Author: feedparser falls back through entry.author -> dc:creator ->
        # author_detail.name. MVP only read entry.get("author") which misses
        # Atom feeds (OkayAfrica) and feeds using dc:creator namespace
        # (TechCentral, Premium Times). _resolve_author covers all three.
        author_name = self._resolve_author(entry)

        text_parts = [entry.get("summary", "") or ""]
        if category_terms:
            text_parts.append(" ".join(category_terms))
        text = " ".join(p for p in text_parts if p).strip()

        return {
            "source": feed_name,
            "platform": self.PLATFORM,
            "market": self.market,
            "content_type": "article",
            "query_group": query_group,
            "query_term": "",
            "author_name": author_name,
            "author_handle": "",
            "title": entry.get("title", ""),
            "text": text,
            "url": entry.get("link", ""),
            "published_at": published_at,
            "views": 0.0,
            "likes": 0.0,
            "comments": 0.0,
            "shares": 0.0,
        }

    @staticmethod
    def _extract_categories(entry) -> list[str]:
        """Pull category / tag labels from a feedparser entry.

        feedparser normalises <category> (RSS) and <atom:category> into
        `entry.tags`, a list of FeedParserDict objects with a `term` key
        carrying the label. Returns a deduped list of non-empty terms.
        """
        tags = entry.get("tags") or []
        terms: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            if isinstance(tag, dict):
                term = tag.get("term") or tag.get("label") or ""
            else:
                term = getattr(tag, "term", "") or getattr(tag, "label", "")
            term = str(term or "").strip()
            if term and term.lower() not in seen:
                seen.add(term.lower())
                terms.append(term)
        return terms

    @staticmethod
    def _resolve_author(entry) -> str:
        """Resolve byline across RSS, Atom, and Dublin Core namespaces.

        Order: entry.author -> entry.author_detail.name -> dc:creator. Tries
        each in turn so OkayAfrica (Atom), TechCentral (dc:creator), and
        Punch (RSS) all surface a populated author.
        """
        candidate = entry.get("author") or ""
        if candidate:
            return str(candidate).strip()
        author_detail = entry.get("author_detail") or {}
        if isinstance(author_detail, dict):
            name = author_detail.get("name") or ""
            if name:
                return str(name).strip()
        dc_creator = entry.get("dc_creator") or ""
        if dc_creator:
            return str(dc_creator).strip()
        return ""

    @staticmethod
    def _parse_date(raw: str):
        """Parse a feedparser date string to a UTC datetime, or return None."""
        if not raw:
            return None
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except (TypeError, ValueError):
            # RFC 2822 parse failed, fall through to ISO 8601 attempt.
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
