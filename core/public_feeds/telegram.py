"""Public Telegram channels as a public feed: the vetted channel list and the reader of a channel's web preview.

Albert's decision (4 Oct 2026, 11:02Z): posts from the researched local public Telegram channels count as local,
one step lower, like the gossip pages' own feeds. The list below is the vetted list of
42-inputs/data/telegram-channels-2026-10-04.md (20 NG, 19 KE, 6 ZA), observed on 4 Oct 2026. Each post is
attributed to its channel's market and written as a sighting in that market's own feeds (source_market), the
market-by-source rule of TRUST.md; no location is inferred from a channel.

The reader fetches https://t.me/s/<handle>, the public web preview: no login, no account, no API key and no
credits. t.me answered robots.txt with HTTP 404 on 4 Oct 2026, which allows every path; collect still reads it
first on every run (core/collect/telegram_collect.py).

TELEGRAM_READY is the switch. It is False: the collect job does not plan, fetch or write anything from Telegram,
and its plan printout, run counts and rows are what they were without this module. Flipping it to True is a
code change and a jobs image deploy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Literal

TELEGRAM_READY = False

Market = Literal["ZA", "NG", "KE"]
PREVIEW = "https://t.me/s/{handle}"
POST_URL = "https://t.me/{handle}/{message_id}"
# Per run: every vetted channel at most once, the newest posts of its preview page only (Telegram shows about
# 20), a pause between page reads, and a time budget for the whole phase.
MAX_CHANNELS_PER_RUN = 45
MAX_POSTS_PER_CHANNEL = 20
MAX_POSTS_PER_RUN = 600
POLITE_DELAY_SECONDS = 4.0
PHASE_BUDGET_SECONDS = 600
MAX_TEXT_CHARS = 4000

_HANDLE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_DATA_POST = re.compile(r"([A-Za-z][A-Za-z0-9_]{3,31})/(\d{1,12})")
_VIEWS = re.compile(r"(\d+(?:[.,]\d+)?)\s*([KkMm]?)")
_HASHTAG = re.compile(r"#(\w+)")
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
              "track", "wbr"}


@dataclass(frozen=True, slots=True)
class Channel:
    handle: str
    name: str
    market: Market
    category: str

    @property
    def url(self) -> str:
        return PREVIEW.format(handle=self.handle)


CHANNELS: tuple[Channel, ...] = (
    # Kenya (KE), 19 verified, sorted by activity on 4 Oct 2026.
    Channel("bnnkenya", "BNN BASIC", "KE", "celebrity / gossip"),
    Channel("nairobiRed", "Nairobi Red", "KE", "news / trends"),
    Channel("thestarkenya", "The Star Kenya", "KE", "news outlet"),
    Channel("Nairobby", "NAIROBI GOSSIP CLUB", "KE", "gossip / entertainment"),
    Channel("standardkenya", "The Standard", "KE", "news outlet"),
    Channel("KTNbreakingnews", "KTN News", "KE", "tv news"),
    Channel("tuko_news", "TUKO.co.ke News", "KE", "news / human interest"),
    Channel("Nyakundi", "Cyprian, Is Nyakundi", "KE", "news / exposes blog"),
    Channel("kenyainsight", "Kenya Insights", "KE", "news / investigations"),
    Channel("nairobigossiphub", "Nairobi Gossip Club", "KE", "gossip / entertainment"),
    Channel("kenyagossipclubpro", "KENYA GOSSIP CLUB", "KE", "gossip / entertainment"),
    Channel("kenyan_news_panel", "Kenyan News Panel", "KE", "news"),
    Channel("Nairobi_Gossip_Club_KE", "NAIROBI GOSSIP CLUB", "KE", "gossip / entertainment"),
    Channel("kenyateenslife", "NAIROBI TEEN LIFE", "KE", "entertainment"),
    Channel("TEACHERSUPDATES", "KENYA TEACHERS' UPDATES", "KE", "education / teachers"),
    Channel("jokersmokermemes001", "Kenyan memes Channel Official", "KE", "memes / humour"),
    Channel("OMGVoiceKE", "OMGVoice Kenya", "KE", "entertainment / viral"),
    Channel("nairobigossipupdate", "Nairobi gossip club", "KE", "gossip / entertainment"),
    Channel("kenyan_memess", "Kenyan Memes", "KE", "memes / humour"),
    # Nigeria (NG), 20 verified.
    Channel("PunchNewspaper", "Punch Newspaper", "NG", "news outlet"),
    Channel("Instablog9jaofficial", "Instablog9ja", "NG", "entertainment / viral news"),
    Channel("legitng", "Legit.ng News", "NG", "news outlet"),
    Channel("freshreporters", "FreshReporters News Channel", "NG", "news"),
    Channel("creebhills", "CreebHills", "NG", "celebrity news"),
    Channel("gist_ng", "Gist Ng - Nigeria News & Gossips", "NG", "news / gossip"),
    Channel("EKSUAMEBO", "EKSU AMEBO", "NG", "campus gist"),
    Channel("naijanews", "Naija News", "NG", "news outlet"),
    Channel("completesports", "Complete Sports", "NG", "sport newspaper"),
    Channel("Nigerii_News", "Nairaland Pulse / News", "NG", "news"),
    Channel("Football_Nigeria", "Nigeria Football Hub", "NG", "football"),
    Channel("Naija_Sports", "Naija Sports Hub", "NG", "sport"),
    Channel("SIRPHILIPFORUM", "SIR PHILIP FORUM", "NG", "education / exams"),
    Channel("yorubabroadcastingnetwork", "Yoruba Broadcasting Network", "NG", "culture / news"),
    Channel("Afirka1", "Hausa News", "NG", "news (Hausa)"),
    Channel("ncdcgov", "Nigeria Centre for Disease Control (NCDC)", "NG", "government / health"),
    Channel("bbc_noun", "BBCNOUN - National Open University of Nigeria", "NG", "education / university"),
    Channel("gistlover", "GISTSLOVERSBLOG", "NG", "gossip"),
    Channel("official_nysc", "NYSC Official Telegram", "NG", "youth service / government notices"),
    Channel("PeterObiSupportNetwork", "Peter Obi Support Network", "NG", "politics"),
    # South Africa (ZA), 6 verified.
    Channel("brieflycoza", "Briefly News", "ZA", "news outlet"),
    Channel("goodthingsguy", "GoodThingsGuy", "ZA", "good-news site feed"),
    Channel("dearsafrica", "Dear South Africa", "ZA", "news digest / civic"),
    Channel("ZA_future_of_the_West", "South Africa, future of the West", "ZA", "politics / crime"),
    Channel("WeAreFASA", "Freedom Alliance South Africa", "ZA", "civic / political movement"),
    Channel("SouthAfricaReports", "South Africa Reports", "ZA", "politics / crime"),
)


def channel_for(handle: str) -> Channel | None:
    """The vetted channel with this handle (Telegram handles ignore case), else None."""
    folded = str(handle or "").casefold()
    return next((c for c in CHANNELS if c.handle.casefold() == folded), None)


def views_count(text: str | None) -> int | None:
    """Telegram's rounded view label as a number: '12' 12, '1.2K' 1200, '3.4M' 3400000; None when unreadable."""
    match = _VIEWS.fullmatch(str(text or "").strip())
    if not match:
        return None
    number = float(match.group(1).replace(",", "." if match.group(2) else ""))
    scale = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2).lower()]
    return int(round(number * scale))


