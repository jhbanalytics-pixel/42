from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from email.message import Message
from html.parser import HTMLParser
from typing import Callable, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit

from core.collect.ids import canonical_url, post_id
from core.public_feeds.catalog import FeedSpec, confirmed_feeds


TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
USER_AGENT = "42-public-feed-reader/1.0"


class FeedReadError(ValueError):
    def __init__(self, feed_id: str, reason: str):
        self.feed_id = feed_id
        self.reason = reason
        super().__init__(f"{feed_id}: {reason}")


def _error(feed: FeedSpec, reason: str) -> FeedReadError:
    return FeedReadError(feed.feed_id, reason)


def _validate_feed(feed: FeedSpec) -> None:
    if not isinstance(feed, FeedSpec) or feed not in confirmed_feeds():
        feed_id = getattr(feed, "feed_id", "unknown")
        raise FeedReadError(str(feed_id), "feed is not a confirmed catalog entry")
    parts = urlsplit(feed.url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise _error(feed, "feed URL is not an anonymous HTTPS endpoint")


def _observed_at(feed: FeedSpec, fetched_at: datetime) -> str:
    if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise _error(feed, "fetched_at must be timezone-aware")
    return fetched_at.astimezone(timezone.utc).isoformat()


def _header(headers: Mapping[str, object], name: str) -> str:
    return next((str(value) for key, value in headers.items() if str(key).lower() == name.lower()), "")


def _is_html(headers: Mapping[str, object]) -> bool:
    value = _header(headers, "content-type").split(";", 1)[0].strip().lower()
    return value in {"text/html", "application/xhtml+xml"}


def _decode_html(feed: FeedSpec, headers: Mapping[str, object], body: bytes) -> str:
    content_type = _header(headers, "content-type")
    message = Message()
    message["content-type"] = content_type
    charset = message.get_content_charset() or "utf-8"
    try:
        return body.decode(charset)
    except (LookupError, UnicodeDecodeError) as exc:
        raise _error(feed, "HTML body has an unsupported or invalid character encoding") from exc


def _default_transport(feed: FeedSpec, *, timeout: int, max_bytes: int) -> tuple[int, dict, bytes]:
    import requests

    session = requests.Session()
    session.trust_env = False
    response = None
    try:
        response = session.get(
            feed.url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        status = response.status_code
        headers = dict(response.headers)
        if status != 200 or not _is_html(headers):
            return status, headers, b""
        declared_length = _header(headers, "content-length")
        if declared_length:
            try:
                declared_bytes = int(declared_length)
            except ValueError:
                declared_bytes = 0
            if declared_bytes > max_bytes:
                raise _error(feed, "response exceeds the 2 MiB body limit")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            if len(body) + len(chunk) > max_bytes:
                raise _error(feed, "response exceeds the 2 MiB body limit")
            body.extend(chunk)
        return status, headers, bytes(body)
    except requests.RequestException as exc:
        raise _error(feed, "HTTPS request failed") from exc
    finally:
        if response is not None:
            response.close()
        session.close()


_HIDDEN_TAGS = {"aside", "button", "canvas", "footer", "form", "iframe", "nav", "noscript", "script", "style",
                "svg", "template"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
              "track", "wbr"}
_NAV_TITLES = {"latest", "latest news", "top news", "news", "read more", "view all", "see all", "watch now",
               "listen live", "live stream"}
_TIME_ZONES = {"EAT": 3, "GMT": 0, "SAST": 2, "UTC": 0, "WAT": 1}


def _text(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _published_at(value: str | None, text: str) -> str | None:
    for candidate in (value or "", text):
        candidate = candidate.strip()
        if not re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", candidate):
            continue
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed.isoformat()
    match = re.fullmatch(r"(\d{1,2} [A-Za-z]+ \d{4}, \d{1,2}:\d{2}) \[([A-Z]+)\]", text.strip())
    if not match or match.group(2) not in _TIME_ZONES:
        return None
    try:
        parsed = datetime.strptime(match.group(1), "%d %B %Y, %H:%M")
    except ValueError:
        return None
    offset = timedelta(hours=_TIME_ZONES[match.group(2)])
    return parsed.replace(tzinfo=timezone(offset)).isoformat()


def _article_url(feed: FeedSpec, href: str | None) -> str | None:
    if not href:
        return None
    resolved = urljoin(feed.url, href.strip())
    parts = urlsplit(resolved)
    feed_host = (urlsplit(feed.url).hostname or "").lower().removeprefix("www.")
    target_host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.scheme.lower() != "https" or not target_host or target_host != feed_host:
        return None
    if parts.username or parts.password or parts.query:
        return None
    article_url = urlunsplit(parts._replace(fragment=""))
    return canonical_url(article_url)


# Legit Media sites put each headline in an article card link (class *article-card*__headline), not a heading.
_CARD_HEADLINE_FEEDS = frozenset({"ke_tuko", "ng_legit"})
_CARD_HEADLINE = re.compile(r"^(?:c-)?article-card[\w-]*__headline$")
_CARD_ARTICLE_PATH = re.compile(r"/\d{3,}-[^/]+/?$")


class _FeedHTMLParser(HTMLParser):
    def __init__(self, feed: FeedSpec):
        super().__init__(convert_charrefs=True)
        self.feed_spec = feed
        self.skipped: list[str] = []
        self.anchors: list[str | None] = []
        self.headings: list[dict] = []
        self.heading: dict | None = None
        self.published: dict[int, str] = {}
        self.time: dict | None = None
        self.rows: list[dict] = []
        self.row: dict | None = None
        self.cell: dict | None = None
        self.cell_labels: list[str | None] = []
        self.playlist_cards: list[dict] = []
        self.playlist_card: dict | None = None
        self.playlist_card_depth: int | None = None
        self.playlist_label_stack: list[tuple[str, str]] = []
        self.div_depth = 0
        self.thead_depth = 0
        self.pulse_article_depth = 0
        self.pulse_title: dict | None = None
        self.pulse_titles: list[dict] = []
        self.card_title: dict | None = None
        self.card_titles: list[dict] = []

    def _finish_heading(self) -> None:
        if self.heading is None:
            return
        self.heading["text"] = _text(self.heading.pop("parts"))
        self.headings.append(self.heading)
        self.heading = None

    def _finish_cell(self) -> None:
        if self.cell is None or self.row is None:
            return
        self.cell["text"] = _text(self.cell.pop("parts"))
        self.cell["labels"] = {key: _text(parts) for key, parts in self.cell.pop("label_parts").items()}
        self.row["cells"].append(self.cell)
        self.cell = None
        self.cell_labels = []

    def _finish_row(self) -> None:
        if self.row is None:
            return
        self._finish_cell()
        self.rows.append(self.row)
        self.row = None

    def _finish_playlist_card(self) -> None:
        if self.playlist_card is None:
            return
        labels = self.playlist_card["labels"]
        self.playlist_card.update({key: _text(value) for key, value in labels.items()})
        del self.playlist_card["labels"]
        self.playlist_cards.append(self.playlist_card)
        self.playlist_card = None
        self.playlist_card_depth = None
        self.playlist_label_stack = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs)
        if self.skipped:
            if tag not in _VOID_TAGS:
                self.skipped.append(tag)
            return
        classes = set((attrs.get("class") or "").lower().split())
        style = re.sub(r"\s+", "", (attrs.get("style") or "").lower())
        hidden = ("hidden" in attrs or (attrs.get("aria-hidden") or "").lower() == "true"
                  or "display:none" in style or "visibility:hidden" in style
                  or bool(classes & {"screen-reader-text", "sr-only", "visually-hidden"}))
        if tag in _HIDDEN_TAGS or hidden:
            if tag not in _VOID_TAGS:
                self.skipped.append(tag)
            return
        if self.feed_spec.feed_id == "ng_pulse" and tag == "article":
            self.pulse_article_depth += 1
        if tag == "div" and self.feed_spec.kind == "playlist":
            self.div_depth += 1
        if tag == "div" and self.feed_spec.kind == "playlist" and "track-info" in classes:
            self._finish_playlist_card()
            self.playlist_card = {
                "labels": {"title": [], "artist": [], "time_text": []},
                "href": None,
            }
            self.playlist_card_depth = self.div_depth
        if tag == "thead":
            self.thead_depth += 1
        if tag == "tr" and self.feed_spec.kind in {"chart", "playlist"}:
            self._finish_row()
            self.row = {"cells": [], "href": None, "time_text": "", "in_thead": self.thead_depth > 0}
        elif tag in {"td", "th"} and self.row is not None:
            self._finish_cell()
            self.cell = {"tag": tag, "parts": [], "label_parts": {}, "href": None}
        elif tag == "a":
            href = attrs.get("href")
            self.anchors.append(href)
            if (self.feed_spec.feed_id == "ng_pulse" and self.pulse_article_depth
                    and "font-accent" in classes):
                self.pulse_title = {"href": href, "parts": []}
            if (self.feed_spec.feed_id in _CARD_HEADLINE_FEEDS and self.card_title is None
                    and any(_CARD_HEADLINE.match(name) for name in classes)):
                self.card_title = {"href": href, "parts": []}
            if self.heading is not None and not self.heading.get("href"):
                self.heading["href"] = href
            if self.cell is not None and href and not self.cell.get("href"):
                self.cell["href"] = href
            if self.row is not None and href and not self.row.get("href"):
                self.row["href"] = href
            if self.playlist_card is not None and href and not self.playlist_card.get("href"):
                self.playlist_card["href"] = href
        elif re.fullmatch(r"h[1-6]", tag):
            self._finish_heading()
            self.heading = {"parts": [], "href": next((href for href in reversed(self.anchors) if href), None)}
        elif tag == "time":
            self.time = {
                "datetime": attrs.get("datetime"),
                "parts": [],
                "heading_index": len(self.headings) if self.heading is not None else len(self.headings) - 1,
            }
        elif tag == "p" and self.cell is not None:
            label = None
            if "title" in classes or "song" in classes or "track" in classes:
                label = "title"
            elif "artist" in classes or "artiste" in classes:
                label = "artist"
            elif "rank" in classes:
                label = "rank"
            self.cell_labels.append(label)
            if label:
                self.cell["label_parts"].setdefault(label, [])
        if self.playlist_card is not None:
            label = None
            if "track-title" in classes:
                label = "title"
            elif "track-artist" in classes:
                label = "artist"
            elif "track-time" in classes:
                label = "time_text"
            if label:
                self.playlist_label_stack.append((tag, label))

    def handle_endtag(self, tag: str) -> None:
        if self.skipped:
            for index in range(len(self.skipped) - 1, -1, -1):
                if self.skipped[index] == tag:
                    del self.skipped[index:]
                    break
            return
        for index in range(len(self.playlist_label_stack) - 1, -1, -1):
            if self.playlist_label_stack[index][0] == tag:
                del self.playlist_label_stack[index:]
                break
        if tag == "div" and self.playlist_card is not None and self.playlist_card_depth == self.div_depth:
            self._finish_playlist_card()
        if tag == "a":
            if self.pulse_title is not None:
                self.pulse_title["text"] = _text(self.pulse_title.pop("parts"))
                self.pulse_titles.append(self.pulse_title)
                self.pulse_title = None
            if self.card_title is not None:
                self.card_title["text"] = _text(self.card_title.pop("parts"))
                self.card_titles.append(self.card_title)
                self.card_title = None
            if self.anchors:
                self.anchors.pop()
        elif tag == "time" and self.time is not None:
            value = self.time.get("datetime")
            visible_text = _text(self.time.pop("parts"))
            index = self.time.get("heading_index", -1)
            published = _published_at(value, visible_text)
            if published and 0 <= index < len(self.headings):
                self.published.setdefault(index, published)
            if self.row is not None and self.feed_spec.kind == "playlist":
                self.row["time_text"] = visible_text
            self.time = None
        elif re.fullmatch(r"h[1-6]", tag):
            self._finish_heading()
        elif tag == "p" and self.cell_labels:
            self.cell_labels.pop()
        elif tag in {"td", "th"}:
            self._finish_cell()
        elif tag == "tr":
            self._finish_row()
        if tag == "thead" and self.thead_depth:
            self.thead_depth -= 1
        if tag == "div" and self.feed_spec.kind == "playlist" and self.div_depth:
            self.div_depth -= 1
        if self.feed_spec.feed_id == "ng_pulse" and tag == "article" and self.pulse_article_depth:
            self.pulse_article_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skipped:
            return
        if self.heading is not None:
            self.heading["parts"].append(data)
        if self.pulse_title is not None:
            self.pulse_title["parts"].append(data)
        if self.card_title is not None:
            self.card_title["parts"].append(data)
        if self.time is not None:
            self.time["parts"].append(data)
        if self.cell is not None:
            self.cell["parts"].append(data)
            label = next((value for value in reversed(self.cell_labels) if value), None)
            if label:
                self.cell["label_parts"][label].append(data)
        if self.playlist_card is not None and self.playlist_label_stack:
            self.playlist_card["labels"][self.playlist_label_stack[-1][1]].append(data)


class _MdundoHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards: list[dict] = []
        self.card: dict | None = None
        self.card_depth: int | None = None
        self.div_depth = 0
        self.labels: list[tuple[str, str]] = []
        self.skipped: list[str] = []

    def _finish_card(self) -> None:
        if self.card is None:
            return
        self.cards.append({key: _text(parts) for key, parts in self.card["parts"].items()})
        self.card = None
        self.card_depth = None
        self.labels = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs)
        if self.skipped:
            if tag not in _VOID_TAGS:
                self.skipped.append(tag)
            return
        classes = set((attrs.get("class") or "").lower().split())
        style = re.sub(r"\s+", "", (attrs.get("style") or "").lower())
        hidden = ("hidden" in attrs or (attrs.get("aria-hidden") or "").lower() == "true"
                  or "display:none" in style or "visibility:hidden" in style
                  or bool(classes & {"screen-reader-text", "sr-only", "visually-hidden"}))
        if tag in _HIDDEN_TAGS or hidden:
            if tag not in _VOID_TAGS:
                self.skipped.append(tag)
            return
        if tag == "div":
            self.div_depth += 1
            row_id = attrs.get("id") or ""
            if row_id.startswith("songlist-top-charts-ke-id--song-"):
                self._finish_card()
                self.card = {"parts": {"rank": [], "title": [], "artist": []}}
                self.card_depth = self.div_depth
        if self.card is None:
            return
        if tag == "span" and "md-playlist-song-item-number" in classes:
            self.labels.append((tag, "rank"))
        elif tag == "div" and "md-player-song-active-color" in classes:
            self.labels.append((tag, "title"))
        elif tag == "div" and {"pb-1", "text-xs", "text-slate-400"} <= classes:
            self.labels.append((tag, "artist"))

    def handle_endtag(self, tag: str) -> None:
        if self.skipped:
            for index in range(len(self.skipped) - 1, -1, -1):
                if self.skipped[index] == tag:
                    del self.skipped[index:]
                    break
            return
        for index in range(len(self.labels) - 1, -1, -1):
            if self.labels[index][0] == tag:
                del self.labels[index:]
                break
        if tag == "div" and self.card is not None and self.card_depth == self.div_depth:
            self._finish_card()
        if tag == "div" and self.div_depth:
            self.div_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skipped and self.card is not None and self.labels:
            self.card["parts"][self.labels[-1][1]].append(data)


def _base_entry(feed: FeedSpec, fetched_at: str, text: str, url: str | None, published_at: str | None) -> dict:
    return {
        "post_id": post_id("news", url=url) if url else None,
        "platform": "news",
        "url": url,
        "text": text,
        "published_at": published_at,
        "source_market": feed.market,
        "source_feed_id": feed.feed_id,
        "source_url": feed.url,
        "observed_at": fetched_at,
    }


def _news_entries(feed: FeedSpec, parser: _FeedHTMLParser, observed_at: str) -> list[dict]:
    entries: dict[str, dict] = {}
    for index, heading in enumerate(parser.headings):
        title = heading["text"]
        if not title or title.casefold() in _NAV_TITLES:
            continue
        url = _article_url(feed, heading.get("href"))
        if not url:
            continue
        entry = _base_entry(feed, observed_at, title, url, parser.published.get(index))
        if not entry["post_id"]:
            continue
        entry["kind"] = "news"
        existing = entries.get(entry["post_id"])
        if existing is None or (existing["published_at"] is None and entry["published_at"] is not None):
            entries[entry["post_id"]] = entry
    return list(entries.values())


def _pulse_entries(feed: FeedSpec, parser: _FeedHTMLParser, observed_at: str) -> list[dict]:
    entries: dict[str, dict] = {}
    for title_link in parser.pulse_titles:
        title = title_link["text"]
        if not title or title.casefold() in _NAV_TITLES:
            continue
        url = _article_url(feed, title_link.get("href"))
        if not url or not urlsplit(url).path.startswith("/story/"):
            continue
        entry = _base_entry(feed, observed_at, title, url, None)
        if not entry["post_id"]:
            continue
        entry["kind"] = "news"
        entries.setdefault(entry["post_id"], entry)
    return list(entries.values())


def _card_entries(feed: FeedSpec, parser: _FeedHTMLParser, observed_at: str) -> list[dict]:
    entries: dict[str, dict] = {}
    for title_link in parser.card_titles:
        title = title_link["text"]
        if not title or title.casefold() in _NAV_TITLES:
            continue
        url = _article_url(feed, title_link.get("href"))
        if not url or not _CARD_ARTICLE_PATH.search(urlsplit(url).path):
            continue
        entry = _base_entry(feed, observed_at, title, url, None)
        if not entry["post_id"]:
            continue
        entry["kind"] = "news"
        entries.setdefault(entry["post_id"], entry)
    return list(entries.values())


def _chart_entries(feed: FeedSpec, parser: _FeedHTMLParser, observed_at: str) -> list[dict]:
    entries = []
    seen = set()
    for row in parser.rows:
        cells = row["cells"]
        if not cells:
            continue
        if row.get("in_thead") or all(cell["tag"] == "th" for cell in cells):
            continue
        labeled = {key: value for cell in cells for key, value in cell["labels"].items() if value}
        title = labeled.get("title", "")
        artist = labeled.get("artist", "")
        if not title and len(cells) > 1:
            title = cells[1]["text"]
        if not title:
            continue
        item_key = f"{artist} - {title}" if artist else title
        url = _article_url(feed, row.get("href"))
        identity = post_id("news", url=url) if url else item_key.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        entry = _base_entry(feed, observed_at, title, url, None)
        entry.update({"kind": feed.kind, "item_key": item_key})
        if artist:
            entry["artist"] = artist
        if row.get("time_text"):
            entry["time_text"] = row["time_text"]
        rank_text = labeled.get("rank", "")
        rank_match = re.fullmatch(r"\s*(\d+)\s*", rank_text)
        if rank_match and int(rank_match.group(1)) > 0:
            entry["rank"] = int(rank_match.group(1))
        elif cells:
            rank_match = re.match(r"\s*(\d+)(?=\s|$)", cells[0]["text"])
            if rank_match and int(rank_match.group(1)) > 0:
                entry["rank"] = int(rank_match.group(1))
        entries.append(entry)
    return entries


def _playlist_entries(feed: FeedSpec, parser: _FeedHTMLParser, observed_at: str) -> list[dict]:
    entries = []
    seen = set()
    for card in parser.playlist_cards:
        title = card.get("title", "")
        artist = card.get("artist", "")
        if not title:
            continue
        item_key = f"{artist} - {title}" if artist else title
        url = _article_url(feed, card.get("href"))
        identity = post_id("news", url=url) if url else item_key.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        entry = _base_entry(feed, observed_at, title, url, None)
        entry.update({"kind": feed.kind, "item_key": item_key})
        if artist:
            entry["artist"] = artist
        if card.get("time_text"):
            entry["time_text"] = card["time_text"]
        entries.append(entry)
    return entries


class _SpladeAppParser(HTMLParser):
    """The data-html of div#app: a Splade page serves its markup as a JSON string there."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.markup: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs)
        if self.markup is None and tag == "div" and attrs.get("id") == "app" and attrs.get("data-html"):
            try:
                markup = json.loads(attrs["data-html"])
            except ValueError:
                return
            if isinstance(markup, str):
                self.markup = markup


def _splade_markup(html: str) -> str | None:
    parser = _SpladeAppParser()
    parser.feed(html)
    parser.close()
    return parser.markup


def _mdundo_entries(feed: FeedSpec, html: str, observed_at: str) -> list[dict]:
    parser = _MdundoHTMLParser()
    # The page as served (2 Oct 2026) holds its chart only inside div#app data-html; read that markup when the
    # page carries it, else the page itself.
    parser.feed(_splade_markup(html) or html)
    parser.close()
    parser._finish_card()
    entries = []
    for card in parser.cards:
        title = card.get("title", "")
        artist = card.get("artist", "")
        if not title:
            continue
        item_key = f"{artist} - {title}" if artist else title
        entry = _base_entry(feed, observed_at, title, None, None)
        entry.update({"kind": feed.kind, "item_key": item_key})
        if artist:
            entry["artist"] = artist
        rank_text = card.get("rank", "")
        if re.fullmatch(r"\d+", rank_text):
            entry["rank"] = int(rank_text)
        entries.append(entry)
    return entries


def parse_feed(feed: FeedSpec, html: str, fetched_at: datetime) -> list[dict]:
    _validate_feed(feed)
    observed_at = _observed_at(feed, fetched_at)
    if not isinstance(html, str) or not html.strip():
        raise _error(feed, "empty HTML body")
    parser = _FeedHTMLParser(feed)
    parser.feed(html)
    parser.close()
    parser._finish_heading()
    parser._finish_row()
    parser._finish_playlist_card()
    if feed.kind == "news":
        if feed.feed_id == "ng_pulse":
            return _pulse_entries(feed, parser, observed_at)
        if feed.feed_id in _CARD_HEADLINE_FEEDS:
            return _card_entries(feed, parser, observed_at)
        return _news_entries(feed, parser, observed_at)
    if feed.kind == "playlist":
        return _playlist_entries(feed, parser, observed_at)
    if feed.feed_id == "ke_mdundo_top_songs":
        return _mdundo_entries(feed, html, observed_at)
    return _chart_entries(feed, parser, observed_at)


def read_feed(
    feed: FeedSpec,
    *,
    fetched_at: datetime,
    transport: Callable[..., tuple[int, Mapping[str, object], bytes]] | None = None,
) -> list[dict]:
    _validate_feed(feed)
    observed_at = _observed_at(feed, fetched_at)
    try:
        if transport is None:
            status, headers, body = _default_transport(
                feed, timeout=TIMEOUT_SECONDS, max_bytes=MAX_RESPONSE_BYTES
            )
        else:
            status, headers, body = transport(
                feed.url, timeout=TIMEOUT_SECONDS, max_bytes=MAX_RESPONSE_BYTES
            )
    except FeedReadError:
        raise
    except Exception as exc:
        raise _error(feed, f"transport failed ({type(exc).__name__})") from exc
    if 300 <= status < 400:
        raise _error(feed, "redirect refused")
    if status != 200:
        raise _error(feed, f"HTTP status {status}")
    if not _is_html(headers):
        raise _error(feed, "response content type is not HTML")
    if not isinstance(body, bytes):
        raise _error(feed, "transport body must be bytes")
    if len(body) > MAX_RESPONSE_BYTES:
        raise _error(feed, "response exceeds the 2 MiB body limit")
    if not body.strip():
        raise _error(feed, "empty response body")
    html = _decode_html(feed, headers, body)
    entries = parse_feed(feed, html, fetched_at)
    if not entries:
        raise _error(feed, "HTML contains no supported feed entries")
    for entry in entries:
        entry["observed_at"] = observed_at
    return entries