def hashtags(text: str) -> list[str]:
    out = []
    for name in _HASHTAG.findall(text or ""):
        if name not in out:
            out.append(name)
    return out


class _PreviewParser(HTMLParser):
    """The messages of a t.me/s page: one per div carrying data-post (class tgme_widget_message). Inside it the
    text is the div whose classes hold js-message_text (a quoted reply's text is js-message_reply_text and is
    skipped), the views span tgme_widget_message_views, and the time inside a.tgme_widget_message_date."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.messages: list[dict] = []
        self._depth = 0
        self._message: dict | None = None
        self._message_depth = 0
        self._capture: str | None = None
        self._capture_depth = 0
        self._in_date = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        void = tag in _VOID_TAGS
        if not void:
            self._depth += 1
        if self._message is None:
            if tag == "div" and "tgme_widget_message" in classes and attrs.get("data-post"):
                self._message = {"data_post": attrs["data-post"], "text": [], "views": [], "datetime": None,
                                 "service": "service_message" in classes, "forwarded": False}
                self._message_depth = self._depth
            return
        if tag == "br" and self._capture == "text":
            self._message["text"].append("\n")
            return
        if void:
            return
        if "tgme_widget_message_forwarded_from" in classes:
            self._message["forwarded"] = True
        if self._capture is None:
            if tag == "div" and "js-message_text" in classes and not self._message["text"]:
                self._capture, self._capture_depth = "text", self._depth
            elif tag == "span" and "tgme_widget_message_views" in classes and not self._message["views"]:
                self._capture, self._capture_depth = "views", self._depth
        if tag == "a" and "tgme_widget_message_date" in classes:
            self._in_date = True
        if tag == "time" and self._in_date and self._message["datetime"] is None:
            self._message["datetime"] = attrs.get("datetime")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return
        if self._message is not None:
            if self._capture is not None and self._depth == self._capture_depth:
                self._capture = None
            if tag == "a":
                self._in_date = False
            if self._depth == self._message_depth:
                self.messages.append(self._message)
                self._message = None
                self._capture = None
                self._in_date = False
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data):
        if self._message is not None and self._capture is not None:
            self._message[self._capture].append(data)


def _text(parts: list[str]) -> str:
    lines = [" ".join(line.split()) for line in "".join(parts).split("\n")]
    return "\n".join(line for line in lines if line).strip()[:MAX_TEXT_CHARS]


def _published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment.astimezone(timezone.utc)


def parse_preview(channel: Channel, html: str) -> tuple[list[dict], dict]:
    """(entries, dropped) from a channel's t.me/s page, newest first, at most MAX_POSTS_PER_CHANNEL.

    An entry: channel (the vetted handle), message_id, native_id (handle/id, lower-cased handle), url, published_at
    (an aware UTC datetime), text, views (int or None), hashtags and forwarded. A message is dropped (counted in
    dropped by reason) when it is a service message, belongs to another channel, has no readable date, or has
    no text (a photo or video without a caption gives the reader nothing to quote)."""
    parser = _PreviewParser()
    parser.feed(html or "")
    parser.close()
    entries, dropped, seen = [], {}, set()

    def drop(reason):
        dropped[reason] = dropped.get(reason, 0) + 1

    for message in parser.messages:
        match = _DATA_POST.fullmatch(str(message["data_post"]).strip())
        if message["service"]:
            drop("service_message")
            continue
        if not match or match.group(1).casefold() != channel.handle.casefold():
            drop("other_channel")
            continue
        message_id = int(match.group(2))
        if message_id in seen:
            drop("duplicate")
            continue
        seen.add(message_id)
        published = _published(message["datetime"])
        if published is None:
            drop("missing_date")
            continue
        text = _text(message["text"])
        if not text:
            drop("no_text")
            continue
        handle = channel.handle.casefold()
        entries.append({
            "channel": channel.handle,
            "message_id": message_id,
            "native_id": f"{handle}/{message_id}",
            "url": POST_URL.format(handle=handle, message_id=message_id),
            "published_at": published,
            "text": text,
            "views": views_count("".join(message["views"])),
            "hashtags": hashtags(text),
            "forwarded": message["forwarded"],
        })
    entries.sort(key=lambda e: e["message_id"], reverse=True)
    if len(entries) > MAX_POSTS_PER_CHANNEL:
        dropped["over_channel_cap"] = len(entries) - MAX_POSTS_PER_CHANNEL
        entries = entries[:MAX_POSTS_PER_CHANNEL]
    return entries, dropped


def valid_handle(handle: str) -> bool:
    return bool(_HANDLE.fullmatch(str(handle or "")))
