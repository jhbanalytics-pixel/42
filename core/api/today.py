"""Today and trends, built from one published brief per market (core/api/contract.md sections 4 and 5)."""
import copy
import datetime as dt
import hashlib
import json
import logging
import math
import re
import sys
from collections import Counter
from urllib.parse import parse_qs, unquote, urlsplit

from core.api.held_words import plain_reason as _plain_reason
from core.api.searching import searching_now
from core.api.store import canon_platform, creator_key
from core.brief.explain import _one_step_lower, _place_fault, _sentence_fault, _source_evidence
from core.brief.payload import NOT_ASSESSED_REASONS
from core.brief.specificity import MIN_EVIDENCE, showable_posts
from core.public_feeds.catalog import FEEDS
from core.trust.claims import LABELS as CLAIM_LABELS
from core.trust.claims import _cited, _k1, _norm, _quote_fault, _verified_quotes, allowed_label, place_fault

log = logging.getLogger(__name__)

MARKETS = ("ZA", "NG", "KE")
LABELS = {"ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya"}
SAST = dt.timezone(dt.timedelta(hours=2))
WARMUP_DAYS = 14
MOMENT_DAYS = 14
THIN_COVERAGE = 0.8
# Breaking on Today: rows of the hourly Breaking rule whose hour started in the last BREAKING_HOURS hours.
BREAKING_HOURS = 6
BREAKING_NEW = "New, no prior posts"
_HANDLE = re.compile(r"(?<![\w@])@[\w.]+")

STATE_WORDS = {"new_to_42": "First spotted", "spike": "Spike", "on_the_boards": "On the boards", "emerging": "Emerging",
               "rising": "Rising", "peaking": "Peaking", "mainstream": "Mainstream", "fading": "Fading",
               "recurring": "Recurring", "seasonal": "Seasonal"}
# detect's spike rule also fires on a chart alone (top 10 twice, state.sql), with no creator posting about it: that
# is a chart position, not a spike in posting, so the reader sees this word instead.
CHART_SPIKE_WORD = "High on the charts"
FLAG_WORDS = {"likely_coordinated": "Likely coordinated", "check_pattern": "Unusual posting pattern",
              "not_assessed": "Not assessed (thin sample)", "market_unconfirmed": "Market unconfirmed",
              "data_issue": "Data issue"}
DROPPED_WORDS = {"faded": "Faded", "held_back": "Held back", "reclassified_seasonal": "Reclassified as Seasonal",
                 "not_confirmed": "Not confirmed today", "not_assessed": "Not assessed this morning"}
HELD_SEE_BELOW = "Held back today; see why below"
FIRST_MORNING = "First morning: nothing to compare yet"
PLATFORM_WORDS = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube", "x": "X", "reddit": "Reddit",
                  "facebook": "Facebook", "telegram": "Telegram", "apple_music": "Apple Music", "twitter": "X"}
PLATFORM_IDS = {"twitter": "x"}
SERIES_WORDS = {"feed_tiktok": "TikTok", "board_tiktok_hashtag": "TikTok hashtag board",
                "curve_tiktok_hashtag": "TikTok hashtag popularity", "board_youtube": "YouTube trending board",
                "board_apple_music": "Apple Music chart", "list_reddit": "Reddit",
                "panel_fb_hub": "Facebook", "panel_culture_desk": "Culture accounts we follow",
                "panel_x_hub": "X", "panel_telegram": "Telegram",
                "ig_location": "Instagram location posts", "counter_tiktok_hashtag": "TikTok hashtag totals",
                "counter_tiktok_sound": "TikTok sound totals",
                "curve_tiktok_sound": "TikTok sound popularity (no longer collected)",
                "news_rss": "News feeds", "panel_ig_gossip": "Gossip and entertainment accounts on Instagram",
                "board_kworb_spotify": "Spotify daily chart", "board_boomplay": "Boomplay trending songs",
                "board_audiomack": "Audiomack trending", "board_shazam": "Shazam national chart",
                "board_shazam_city": "Shazam city chart", "board_turntable": "TurnTable Top 100",
                "board_nairaland": "Nairaland front page", "board_app_store_iphone": "App Store top free apps (iPhone)",
                "board_google_play": "Google Play top free apps", "board_music_country": "National music charts",
                "radio_playlist": "Radio playlists"}
# The brief job names a board it has no word for by its series with spaces ("board boomplay"); Today reads
# that stored name back as the series' own name.
BOARD_LIST_WORDS = {k.replace("_", " "): v for k, v in SERIES_WORDS.items()}
# board_music_country holds every public chart feed (core/collect/public_feed_rows.py), one platform each, so its
# board is named for its own chart; a chart platform not listed here reads as a national music chart.
CHART_FEED_WORDS = {"mdundo": "Mdundo top songs", "turntable": "TurnTable Top 100"}
CHART_FEED_LIST = "board music country"
CHART_FEED_FALLBACK = "National music chart"
# A board whose every entry was left out: said so, so an empty list does not read as one with a few names missing.
NO_NAMES_READ_AT = "None of this list's entries had a readable name {day}"
NO_NAMES_READ = NO_NAMES_READ_AT.format(day="today")
PLATFORM_SERIES_WORDS = {"board_global_music": "global music board", "search": "search"}
CROSS_SERIES_WORDS = {"counter_post_views": "Post view re-reads", "search": "Searches"}
INVALID_WORDS = {"calls": "could not be read", "items": "post count far from usual",
                 "effort": "too few accounts checked", "drift": "mix of posts far from usual",
                 "zero_yield": "answered but returned no posts or counts"}
# Each news feed is its own collection_health row (one protocol a feed), so Today names the feeds that failed
# and how many of the market's feeds were read, instead of one "News feeds" line per failed feed. The failure
# class is the one writers.call_failure writes after "calls: "; any other class is not worded.
NEWS_SERIES = "news_rss"
NEWS_FEED_FAILURES = {"robots": "robots.txt did not allow it", "http_4xx": "the site refused it",
                      "http_429": "the site asked 42 to slow down", "http_5xx": "the site answered with an error",
                      "timeout": "no answer in time", "connection": "no connection", "empty": "nothing to read",
                      "unparsable": "could not be parsed"}


def invalid_code(row):
    """The judge's word of a collection_health row's invalid_reason. A calls failure is written as
    "calls: <failure classes>" (core/collect/writers.py), so only the part before the colon is the word."""
    reason = row.get("invalid_reason")
    return None if reason is None else str(reason).split(":", 1)[0].strip()


# A board title that is an id, not a name: a YouTube channel id (uc plus exactly 22 id characters, any case), a
# Reddit account id (t2_ and its base-36 id) or a 64-hex item hash.
_ID_TITLE = re.compile(r"uc[a-z0-9_-]{22}|t2_[a-z0-9]+|[0-9a-f]{64}", re.IGNORECASE)
_YOUTUBE_CHANNEL_ID = re.compile(r"uc[a-z0-9_-]{22}\Z", re.IGNORECASE)
NO_NAME = "No readable name"
YOUTUBE_CHANNEL = "YouTube channel"
# core/brief/job.py CRITIC_MENU's wording, in its order. claim_checks.reason names at most one of these after
# "not ruled out: "; held_detail names an explanation only through this wording.
CRITIC_MENU_WORDS = ("a paid campaign", "a platform feature change", "a coordinated push", "a news or scheduled event",
                     "a scraping or collection artefact", "one viral post or creator")
CRITIC_HELD = "Critic: a simpler explanation was not ruled out"
# From the critic scope fix on: the local why-now part is named when it failed, alone or after the simpler one.
CRITIC_WHY_NOW = "Critic: local why-now not shown"
CRITIC_ALSO_WHY_NOW = "; local why-now not shown"
WHY_NOW_UNSHOWN = "The posts do not show why this is happening in this market now."
CHECK_REPAIR = "before repair: "
CRITIC_UNNAMED = "A simpler explanation could not be ruled out from these posts."
# Rules that only lower a label or status; their rows never hold an item (core/brief/job.py OTHER_WORDS).
CHECK_DOWNGRADES = {"K5", "K10"}
# core/brief/explain.py TITLE_RULE: a cut written title only leaves the card its label, so it never names a hold.
CHECK_TITLE = "title"
CHECK_RULES = ("K1", "K2", "K3", "K4", "K6", "K8")
CHECK_LIMIT = 5000


# Why the reader held a card back, in the order _today_card_fault checks them.
REJECT_EXPLAINED = "The explanation did not pass its checks"
REJECT_RECORD = "The trend record is incomplete"
REJECT_POSTS = "The posts behind it could not be read"
REJECT_CLAIMS = "The explanation is not tied to checked claims"
REJECT_WHY_NOW = "The local why-now was not checked"
REJECT_LOCAL = "Fewer than 2 local posts can be shown"
REJECT_QUOTE = "The quoted post wording could not be verified"
# A card's failed_reason when a hidden person's posts were left out of it and what stays no longer passes the
# brief's checks (without_hidden), held as a card whose explanation failed its checks.
HIDDEN_UNSUPPORTED = "A hidden person's post was left out, and the posts left do not support the explanation"
HIDDEN_TOO_FEW_POSTS = "A hidden person's posts were left out, leaving fewer than 3 posts 42 can show"


class NotReady(Exception):
    """No brief exists for the requested date."""


class NotFound(Exception):
    """The item or market is not in that brief."""


def hidden_people(store):
    """(creator keys, creator ids, names) of the suppressed creators, read as the people routes read them: the ids
    from v_suppressed_creators and the platform and handle of each one's creators rows, matched by creator_key, and
    the map labels of their YouTube channels' creator items (the channel's display name). None when the list
    cannot be read."""
    try:
        ids = store.suppressed_creators()
        if ids is None:
            log.warning("the suppression view does not exist; no evidence author is named")
            return None
        rows = (store.creators_by_id(sorted(ids)) or []) if ids else []
        keys = {creator_key(c.get("platform"), c.get("handle")) for c in rows} - {None}
        names = hidden_names(store, keys, ids)
    except Exception as exc:
        log.warning("the suppression list could not be read (%s); no evidence author is named", type(exc).__name__)
        return None
    return keys, set(ids), names


def hidden_names(store, keys, ids):
    """The display names of the hidden people's YouTube channels: the map labels of their own creator items. One
    read of the map, and only when one of them is a channel."""
    channels = _channel_ids(keys, ids)
    items = _creator_items((), channels)
    mapped = (store.map_items(sorted(items)) or []) if items else []
    return {r["label"].strip() for r in mapped if isinstance(r, dict) and r.get("item_id") in items
            and isinstance(r.get("label"), str) and _readable(r["label"])}


def _channel_ids(keys, ids):
    """The YouTube channel ids among the suppressed creators' ids and handles."""
    handles = {k.split(":", 1)[1] for k in keys if k.startswith("youtube:")}
    return {i for i in set(ids) | handles if _is_youtube_channel_id(i)}


def _creator_items(keys, channels):
    """{item id: platform} of the suppressed creators' own creator items, keyed as core.detect.items keys them
    (kind creator, "platform:handle" folded; X as twitter too, and a YouTube channel by its id)."""
    found = {}
    for key in set(keys) | {"youtube:" + c.casefold() for c in channels}:
        platform, handle = key.split(":", 1)
        for p in ("x", "twitter") if platform == "x" else (platform,):
            found[hashlib.sha256(f"creator|{p}:{handle.casefold()}".encode("utf-8")).hexdigest()] = platform
    return found


def _dicts(value):
    """Every dict inside value."""
    if isinstance(value, list):
        for v in value:
            yield from _dicts(v)
    elif isinstance(value, dict):
        yield value
        for v in value.values():
            yield from _dicts(v)


def _evidence_holders(value):
    """Every dict inside value that holds an evidence list."""
    if isinstance(value, list):
        for v in value:
            yield from _evidence_holders(v)
    elif isinstance(value, dict):
        if isinstance(value.get("evidence"), list):
            yield value
        for v in value.values():
            yield from _evidence_holders(v)


def without_hidden(value, hidden):
    """A copy of value (a brief payload, a card or a topic page) with no suppressed person in it (SETUP.md data
    protection): their posts leave every evidence list, with the thumbnails and evidence_ids that point at them,
    and their handle and YouTube channel id are masked in every other string. Their display name (the map label
    of their own creator item, or an author name on a left-out post) is masked only where it names them: their
    own creator item (title, label, ask and aliases), titled as a YouTube channel when it is one, and the author
    fields of evidence. Claims lose the quotes of a left-out post. A claim that rested only on left-out posts keeps
    its evidence ids, so Today's checks hold its card back with their reason. A claim that also cites posts that stay
    is checked again on those by the brief's rules, and an explained card that lost posts meets the brief's card
    floors again (_recheck_explanation). hidden None (the list cannot be read) names no evidence author at all, as a
    skin report masks an author it may not name."""
    from core.api import skins

    if hidden is None:
        records = [e for h in _evidence_holders(value) for e in h["evidence"] if isinstance(e, dict)]
        # One deepcopy keeps the records shared with the body, so stripping them strips the body's.
        return skins.mask_people({"evidence": records, "body": value}, (), ())["body"]
    keys, ids, names = hidden
    if not keys and not ids:
        return value
    channels = _channel_ids(keys, ids)
    keys = keys | {"youtube:" + c.lower() for c in channels}
    own, names = _creator_items(keys, channels), set(names)
    out = copy.deepcopy(value)
    for holder in _evidence_holders(out):
        theirs = [isinstance(e, dict) and (creator_key(e.get("platform"), e.get("handle")) in keys
                                           or e.get("creator_id") in ids) for e in holder["evidence"]]
        if not any(theirs):
            continue
        names.update(" ".join(e[k].split()) for e, t in zip(holder["evidence"], theirs) if t
                     for k in skins.NAME_KEYS if isinstance(e.get(k), str) and e[k].strip())
        gone = {e.get("id") for e, t in zip(holder["evidence"], theirs) if t} - {None}
        holder["evidence"] = [e for e, t in zip(holder["evidence"], theirs) if not t]
        # The written title was checked against posts that have now left the card, so it keeps its label.
        if holder.get("title_written") is not None:
            holder["title_written"] = None
        for key in ("thumbnails", "evidence_ids"):
            if isinstance(holder.get(key), list):
                holder[key] = [i for i in holder[key] if i not in gone]
        # A quote is their words: it leaves with their post, while the claim keeps its evidence ids.
        for claim in holder.get("claims") if isinstance(holder.get("claims"), list) else []:
            if isinstance(claim, dict) and isinstance(claim.get("quotes"), list):
                claim["quotes"] = [q for q in claim["quotes"]
                                   if not (isinstance(q, dict) and q.get("evidence_id") in gone)]
        specificity = holder.get("specificity")
        if isinstance(specificity, dict) and isinstance(specificity.get("quote"), dict) \
                and specificity["quote"].get("evidence_id") in gone:
            specificity["quote"] = None
        if isinstance(specificity, dict) and isinstance(specificity.get("local_evidence_ids"), list):
            specificity["local_evidence_ids"] = [i for i in specificity["local_evidence_ids"] if i not in gone]
        _recheck_explanation(holder, gone)
    # Their own creator items: the title and label name them; a YouTube channel's item shows as the channel. A
    # display name is masked only where it names them (their own item, an author field), never in other words.
    # An own item id is a creator item's, so a row naming it without a kind (a Compare subject) is theirs too.
    owned = [d for d in _dicts(out) if d.get("kind") in ("creator", None) and d.get("item_id") in own]
    for item in owned:
        names.update(str(item[k]).strip() for k in ("title", "label") if _readable(item.get(k)))
    names = {" ".join(n.split()).lower().lstrip("@") for n in names} - {"", YOUTUBE_CHANNEL.lower()}
    handles = {k.split(":", 1)[1] for k in keys}
    known, tagged, called = skins._known(handles), skins._tagged(handles), skins._known(names)

    def masked(text):
        if tagged is not None:
            text = tagged.sub(lambda m: m.group(1) + skins.URL_MASK, text)
        if known is not None:
            text = known.sub(lambda m: skins.MASK if m.group(0).startswith("@") else skins.URL_MASK, text)
        return text

    for item in owned:
        word = YOUTUBE_CHANNEL if own[item["item_id"]] == "youtube" else skins.URL_MASK
        for k in ("title", "label", "ask"):
            if isinstance(item.get(k), str):
                text = word if k != "ask" and word == YOUTUBE_CHANNEL else item[k]
                item[k] = masked(called.sub(word, text) if called is not None else text)
        if isinstance(item.get("aliases"), list):
            item["aliases"] = [masked(called.sub(word, a) if called is not None else a) if isinstance(a, str) else a
                               for a in item["aliases"]]
    for holder in _evidence_holders(out):
        for e in holder["evidence"]:
            for k in skins.NAME_KEYS if isinstance(e, dict) else ():
                if isinstance(e.get(k), str) and " ".join(e[k].split()).lower().lstrip("@") in names:
                    e[k] = skins.URL_MASK
    return skins._mask_strings(out, masked, lambda text: text)


def _recheck_explanation(card, gone):
    """Check an explained card again once the posts in gone left it (without_hidden), by the rules the brief applied.

    A claim that cites a left-out post beside posts that stay is judged on the posts that stay: K1 (its ids resolve,
    its quotes are verbatim in them and every quoted span in its text is one of them) and K3's place rule, or it is
    dropped; its label is lowered to what K5 allows on them, one step lower on a news-driven card, and never raised.
    A claim with no post left is not touched here: Today's own check holds its card for posts that could not be read.
    Then the card floors: the explanation must still rest on claims that stand (at least 2, every one it names, its
    named places supported) and, for a card in a market, at least MIN_EVIDENCE posts must be showable. A card that
    fails is held as the brief holds one whose explanation failed its checks (G10): held, reason shown, with its
    numbers and posts and a failed_reason."""
    claims, rests_on = card.get("claims"), card.get("explanation_claim_ids")
    if (card.get("explained") is not True or not isinstance(claims, list) or not isinstance(rests_on, list)
            or not all(isinstance(c, dict) and isinstance(c.get("evidence_ids"), list)
                       and all(isinstance(i, str) for i in c["evidence_ids"]) for c in claims)):
        return
    records = {r.get("id"): r for r in _source_evidence(card) if isinstance(r, dict)}
    touched = [c for c in claims if gone & set(c["evidence_ids"])]
    if any(not any(i in records for i in c["evidence_ids"] if i not in gone) for c in touched):
        return
    ids, rechecked = Counter(c.get("id") for c in claims), {id(c) for c in touched}
    kept = []
    for claim in claims:
        if id(claim) not in rechecked:
            kept.append(claim)
            continue
        claim = dict(claim, evidence_ids=[i for i in claim["evidence_ids"] if i not in gone])
        if _k1(claim, records, ids)[0] != "pass" or place_fault(claim.get("text") or "", _cited(claim, records),
                                                                 _verified_quotes(claim, records)):
            continue
        ceiling = {"label": allowed_label(claim, records)}
        if card.get("news_driven") is True:
            ceiling = _one_step_lower([ceiling])[0]
        if claim.get("label") in CLAIM_LABELS \
                and CLAIM_LABELS.index(claim["label"]) > CLAIM_LABELS.index(ceiling["label"]):
            claim["label"] = ceiling["label"]
        kept.append(claim)
    card["claims"] = kept
    checked = {"short_answer": card.get("explanation") if isinstance(card.get("explanation"), str) else "",
               "claims": kept}
    failed = None
    # explain_trend's floors: the sentence rests on claims that stand, 2 or more of them stand, and the places it
    # names are supported by the posts those claims cite.
    if touched and (_sentence_fault(checked, rests_on) or len(kept) < 2
                    or _place_fault(checked["short_answer"], checked, rests_on, records)):
        failed = HIDDEN_UNSUPPORTED
    elif card.get("market") in MARKETS and len(showable_posts(card.get("evidence"), card["market"])) < MIN_EVIDENCE:
        failed = HIDDEN_TOO_FEW_POSTS
    if failed:
        card.update(explained=False, explanation=None, explanation_claim_ids=[], claims=[], news_driven=False,
                    explanation_status="failed_checks", failed_reason=failed)
        if "title_written" in card:
            card["title_written"] = None


class _WithoutHidden:
    """A store whose brief payloads are read through without_hidden, so Today's checks see what may be shown."""

    def __init__(self, store):
        self._store, self._hidden = store, hidden_people(store)

    def __getattr__(self, name):
        return getattr(self._store, name)

    def _row(self, row):
        return dict(row, payload=without_hidden(row.get("payload"), self._hidden)) if row else row

    def briefs(self, date):
        return [self._row(r) for r in self._store.briefs(date) or []]

    def previous_brief(self, market, before):
        return self._row(self._store.previous_brief(market, before))


def _long_date(date):
    d = dt.date.fromisoformat(date)
    return f"{d.day} {d:%B} {d.year}"


def _rows_by_market(store, date):
    rows = store.briefs(date)
    if not rows:
        raise NotReady(f"No brief for {date}")
    return {r["market"]: r for r in rows}


def _entries(payload):
    return (payload.get("cards") or []) + (payload.get("more") or [])


def _prev_ranks(store, market, date):
    prev = store.previous_brief(market, date)
    if prev is None:
        return None
    return {c["item_id"]: c["rank"] for c in _entries(prev["payload"])}, prev


def _platform_x(rows):
    """Collection writes X as "twitter"; every record 42 returns names it "x" (contract section 4)."""
    return [dict(r, platform=canon_platform(r.get("platform"))) for r in rows or []]


def _readable(title):
    if title is None:
        return False
    title = str(title).strip()
    if not title or _ID_TITLE.fullmatch(title):
        return False
    prefix, separator, suffix = title.rpartition(":")
    if separator and canon_platform(prefix) in PLATFORM_WORDS and (suffix.isdecimal() or _ID_TITLE.fullmatch(suffix)):
        return False
    return True


# A chart row the collector stored with Markdown still in it: a [label](url) link, and backslash escapes such as \_
# and \[. Shazam's broken rows are "artist by [song](url)", the song and artist swapped. Seen on staging, 4 October 2026.
_MD_LINK = re.compile(r"\[((?:\\.|[^\]\\])*)\]\((https?://[^)\s]*)\)")
_SWAPPED = re.compile(r"(?P<artist>.+?) by \[(?P<song>(?:\\.|[^\]\\])*)\]\(https?://[^)\s]*\)\Z")
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!])")


def _board_title(entry, platform=None):
    """The entry's title as a reader should see it, or None when it carries no name: Markdown links become their
    label, escapes are dropped, a Shazam row swapped as "artist by [song](url)" reads "song by artist", and a title that is only
    the entry's own rank (a parse that took the rank for the name) is no name."""
    title = entry.get("title")
    if title is None:
        return None
    text = str(title).strip()
    swapped = _SWAPPED.fullmatch(text) if platform == "shazam" else None
    if swapped:
        text = f"{swapped['song']} by {swapped['artist']}"
    text = _MD_ESCAPE.sub(r"\1", _MD_LINK.sub(lambda m: m.group(1), text)).strip()
    rank = entry.get("rank")
    if rank is not None and text == str(rank).strip():
        return None
    return text if _readable(text) else None


def _post_id(title):
    if title is None:
        return None
    value = str(title).strip()
    if value.isdecimal():
        return value
    prefix, separator, suffix = value.rpartition(":")
    return suffix if separator and suffix.isdecimal() and canon_platform(prefix) in PLATFORM_WORDS else None


def _platform_word(platform):
    return PLATFORM_WORDS.get(platform) or _sentence(platform)


def _display_title(candidates, evidence=None, platforms=None, kind=None):
    candidates = list(candidates or [])
    evidence = list(evidence or [])
    for candidate in candidates:
        if _post_id(candidate) is None and _readable(candidate):
            return str(candidate).strip()

    for candidate in candidates:
        post_id = _post_id(candidate)
        if post_id is None:
            continue
        matched = next((e for e in evidence if str(e.get("id") or "").strip() == post_id), None)
        if matched is None:
            continue
        platform = canon_platform(matched.get("platform"))
        handle = str(matched.get("handle") or "").strip().lstrip("@")
        if platform and handle:
            if platform == "youtube" and _is_youtube_channel_id(handle):
                return f"{_platform_word(platform)} post"
            return f"{_platform_word(platform)} post by @{handle}"
        if platform:
            return f"{_platform_word(platform)} post"

    found = []
    for platform in list(platforms or []) + [e.get("platform") for e in evidence]:
        platform = canon_platform(platform)
        if platform and platform not in found:
            found.append(platform)
    if len(found) == 1:
        return f"{_platform_word(found[0])} {'account' if kind == 'creator' else 'post'}"
    if found:
        return "Social media post"
    kind_words = {"hashtag": "Hashtag", "sound": "Sound", "creator": "Creator", "topic": "Trend",
                  "format": "Format", "meme": "Meme", "brand": "Brand", "event": "Event"}
    return f"{kind_words.get(kind, 'Trend')} item"


def _is_youtube_channel_id(title):
    return isinstance(title, str) and _YOUTUBE_CHANNEL_ID.fullmatch(title.strip()) is not None


def _today_creator_map_ids(rows):
    item_ids = []
    for row in rows.values():
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        entries = []
        for key in ("cards", "more"):
            values = payload.get(key)
            if isinstance(values, list):
                entries.extend(values)
        held = payload.get("held_back")
        if isinstance(held, dict) and isinstance(held.get("items"), list):
            entries.extend(held["items"])
        for item in entries:
            if (isinstance(item, dict) and item.get("kind") == "creator"
                    and _is_youtube_channel_id(item.get("title"))
                    and isinstance(item.get("item_id"), str) and item["item_id"].strip()):
                item_ids.append(item["item_id"])
    return list(dict.fromkeys(item_ids))


def _today_creator_labels(store, rows):
    item_ids = _today_creator_map_ids(rows)
    if not item_ids:
        return {}
    map_items = getattr(store, "map_items", None)
    if not callable(map_items):
        return {}
    mapped = map_items(item_ids)
    if not isinstance(mapped, list):
        return {}
    requested = set(item_ids)
    labels = {}
    ambiguous = set()
    for row in mapped:
        if not isinstance(row, dict):
            continue
        item_id, label = row.get("item_id"), row.get("label")
        if (not isinstance(item_id, str) or item_id not in requested or row.get("kind") != "creator"
                or not isinstance(label, str) or not label.strip() or label.strip().casefold() == NO_NAME.casefold()
                or _post_id(label) is not None or not _readable(label)):
            continue
        label = label.strip()
        if item_id in labels and labels[item_id] != label:
            ambiguous.add(item_id)
        else:
            labels[item_id] = label
    return {item_id: label for item_id, label in labels.items() if item_id not in ambiguous}


def _item_title(item, evidence, creator_labels):
    title = item.get("title")
    if item.get("kind") == "creator" and _is_youtube_channel_id(title):
        item_id = item.get("item_id")
        return (creator_labels.get(item_id) if isinstance(item_id, str) else None) or YOUTUBE_CHANNEL
    raw_platforms = item.get("platforms")
    if isinstance(raw_platforms, list):
        platforms = [canon_platform(platform) for platform in raw_platforms if isinstance(platform, str)]
    else:
        platforms = None
    kind = item.get("kind")
    kind = kind if isinstance(kind, str) else None
    return _display_title([title], evidence=evidence, platforms=platforms, kind=kind)


def _board_list(board):
    if board.get("list") == CHART_FEED_LIST:
        return CHART_FEED_WORDS.get(board.get("platform"), CHART_FEED_FALLBACK)
    return BOARD_LIST_WORDS.get(board.get("list"), board.get("list"))


# Music charts, by the list name the brief job stores (its BOARD_WORDS word or the series with spaces) or the
# board's platform. Only there does one title naming its artist mean one entry: a song under two item ids.
MUSIC_SERIES = ("board_apple_music", "board_kworb_spotify", "board_boomplay", "board_audiomack", "board_shazam",
                "board_shazam_city", "board_turntable", "board_music_country", "board_global_music", "radio_playlist")
MUSIC_LISTS = frozenset({s.replace("_", " ") for s in MUSIC_SERIES} | {"Apple Music chart"})
MUSIC_PLATFORMS = frozenset({"apple_music", "spotify", "boomplay", "audiomack", "shazam", "turntable", "mdundo"})
# "Song by Artist" (Shazam is unswapped to it) or "Artist - Song" (Mdundo): a title that names its artist.
_NAMES_ARTIST = re.compile(r"\S\s+(?:by|-|\u2013)\s+\S", re.IGNORECASE)


def _entry_keys(entry, music):
    """What makes two entries of one board the same entry: the same item id, or on a music chart the same title
    naming the same artist. Two items that only share a label, such as two YouTube videos both called "Highlights",
    stay two entries."""
    keys = set()
    item_id = entry.get("item_id")
    if isinstance(item_id, str) and item_id:
        keys.add(("item", item_id))
    if music and _NAMES_ARTIST.search(entry["title"]):
        keys.add(("song", " ".join(entry["title"].casefold().split())))
    return keys


def _chart_key(board):
    """A chart is one platform and list of a market's boards for the brief date; a city list is one chart per city."""
    list_ = board.get("list")
    return (board.get("platform"), list_.strip() if isinstance(list_, str) else "", board.get("city"))


def _chart_sets(boards):
    """item_id to the charts that hold it, read from every stored entry before any is left out or merged: an entry
    titled with an id, one with no rank and a repeat within its chart all still count, each chart once."""
    found = {}
    for b in boards:
        if not isinstance(b, dict):
            continue
        for e in b.get("entries") or []:
            item_id = e.get("item_id") if isinstance(e, dict) else None
            if isinstance(item_id, str) and item_id.strip():
                found.setdefault(item_id, set()).add(_chart_key(b))
    return found


def _boards(boards, day="today"):
    """Each board keeps its own list and each entry its own rank, L2's best rank today, so ties and gaps stay.
    Entries titled with an id are left out, never named, and added to any count the board already carries. Two
    entries of one board that are the same entry (_entry_keys: one song under two item ids) show once, at the better
    positive integer rank. Equal ranks keep the first input row; unrelated entries keep their source order.
    chart_counts, on a board with shown entries that have an item id, gives the number of charts holding each item
    in this market's boards, counted over the stored entries (_chart_sets). day words the all-ids reason: "today",
    or "on 30 September 2026" on a past brief."""
    out = []
    boards = _platform_x(boards)
    charts = _chart_sets(boards)
    for b in boards:
        named = [dict(e, title=t) for e in b.get("entries") or [] for t in [_board_title(e, b.get("platform"))] if t]
        music = b.get("list") in MUSIC_LISTS or b.get("platform") in MUSIC_PLATFORMS
        ranked = []
        for index, e in enumerate(named):
            rank = e.get("rank")
            priority = rank if isinstance(rank, int) and not isinstance(rank, bool) and rank > 0 else math.inf
            ranked.append((priority, index, e))
        shown, seen = [], set()
        for _, index, e in sorted(ranked, key=lambda row: row[:2]):
            keys = _entry_keys(e, music)
            if not keys & seen:
                shown.append((index, e))
            seen |= keys
        shown = [e for _, e in sorted(shown, key=lambda row: row[0])]
        left_out = (b.get("left_out") or 0) + len(b.get("entries") or []) - len(named)
        reason = b.get("left_out_reason") or (NO_NAMES_READ_AT.format(day=day) if not shown else NO_NAME)
        counts = {e["item_id"]: len(charts[e["item_id"]]) for e in shown
                  if isinstance(e.get("item_id"), str) and e["item_id"].strip()}
        out.append(dict(b, list=_board_list(b), entries=shown, left_out=left_out,
                        left_out_reason=reason if left_out else None, **({"chart_counts": counts} if counts else {})))
    return out


# Briefs written before 6 October 2026 say "1 creators and 1 posts in 3 days" (core/brief/payload.py _count_line now
# says "1 creator"); a stored line reads the same way. "21 posts" keeps its plural.
_ONE_COUNT = re.compile(r"(?<![\d.,\u00a0\u202f])1 (creator|post)s\b")


def _one_count(line):
    return _ONE_COUNT.sub(r"1 \1", line) if isinstance(line, str) else line


def state_word(state, creators3=None):
    """The reader's word for an item state. A spike no creator posted about in 3 days (creators3 0, as item_state's
    NULL reads on a card) is worded as a chart position; with no count given the state keeps its own word."""
    if state == "spike" and creators3 == 0:
        return CHART_SPIKE_WORD
    return STATE_WORDS.get(state)


def _card(card, market, date, prev_ranks):
    out = dict(card, market=market, date=date)
    if "count_line" in card:
        out["count_line"] = _one_count(card["count_line"])
    if card.get("evidence"):
        out["evidence"] = _platform_x(card["evidence"])
    if prev_ranks is None:
        out["tag"] = None
    elif card["item_id"] not in prev_ranks:
        out["tag"] = "first_time"
    elif card["rank"] < prev_ranks[card["item_id"]]:
        out["tag"] = "moved_up"
    else:
        out["tag"] = "held_place"
    out["state_word"] = STATE_WORDS.get(card.get("state"))
    out["flag_word"] = FLAG_WORDS.get(card.get("flag"))
    if not card.get("explained"):
        out.update(explained=False, explanation=None, explanation_claim_ids=[], claims=[])
    if "title_written" in card:
        # The writer's checked title (core/brief/payload.py) shows only on an explained card; title stays the
        # cluster label. A brief written before 6 October 2026 has no such field and none is added.
        written = card.get("title_written")
        out["title_written"] = (" ".join(written.split()) if card.get("explained") and isinstance(written, str)
                                and written.strip() else None)
    out["thumbnails"] = (card.get("thumbnails") or [])[:2]
    return out


def _claim_lost_every_post(card):
    """True when an explained card has a claim whose cited posts are none of the posts the card returns, as when a
    suppressed creator's post was its only support. Today holds such a card; the trend readers show it unexplained."""
    evidence = card.get("evidence")
    if card.get("explained") is not True or not isinstance(evidence, list) or not isinstance(card.get("claims"), list):
        return False
    returned = {e.get("id") for e in evidence if isinstance(e, dict)}
    return any(isinstance(c, dict) and isinstance(c.get("evidence_ids"), list) and c["evidence_ids"]
               and not returned & set(c["evidence_ids"]) for c in card["claims"])


def _trend_card(card, market, date, ranks):
    out = _card(card, market, date, ranks)
    if _claim_lost_every_post(out):
        out.update(explained=False, explanation=None, explanation_claim_ids=[], claims=[], news_driven=False,
                   explanation_status="failed_checks", failed_reason=HIDDEN_UNSUPPORTED)
        if "title_written" in out:
            out["title_written"] = None
    return out


def _cards(payload, market, date, prev):
    ranks = prev[0] if prev else None
    cards = [_trend_card(c, market, date, ranks) for c in payload.get("cards") or []]
    more = [_trend_card(c, market, date, ranks) for c in payload.get("more") or []]
    return cards, more


def _today_card_admissible(card):
    return _today_card_fault(card) is None


def _today_card_fault(card):
    """None when Today may show the card, else the plain words for the first check it failed."""
    if not isinstance(card, dict) or card.get("explained") is not True:
        return REJECT_EXPLAINED
    if not isinstance(card.get("item_id"), str) or not card["item_id"].strip():
        return REJECT_RECORD
    rank = card.get("rank")
    if not isinstance(rank, int) or isinstance(rank, bool):
        return REJECT_RECORD
    if any(card.get(field) is not None and not isinstance(card.get(field), str) for field in ("state", "flag")):
        return REJECT_RECORD
    thumbnails = card.get("thumbnails")
    if thumbnails is not None and not isinstance(thumbnails, list):
        return REJECT_RECORD
    if "explanation_status" in card and card["explanation_status"] is not None:
        if card["explanation_status"] != "explained":
            return REJECT_EXPLAINED

    explanation = card.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        return REJECT_EXPLAINED

    evidence = card.get("evidence")
    if not isinstance(evidence, list) or any(not isinstance(record, dict) for record in evidence):
        return REJECT_POSTS
    evidence_by_id = {}
    for record in evidence:
        platform = record.get("platform")
        if platform is not None and not isinstance(platform, str):
            return REJECT_POSTS
        evidence_id = record.get("id")
        if isinstance(evidence_id, str) and evidence_id.strip():
            evidence_by_id.setdefault(evidence_id, []).append(record)

    claims = card.get("claims")
    if not isinstance(claims, list) or not claims:
        return REJECT_CLAIMS
    claims_by_id = {}
    for claim in claims:
        if not isinstance(claim, dict):
            return REJECT_CLAIMS
        claim_id = claim.get("id")
        evidence_ids = claim.get("evidence_ids")
        if not isinstance(claim_id, str) or not claim_id.strip() or claim_id in claims_by_id:
            return REJECT_CLAIMS
        if not isinstance(evidence_ids, list) or any(not isinstance(i, str) or not i.strip() for i in evidence_ids):
            return REJECT_CLAIMS
        claims_by_id[claim_id] = claim

    explanation_claim_ids = card.get("explanation_claim_ids")
    if (not isinstance(explanation_claim_ids, list) or not explanation_claim_ids
            or any(not isinstance(i, str) or not i.strip() for i in explanation_claim_ids)):
        return REJECT_CLAIMS
    if any(claim_id not in claims_by_id for claim_id in explanation_claim_ids):
        return REJECT_CLAIMS
    referenced_evidence_ids = {
        evidence_id
        for claim_id in explanation_claim_ids
        for evidence_id in claims_by_id[claim_id]["evidence_ids"]
    }

    specificity = card.get("specificity")
    if not isinstance(specificity, dict) or specificity.get("status") != "pass":
        return REJECT_WHY_NOW
    if "reason" not in specificity or specificity["reason"] is not None:
        return REJECT_WHY_NOW
    why_now = specificity.get("why_now")
    if not isinstance(why_now, str) or why_now != explanation.strip():
        return REJECT_WHY_NOW
    local_ids = specificity.get("local_evidence_ids")
    if (not isinstance(local_ids, list) or any(not isinstance(i, str) or not i.strip() for i in local_ids)
            or len(set(local_ids)) < 2 or not set(local_ids) <= referenced_evidence_ids):
        return REJECT_LOCAL
    available_examples = 0
    for evidence_id in set(local_ids):
        records = evidence_by_id.get(evidence_id, [])
        if len(records) != 1:
            continue
        record = records[0]
        if any(isinstance(record.get(field), str) and record[field].strip() for field in ("text", "quote_text")):
            available_examples += 1
            continue
        url = record.get("url")
        if isinstance(url, str) and url.strip():
            try:
                parsed_url = urlsplit(url.strip())
                port = parsed_url.port
                safe_url = (parsed_url.scheme.lower() in {"http", "https"} and bool(parsed_url.hostname)
                            and not parsed_url.username and not parsed_url.password
                            and (port is None or 0 <= port <= 65535))
            except ValueError:
                safe_url = False
            if safe_url:
                available_examples += 1
    if available_examples < 2:
        return REJECT_LOCAL

    quote = specificity.get("quote")
    if not isinstance(quote, dict):
        return REJECT_QUOTE
    quote_id, quote_text = quote.get("evidence_id"), quote.get("text")
    if (not isinstance(quote_id, str) or not quote_id.strip() or not isinstance(quote_text, str)
            or not quote_text.strip() or quote_id not in local_ids or quote_id not in referenced_evidence_ids):
        return REJECT_QUOTE
    cited_quotes = set()
    for claim_id in explanation_claim_ids:
        claim = claims_by_id[claim_id]
        quotes = claim.get("quotes")
        if isinstance(quotes, list):
            for claim_quote in quotes:
                if isinstance(claim_quote, dict):
                    evidence_id, text = claim_quote.get("evidence_id"), claim_quote.get("text")
                    if (isinstance(evidence_id, str) and evidence_id in claim["evidence_ids"]
                            and isinstance(text, str)):
                        cited_quotes.add((evidence_id, text))
    if (quote_id, quote_text) not in cited_quotes:
        return REJECT_QUOTE
    quote_records = evidence_by_id.get(quote_id, [])
    if len(quote_records) != 1:
        return REJECT_QUOTE
    record = quote_records[0]
    full_source_text = record.get("quote_text")
    source_text = (full_source_text if isinstance(full_source_text, str) and full_source_text.strip()
                   else record.get("text"))
    words = quote_text.split()
    if (not isinstance(source_text, str) or quote_text not in source_text or len(words) < 2
            or len(words) > 25 or len(quote_text) > 160 or _quote_fault(_norm(quote_text), source_text)):
        return REJECT_QUOTE
    # A claim may cite a post the card does not return, but it must keep one it does, and every quote it still
    # carries must be from a returned post: a claim that rested only on a left-out post (a suppressed creator's,
    # whose quotes without_hidden removes) holds the card rather than show a citation that does not resolve.
    for claim in claims:
        if not any(len(evidence_by_id.get(i, [])) == 1 for i in claim["evidence_ids"]):
            return REJECT_POSTS
        quotes = claim.get("quotes") if isinstance(claim.get("quotes"), list) else []
        if any(isinstance(q, dict) and not (isinstance(q.get("evidence_id"), str)
                                            and len(evidence_by_id.get(q["evidence_id"], [])) == 1)
               for q in quotes):
            return REJECT_QUOTE
    return None


def _today_cards(payload, market, date, prev):
    raw_cards = payload.get("cards")
    raw_more = payload.get("more")
    raw_cards = raw_cards if isinstance(raw_cards, list) else []
    raw_more = raw_more if isinstance(raw_more, list) else []
    raw_entries = raw_cards + raw_more
    current_ids = {card["item_id"] for card in raw_entries
                   if isinstance(card, dict) and isinstance(card.get("item_id"), str) and card["item_id"].strip()}
    ranks = prev[0] if prev else None
    admitted, rejected = [], []
    for card in raw_entries:
        if _today_card_admissible(card):
            admitted.append(_card(card, market, date, ranks))
        elif (isinstance(card, dict) and isinstance(card.get("item_id"), str)
              and card["item_id"].strip()):
            rejected.append(card)
    return admitted[:5], admitted[5:], current_ids, rejected


def brief_counts(payload):
    """What Today shows of one stored brief row, as item ids: {"shown": the cards and more Today admits, "held":
    held_back.items as Today counts them (the producer's held entries, then the cards Today does not admit, each
    item once; an entry with no item id stays, as None)}. History counts zero-card briefs from this."""
    payload = payload if isinstance(payload, dict) else {}
    entries = [c for key in ("cards", "more") if isinstance(payload.get(key), list) for c in payload[key]]
    shown = [c["item_id"] for c in entries if _today_card_admissible(c)]
    rejected = [c["item_id"] for c in entries if not _today_card_admissible(c) and isinstance(c, dict)
                and isinstance(c.get("item_id"), str) and c["item_id"].strip()]
    block = payload.get("held_back") if isinstance(payload.get("held_back"), dict) else {}
    items = block.get("items") if isinstance(block.get("items"), list) else []
    held, seen = [], set()
    for item_id in [h.get("item_id") for h in items if isinstance(h, dict)] + rejected:
        if isinstance(item_id, str) and item_id.strip():
            if item_id in seen:
                continue
            seen.add(item_id)
        else:
            item_id = None
        held.append(item_id)
    return {"shown": shown, "held": held}


def _moments(calendar_rows, payload, market):
    out = [{"date": r["moment_date"], "name": r["name"], "kind": r["kind"], "source": r["source"],
            "item_ids": list(r.get("item_ids") or [])}
           for r in calendar_rows if r["market"] == market]
    seen = {(m["date"], m["name"]) for m in out}
    out += [m for m in payload.get("moments") or [] if (m.get("date"), m.get("name")) not in seen]
    return sorted(out, key=lambda m: (m["date"], m["name"]))


def _not_assessed(payload, excluded=()):
    block = payload.get("not_assessed")
    if not isinstance(block, dict):
        return None
    detect, pool, judged, count, raw = (block.get(key) for key in
        ("detect_run_id", "pool_limit", "judged_limit", "count", "items"))
    if (not isinstance(detect, str) or not detect.strip() or type(pool) is not int or pool <= 0
            or type(judged) is not int or judged <= 0 or type(count) is not int
            or not isinstance(raw, list) or count != len(raw)):
        return None
    represented = set(excluded)
    represented.update(c.get("item_id") for key in ("cards", "more") for c in payload.get(key) or []
                       if isinstance(c, dict))
    held = payload.get("held_back") if isinstance(payload.get("held_back"), dict) else {}
    represented.update(c.get("item_id") for c in held.get("items") or [] if isinstance(c, dict))
    represented.update(item_id for moment in payload.get("moments") or [] if isinstance(moment, dict)
                       for item_id in moment.get("item_ids") or [])
    items, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            return None
        item_id, title, reason, rank, pool_rank, scope = (item.get(key) for key in
            ("item_id", "title", "reason", "sql_rank", "pool_rank", "market_scope"))
        if (not isinstance(item_id, str) or not item_id.strip() or item_id in seen
                or not isinstance(title, str) or not title.strip() or item.get("status") != "not_assessed"
                or not isinstance(reason, str) or reason not in NOT_ASSESSED_REASONS
                or type(rank) is not int or rank <= 0
                or scope not in ("market", "global", None)
                or (reason == "outside_candidate_pool" and (rank <= pool or pool_rank is not None))
                or (reason != "outside_candidate_pool" and (rank > pool or type(pool_rank) is not int
                                                           or not 1 <= pool_rank <= pool))):
            return None
        seen.add(item_id)
        if item_id in represented:
            continue
        items.append({"item_id": item_id, "title": title, "status": "not_assessed", "reason": reason,
            "reason_text": NOT_ASSESSED_REASONS[reason], "sql_rank": rank, "pool_rank": pool_rank,
            "market_scope": scope})
    return {"detect_run_id": detect, "pool_limit": pool, "judged_limit": judged, "count": len(items), "items": items}


def _dropped(payload, prev, today_ids, moments):
    if prev is None:
        return {"first_morning": True, "text": FIRST_MORNING, "items": []}
    held = {i["item_id"]: i for i in (payload.get("held_back") or {}).get("items") or []}
    seasonal_ids = {i for m in moments for i in m.get("item_ids") or []}
    audit = _not_assessed(payload, today_ids | seasonal_ids)
    unassessed = {item["item_id"] for item in audit["items"]} if audit is not None else set()
    items = []
    for c in _entries(prev[1]["payload"]):
        if c["item_id"] in today_ids:
            continue
        if c["item_id"] in held:
            reason = "held_back"
        elif c["item_id"] in seasonal_ids:
            reason = "reclassified_seasonal"
        elif c["item_id"] in unassessed:
            reason = "not_assessed"
        else:
            reason = "not_confirmed"
        words = DROPPED_WORDS[reason]
        if reason == "held_back":
            why = _plain_reason(dict(held[c["item_id"]])).get("reason_text")
            words = f"Held back today: {why.strip()}" if isinstance(why, str) and why.strip() else HELD_SEE_BELOW
        items.append({"item_id": c["item_id"], "title": c["title"], "reason": reason, "reason_text": words})
    text = f"Dropped since yesterday: {len(items)}" if items else "Nothing dropped since yesterday"
    return {"first_morning": False, "text": text, "items": items}


def _sentence(snake):
    return snake.replace("_", " ").strip().capitalize()


def _held_item(item, creator_labels):
    raw_evidence = item.get("evidence")
    evidence_records = []
    if isinstance(raw_evidence, list):
        for record in raw_evidence:
            if not isinstance(record, dict):
                continue
            normalized = dict(record)
            if normalized.get("platform") is not None and not isinstance(normalized["platform"], str):
                normalized["platform"] = None
            evidence_records.append(normalized)
    evidence = _platform_x(evidence_records)
    if "platform" in item:
        platform = item["platform"]
        out_platform = canon_platform(platform) if isinstance(platform, str) else None
    else:
        out_platform = None
    raw_platforms = item.get("platforms")
    platforms = ([canon_platform(platform) for platform in raw_platforms if isinstance(platform, str)]
                 if isinstance(raw_platforms, list) else None)
    out = dict(item, title=_item_title(item, evidence, creator_labels))
    if "count_line" in item:
        out["count_line"] = _one_count(item["count_line"])
    if "platform" in item:
        out["platform"] = out_platform
    if "platforms" in item:
        out["platforms"] = platforms or []
    out["evidence"] = evidence
    evidence_ids = item.get("evidence_ids")
    if not isinstance(evidence_ids, list):
        evidence_ids = [record["id"] for record in evidence if isinstance(record.get("id"), str)]
    out["evidence_ids"] = evidence_ids
    out.setdefault("rule", None)
    return _plain_reason(out)


def _reader_held_item(card, creator_labels):
    out = _held_item(card, creator_labels)
    failed_reason = card.get("failed_reason")
    if isinstance(failed_reason, str) and failed_reason.strip():
        out["failed_reason"] = failed_reason.strip()
    else:
        out.pop("failed_reason", None)
    specificity = card.get("specificity")
    specificity = specificity if isinstance(specificity, dict) else {}
    reason = specificity.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = card.get("reason")
        reason = reason if isinstance(reason, str) and reason.strip() else None
    reason_text = specificity.get("reason_text")
    if not isinstance(reason_text, str) or not reason_text.strip():
        reason_text = card.get("reason_text")
    if not isinstance(reason_text, str) or not reason_text.strip():
        if reason:
            reason_text = _sentence(reason)
        else:
            reason_text = _today_card_fault(card) or "Reason not provided"
    out.pop("reason_raw", None)
    out["reason"] = reason
    out["reason_text"] = reason_text
    return _plain_reason(out)


def _series_name(row):
    series, platform = row["series"], row.get("platform")
    if series in SERIES_WORDS:
        return SERIES_WORDS[series]
    if not platform:
        return CROSS_SERIES_WORDS.get(series) or _sentence(series)
    platform_word = PLATFORM_WORDS.get(platform) or _sentence(platform)
    if series in PLATFORM_SERIES_WORDS:
        return f"{platform_word} {PLATFORM_SERIES_WORDS[series]}"
    return f"{platform_word}: {_sentence(series)}"


def _with_brief_issues(coverage, payload):
    """The brief's own issues (L2) follow the collection ones, each once."""
    extra = ((payload.get("coverage") or {}).get("issues")) or []
    issues = coverage["issues"] + [i for i in extra if isinstance(i, str) and i not in coverage["issues"]]
    return {**coverage, "issues": issues}


def _coverage(rows):
    valid = [r for r in rows if r["valid"]]
    invalid = [r for r in rows if not r["valid"]]
    weighted = [r for r in valid if r.get("located_share") is not None and r.get("items")]
    weight = sum(r["items"] for r in weighted)
    share = round(sum(r["items"] * r["located_share"] for r in weighted) / weight, 4) if weight else None
    issues, news_done = [], False
    for r in invalid:
        if r.get("series") == NEWS_SERIES:
            if not news_done:
                issues += _news_issues(rows)
                news_done = True
            continue
        issues.append(f"{_series_name(r)}: {INVALID_WORDS.get(invalid_code(r), 'not usable')}")
    return {"posts": sum(r.get("items") or 0 for r in valid) if valid else None,
            "platforms": sorted({PLATFORM_IDS.get(r["platform"], r["platform"]) for r in valid if r.get("platform")}),
            "located_share": share, "invalid_series": len(invalid), "issues": issues}


def _news_feed_name(row):
    """The publication a news_rss row read: the catalog name of a public feed (protocol public_feed?feed_id=...),
    else the host of the feed URL in the protocol (rss?url=...), else "News RSS"."""
    query = str(row.get("protocol") or "").partition("?")[2]
    params = parse_qs(query) if query else {}
    feed_id = (params.get("feed_id") or [""])[0]
    if feed_id:
        name = next((f.name for f in FEEDS if f.feed_id == feed_id), None)
        if name:
            return name
    url = unquote((params.get("url") or [""])[0])
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return {"ewn.co.za": "EWN"}.get(host, host or "News RSS")


def _news_failure(row):
    reason = str(row.get("invalid_reason") or "")
    classes = [c.strip() for c in reason.partition(":")[2].split(",") if c.strip()]
    return NEWS_FEED_FAILURES.get(classes[0]) if len(classes) == 1 else None


def _news_issues(rows):
    """The market's news feed lines: the feeds not read (with why, when the class is known) and how many of the
    feeds were read, then any read feed judged unusable for another reason, by name."""
    news = [r for r in rows if r.get("series") == NEWS_SERIES]
    unread = [r for r in news if not r["valid"] and invalid_code(r) == "calls"]
    other = [r for r in news if not r["valid"] and invalid_code(r) != "calls"]
    out = []
    if unread:
        named = {}
        for r in unread:
            why = _news_failure(r)
            named.setdefault(_news_feed_name(r), why)
        parts = [f"{n} ({named[n]})" if named[n] else n for n in sorted(named, key=str.casefold)]
        out.append(f"{SERIES_WORDS[NEWS_SERIES]}: {len(news) - len(unread)} of {len(news)} read; "
                   f"could not read {', '.join(parts)}")
    words = {}
    for r in other:
        words.setdefault(INVALID_WORDS.get(invalid_code(r), "not usable"), set()).add(_news_feed_name(r))
    for word, names in words.items():
        out.append(f"{SERIES_WORDS[NEWS_SERIES]}: {word}: {', '.join(sorted(names, key=str.casefold))}")
    return out


def _failed_source_name(row):
    if row.get("series") == "ig_location":
        return "Instagram locations"
    if row.get("series") == NEWS_SERIES:
        return _news_feed_name(row)
    return _series_name(row)


def _failed_sources(rows):
    names = set()
    for row in rows:
        calls, calls_ok = row.get("calls"), row.get("calls_ok")
        if (invalid_code(row) == "calls" and calls is not None and calls > 0 and calls_ok is not None
                and calls_ok < calls):
            names.add(_failed_source_name(row))
    return sorted(names, key=str.casefold)


def _latest_run(runs):
    def key(row):
        started = row.get("started_at") or row.get("finished_at")
        finished = row.get("finished_at")
        started = started.isoformat() if hasattr(started, "isoformat") else str(started or "")
        finished = finished.isoformat() if hasattr(finished, "isoformat") else str(finished or "")
        return started, row.get("finished_at") is not None, finished, str(row.get("run_id") or "")

    return max(runs, key=key, default=None)


# core/understand/job.py records a run whose clusterer failed as ok but partial; the page says so in these words.
TOPICS_PARTIAL_REASON = "cluster_stack_failed"
TOPICS_FAILED_TEXT = "Data issue: topic grouping failed today"


def _newest_ok_run(runs):
    """The newest run with status ok by finished_at, the run core/detect/job.py topics_failed_today reads, so a failed
    rerun after an ok run does not hide what that run said."""
    def key(row):
        finished = row.get("finished_at")
        return (finished.isoformat() if hasattr(finished, "isoformat") else str(finished or "")), str(row.get("run_id") or "")

    return max((r for r in runs if r.get("status") == "ok" and r.get("finished_at") is not None), key=key, default=None)


def _topics_failed(runs):
    """True when the day's newest ok understand run says the topic grouping failed (partial_reason
    cluster_stack_failed), the same run detect reads before it stops topics."""
    latest = _newest_ok_run(runs)
    counts = latest.get("counts") if latest else None
    if isinstance(counts, str):
        try:
            counts = json.loads(counts)
        except ValueError:
            return False
    return (isinstance(counts, dict) and counts.get("partial") is True
            and counts.get("partial_reason") == TOPICS_PARTIAL_REASON)


def _stage_failed(runs):
    latest = _latest_run(runs)
    return latest is not None and latest.get("status") == "failed"


def _warmup(store, date):
    first = store.first_ok_collect_date()
    if first is None:
        return {"active": True, "day": 0, "of": WARMUP_DAYS, "text": "Warming up: collection has not started"}
    day = (dt.date.fromisoformat(date) - dt.date.fromisoformat(first)).days + 1
    return {"active": day <= WARMUP_DAYS, "day": day, "of": WARMUP_DAYS,
            "text": f"Warming up: day {day} of {WARMUP_DAYS}"}


def _collect_ok(store, date):
    return any(r["status"] == "ok" for r in store.runs("collect", date))


def _run_receipt(collect_runs, rows, markets):
    """Posts from the latest ok collect run's stored counts, and what the brief rows show and hold. None when any
    count is missing, so nothing is estimated."""
    latest = _latest_run([r for r in collect_runs if r.get("status") == "ok"])
    counts = latest.get("counts") if latest else None
    if isinstance(counts, str):
        try:
            counts = json.loads(counts)
        except ValueError:
            return None
    posts = counts.get("posts") if isinstance(counts, dict) else None
    if type(posts) is not int or posts < 0 or any(m not in rows for m in MARKETS):
        return None
    return {"posts": posts, "markets": [LABELS[m] for m in MARKETS],
            "shown": sum(len(m["cards"]) + len(m["more"]) for m in markets),
            "held": sum(m["held_back"]["count"] for m in markets), "collect_run_id": latest.get("run_id")}


def _overall(rows, collect_ok):
    statuses = [rows[m]["status"] if m in rows else None for m in MARKETS]
    if not collect_ok or all(s in (None, "data_issue") for s in statuses):
        return "data_issue"
    if all(s == "published" for s in statuses):
        return "published"
    return "partial"


def _headline(markets):
    candidates = [(m, m.pop("_headline", None) or {}) for m in markets]
    for m, h in candidates:
        if not isinstance(h, dict):
            continue
        item_id, text, headline_claim_ids = h.get("item_id"), h.get("text"), h.get("claim_ids")
        if (not isinstance(item_id, str) or not item_id.strip() or not isinstance(text, str) or not text.strip()
                or not isinstance(headline_claim_ids, list) or not headline_claim_ids
                or any(not isinstance(i, str) or not i.strip() for i in headline_claim_ids)):
            continue
        card = next((c for c in m["cards"] + m["more"] if c["item_id"] == item_id), None)
        if card is None or card["explained"] is not True:
            continue
        claim_ids = {c["id"] for c in card["claims"] if isinstance(c, dict) and isinstance(c.get("id"), str)}
        if not set(headline_claim_ids) <= claim_ids:
            continue
        return {"text": text, "market": m["market"], "item_id": item_id, "claim_ids": list(headline_claim_ids)}
    return None


def _market(store, market, date, row, health_rows, calendar_rows, warmup, collect_ok, stage_failed, creator_labels,
            day="today", topics_failed=False):
    payload = row.get("payload") if row and isinstance(row.get("payload"), dict) else {}
    status = row["status"] if row else "data_issue"
    prev = _prev_ranks(store, market, date) if row else None
    cards, more, today_ids, rejected_cards = _today_cards(payload, market, date, prev)
    for card in cards + more:
        if card.get("kind") == "creator" and _is_youtube_channel_id(card.get("title")):
            card["title"] = _item_title(card, card.get("evidence"), creator_labels)
    moments = _moments(calendar_rows, payload, market)
    rows = [r for r in health_rows if r["market"] == market]
    held = payload.get("held_back")
    held = held if isinstance(held, dict) else {}
    held_items = []
    held_ids = set()
    explicit_items = held.get("items")
    explicit_items = explicit_items if isinstance(explicit_items, list) else []
    rejected_by_id = {}
    for card in rejected_cards:
        if isinstance(card, dict):
            item_id = card.get("item_id")
            if isinstance(item_id, str) and item_id.strip():
                rejected_by_id.setdefault(item_id, card)
    for h in explicit_items:
        if not isinstance(h, dict):
            continue
        item_id = h.get("item_id")
        rejected = rejected_by_id.get(item_id) if isinstance(item_id, str) else None
        if isinstance(rejected, dict):
            enriched = dict(h)
            if "numbers" not in enriched:
                numbers = rejected.get("numbers")
                valid_numbers = isinstance(numbers, list)
                if valid_numbers:
                    for number in numbers:
                        if not isinstance(number, dict):
                            valid_numbers = False
                            break
                        value = number.get("value")
                        valid_value = (value is None or (isinstance(value, int) and not isinstance(value, bool))
                                       or (isinstance(value, float) and math.isfinite(value)))
                        if (not valid_value or not all(
                                isinstance(number.get(field), str) and number[field].strip()
                                for field in ("unit", "query_id", "run_id", "result_hash"))):
                            valid_numbers = False
                            break
                if valid_numbers:
                    enriched["numbers"] = numbers
            for field in ("count_line", "failed_reason"):
                value = rejected.get(field)
                if field not in enriched and isinstance(value, str) and value.strip():
                    enriched[field] = value
            h = enriched
        if isinstance(item_id, str) and item_id.strip():
            if item_id in held_ids:
                continue
            held_ids.add(item_id)
        held_items.append(_held_item(h, creator_labels))
    for card in rejected_cards:
        item_id = card["item_id"]
        if item_id in held_ids:
            continue
        held_ids.add(item_id)
        held_items.append(_reader_held_item(card, creator_labels))
    held_text = (f"{len(held_items)} item{'s' if len(held_items) != 1 else ''} held back"
                 if held_items else "Nothing held back")
    represented = today_ids | held_ids | {i for m in moments for i in m.get("item_ids") or []}
    not_assessed = (_not_assessed(payload, represented)
                    if getattr(store, "_hidden", ()) is not None else None)

    banners = []
    if warmup["active"]:
        banners.append({"kind": "warming_up", "text": warmup["text"]})
    valid = sum(1 for r in rows if r["valid"])
    if rows and valid < THIN_COVERAGE * len(rows):
        banners.append({"kind": "thin_coverage",
                        "text": f"Thin coverage: {valid} of {len(rows)} collection series usable today"})
    if row is None:
        banners.append({"kind": "data_issue", "text": f"Data issue: no brief was published for {LABELS[market]}"})
    if stage_failed:
        banners.append({"kind": "data_issue",
                        "text": f"Data issue: collection or detection failed for {LABELS[market]}"})
    elif not collect_ok:
        banners.append({"kind": "data_issue", "text": "Data issue: no successful collection run is recorded for this date"})

    failed_sources = _failed_sources(rows)
    if failed_sources:
        n = len(failed_sources)
        noun = "source" if n == 1 else "sources"
        banners.append({"kind": "data_issue", "text": f"{n} {noun} failed today: {', '.join(failed_sources)}"})

    if topics_failed:
        banners.append({"kind": "data_issue", "text": TOPICS_FAILED_TEXT})

    existing_kinds = {b["kind"] for b in banners}
    payload_banners = []
    for banner in payload.get("banners") or []:
        if banner.get("kind") in {"warming_up", "thin_coverage"} and banner.get("kind") in existing_kinds:
            continue
        if (banner.get("kind") == "data_issue"
                and str(banner.get("text") or "").startswith(
                    "Data issue: the steps before the brief did not finish by 06:15")):
            if stage_failed:
                continue
            banner = dict(banner, text=(f"Data issue: the brief reports an upstream issue for {LABELS[market]}; "
                                        "no failed stage is recorded"))
        payload_banners.append(banner)
    banners.extend(payload_banners)
    has_payload_issue = any(b.get("kind") == "data_issue" for b in payload_banners)
    if (row is not None and status == "data_issue" and not stage_failed and not failed_sources and collect_ok
            and not has_payload_issue):
        banners.append({"kind": "data_issue", "text": f"Data issue: today's brief is incomplete for {LABELS[market]}"})

    return {
        "market": market, "label": LABELS[market], "status": status, "banners": banners,
        "cards": cards, "more": more,
        "dropped": _dropped(dict(payload, not_assessed=not_assessed), prev, today_ids, moments),
        "held_back": {"count": len(held_items), "text": held_text, "items": held_items},
        "not_assessed": not_assessed,
        "moments": moments,
        "boards": _boards(payload.get("boards"), day),
        "coverage": _with_brief_issues(_coverage(rows), payload),
        "_headline": payload.get("headline"),
    }


def _check_rows(store, keys):
    """The brief's failed claim_checks rows for these answer_or_brief_ids: one bounded, read-only query. A store
    without SQL (the fixtures) gives no rows. A failed read gives None, so the caller can tell the detail could
    not be read from no detail stored, and Today still renders."""
    query, table = getattr(store, "_query", None), getattr(store, "_t", None)
    if not keys or not callable(query) or not callable(table):
        return []
    try:
        return query(
            "SELECT c.answer_or_brief_id, c.claim_id, c.rule, c.verdict, c.reason "
            f"FROM {table('intelligence_42_agent.claim_checks')} c "
            f"WHERE c.verdict != 'pass' AND c.answer_or_brief_id IN UNNEST(@ids) LIMIT {CHECK_LIMIT}",
            ids=("ARRAY<STRING>", list(keys)))
    except Exception as e:  # noqa: BLE001 - the detail is optional; Today must not fail on it
        print(f"today: claim checks read failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _critic_part(reason):
    """(menu or "", local why-now failed, simpler explanation not ruled out) from a stored critic reason. Reasons
    written before the critic scope fix never name the why-now part."""
    if reason == CRITIC_WHY_NOW:
        return "", True, False
    why_now = reason.endswith(CRITIC_ALSO_WHY_NOW)
    if why_now:
        reason = reason[:-len(CRITIC_ALSO_WHY_NOW)]
    return reason[len(CRITIC_HELD):].removeprefix(": ").strip(), why_now, True


def _critic_sentence(rows):
    parts = {_critic_part(r["reason"]) for r in rows}
    if len(parts) != 1:
        return CRITIC_UNNAMED
    menu, why_now, simpler = parts.pop()
    if not simpler:
        return WHY_NOW_UNSHOWN
    if menu in CRITIC_MENU_WORDS:
        named = f"{menu[0].upper()}{menu[1:]} could explain this"
        if why_now:
            return (f"{named}, the posts do not rule it out, and they do not show why this is happening in this "
                    "market now.")
        return f"{named}, and the posts do not rule it out."
    if why_now:
        return ("A simpler explanation could not be ruled out from these posts, and they do not show why this is "
                "happening in this market now.")
    return CRITIC_UNNAMED


def _is_critic_hold(row):
    return row.get("rule") == "critic" and (row["reason"] == CRITIC_WHY_NOW or row["reason"].startswith(CRITIC_HELD))


def _check_sentence(row):
    """A check's stored fixed wording without its check name, as a sentence."""
    words = row["reason"].partition(": ")[2].strip()
    return f"{words[0].upper()}{words[1:]}." if words else None


def _rule_order(row):
    rule = row.get("rule")
    return (CHECK_RULES.index(rule) if rule in CHECK_RULES else len(CHECK_RULES), str(rule), row["reason"])


def _held_detail(rows):
    """One plain sentence on the stored check that held the item, or None when no stored row held it. Follows
    core/brief/job.py failed_reason: a breach first, then the sentence's support check, the place fault, the
    critic, any other fault in the sentence, then a cut claim. Which claims the sentence rests on is not stored,
    so cut claims come last. Rows from before the repair round did not hold the card."""
    rows = [r for r in rows if isinstance(r, dict) and r.get("verdict") in ("cut", "breach")
            and isinstance(r.get("reason"), str) and not r["reason"].startswith(CHECK_REPAIR)
            and r.get("rule") not in CHECK_DOWNGRADES and r.get("rule") != CHECK_TITLE]
    breaches = sorted((r for r in rows if r["verdict"] == "breach"), key=_rule_order)
    if breaches:
        if breaches[0].get("rule") == "critic":
            return _critic_sentence([r for r in breaches if _is_critic_hold(r)] or breaches[:1])
        return _check_sentence(breaches[0])
    sentence = [r for r in rows if r.get("claim_id") is None]
    for held in (lambda r: r.get("rule") == "K4", lambda r: r.get("rule") == "K3"):
        row = next((r for r in sentence if held(r)), None)
        if row is not None:
            return _check_sentence(row)
    critic = [r for r in rows if _is_critic_hold(r)]
    if critic:
        return _critic_sentence(critic)
    for group in ([r for r in sentence if r.get("rule") != "critic"],
                  [r for r in rows if r.get("claim_id") is not None]):
        if group:
            return _check_sentence(min(group, key=_rule_order))
    return None


def _with_held_details(store, rows, markets):
    keys = {}
    for m in markets:
        row = rows.get(m["market"])
        run_id = row.get("run_id") if row else None
        if not isinstance(run_id, str) or not run_id:
            continue
        for item in m["held_back"]["items"]:
            item_id = item.get("item_id")
            if isinstance(item_id, str) and item_id.strip():
                keys.setdefault(f"{run_id}:{m['market']}:{item_id}", []).append(item)
    found = _check_rows(store, list(keys))
    if found is None:
        for items in keys.values():
            for item in items:
                item["check_detail"] = "unavailable"
        return
    by_key = {}
    for r in found:
        if isinstance(r, dict) and r.get("answer_or_brief_id") in keys:
            by_key.setdefault(r["answer_or_brief_id"], []).append(r)
    for key, items in keys.items():
        detail = _held_detail(by_key.get(key, []))
        if detail is not None:
            for item in items:
                item["held_detail"] = detail


def _ago_text(end, now):
    hours = int((now - end).total_seconds() // 3600)
    if hours < 1:
        return "In the last hour"
    return "1 hour ago" if hours == 1 else f"{hours} hours ago"


def _breaking_hidden(kind, key, hidden):
    """True when a creator item names a suppressed creator, or any creator while the list cannot be read."""
    if kind != "creator":
        return False
    if hidden is None:
        return True
    platform, _, handle = str(key or "").partition(":")
    return creator_key(platform, handle) in hidden[0]


def _breaking(store, hidden, now):
    """{market: [breaking line]} from v_breaking_signals_current: each item once per market at its latest hour in
    the last BREAKING_HOURS hours, newest first, in plain words. A suppressed creator is left out and their handle
    masked; while the suppression list cannot be read no creator item is listed and no handle is written. {} when
    there is nothing or the read fails, so Today renders without the strip."""
    since = now.replace(second=0, microsecond=0) - dt.timedelta(hours=BREAKING_HOURS)
    try:
        rows = store.breaking_signals(since.isoformat())
        if not rows:
            return {}
        latest = {}
        for r in rows:
            hour = dt.datetime.fromisoformat(str(r["hour"])).astimezone(SAST)
            key = (r.get("market"), r.get("item_id"))
            if (key[0] not in MARKETS or not isinstance(key[1], str) or not key[1].strip() or not since <= hour <= now):
                continue
            if key not in latest or hour > latest[key][0]:
                latest[key] = (hour, r)
        if not latest:
            return {}
        mapped = store.map_items(sorted({item for _, item in latest}))
        names = {m["item_id"]: m for m in mapped or [] if isinstance(m, dict) and m.get("item_id")}
        lines = []
        for (market, item_id), (hour, r) in sorted(latest.items(), key=lambda kv: (-kv[1][0].timestamp(), kv[0])):
            row = names.get(item_id) or {}
            kind = row.get("kind") if isinstance(row.get("kind"), str) else None
            if _breaking_hidden(kind, row.get("canonical_key"), hidden):
                continue
            label = row.get("label")
            label = label if isinstance(label, str) and _post_id(label) is None else None
            end = hour + dt.timedelta(hours=1)
            new = r.get("ratio") is None or not r.get("expected6")
            lines.append({"item_id": item_id, "market": market, "market_label": LABELS[market], "kind": kind,
                          "title": _display_title([label], kind=kind), "time_text": f"{end:%H:%M}",
                          "ago_text": _ago_text(end, now), "note": BREAKING_NEW if new else None})
        if hidden is None:
            lines = [dict(b, title=_HANDLE.sub(lambda m: "@***", b["title"])) for b in lines]
        else:
            lines = without_hidden({"breaking": lines}, hidden)["breaking"]
    except Exception as exc:  # noqa: BLE001 - Breaking is extra; Today must not fail on it
        log.warning("today: the breaking read failed (%s); Today is shown without it", type(exc).__name__)
        return {}
    out = {}
    for b in lines:
        out.setdefault(b["market"], []).append(b)
    return out


def build_today(store, date=None, now=None):
    now = (now or dt.datetime.now(SAST)).astimezone(SAST)
    current = date is None or date == now.date().isoformat()
    search_day = now.date().isoformat() if current else date
    store = _WithoutHidden(store)
    date = date or store.latest_brief_date()
    if date is None:
        raise NotReady("No brief has been published yet")
    rows = _rows_by_market(store, date)
    health_rows = store.collection_health(date)
    end = (dt.date.fromisoformat(date) + dt.timedelta(days=MOMENT_DAYS)).isoformat()
    calendar_rows = store.calendar(date, end)
    warmup = _warmup(store, date)
    collect_runs = store.runs("collect", date)
    detect_runs = store.runs("detect", date)
    collect_ok = any(r.get("status") == "ok" for r in collect_runs)
    stage_failed = _stage_failed(collect_runs) or _stage_failed(detect_runs)
    creator_labels = _today_creator_labels(store, rows)
    day = "today" if current else f"on {_long_date(date)}"
    topics_failed = _topics_failed(store.runs("understand", date))
    markets = [_market(store, m, date, rows.get(m), health_rows, calendar_rows, warmup, collect_ok, stage_failed,
                       creator_labels, day, topics_failed)
               for m in MARKETS]
    _with_held_details(store, rows, markets)
    breaking = _breaking(store, store._hidden, now) if current else {}
    for m in markets:
        m["breaking"] = breaking.get(m["market"], [])
    headline = _headline(markets)
    receipt = _run_receipt(collect_runs, rows, markets)
    published = [dt.datetime.fromisoformat(r["published_at"]) for r in rows.values() if r.get("published_at")]
    return {
        "date": date,
        "status": _overall(rows, collect_ok),
        "published_at": max(published).astimezone(SAST).isoformat() if published else None,
        "heading": f"{'Taking off' if any(m['cards'] + m['more'] for m in markets) else 'Today'}, {_long_date(date)}",
        "headline": headline,
        "warmup": warmup,
        "first_morning": all(m["dropped"]["first_morning"] for m in markets),
        "markets": markets,
        "searching_now": searching_now(store, MARKETS, search_day, store._hidden),
        **({"run_receipt": receipt} if receipt else {}),
    }


def _market_row(store, market, date):
    if market not in MARKETS:
        raise NotFound(f"Unknown market {market}")
    date = date or store.latest_brief_date()
    if date is None:
        raise NotReady("No brief has been published yet")
    row = _rows_by_market(store, date).get(market)
    if row is None:
        raise NotFound(f"No brief for {market} on {date}")
    return row, date


def build_trends(store, market, date=None):
    store = _WithoutHidden(store)
    row, date = _market_row(store, market, date)
    cards, more = _cards(row["payload"], market, date, _prev_ranks(store, market, date))
    return {"date": date, "market": market, "cards": cards + more}


def _trend_held(h):
    """A held item's reason as Today words it: the job's failed_reason goes through, so a topic a busy model left
    unexplained reads as busy here too, not as a failed check."""
    out = {"rule": h.get("rule"), "reason": h.get("reason"), "reason_text": h.get("reason_text")}
    if isinstance(h.get("explanation_status"), str):
        out["explanation_status"] = h["explanation_status"]
    failed_reason = h.get("failed_reason")
    if isinstance(failed_reason, str) and failed_reason.strip():
        out["failed_reason"] = failed_reason.strip()
    return _plain_reason(out)


def build_trend(store, item_id, market, date=None):
    store = _WithoutHidden(store)
    row, date = _market_row(store, market, date)
    payload = row["payload"]
    cards, more = _cards(payload, market, date, _prev_ranks(store, market, date))
    for c in cards + more:
        if c["item_id"] == item_id:
            return c
    for h in (payload.get("held_back") or {}).get("items") or []:
        if h["item_id"] == item_id:
            evidence = _platform_x(h.get("evidence"))
            title = _display_title([h.get("title")], evidence=evidence)
            return {
                "item_id": item_id, "market": market, "date": date, "rank": None, "kind": h.get("kind"),
                "title": title, "tag": None, "state": None, "state_word": None, "flag": None,
                "flag_word": None, "explained": False, "explanation": None, "explanation_claim_ids": [],
                "claims": [], "count_line": None, "numbers": h.get("numbers") or [], "sparkline": None,
                "thumbnails": [e["id"] for e in evidence[:2]],
                "evidence_ids": h.get("evidence_ids") or [e["id"] for e in evidence],
                "evidence": evidence,
                "ask": f"What is behind {title} in {LABELS[market]} this week?",
                "held_back": _trend_held(h),
            }
    raise NotFound(f"{item_id} is not in the {market} brief for {date}")


def today_status(store, date):
    rows = store.briefs(date)
    if not rows:
        return "not_ready"
    overall = _overall({r["market"]: r for r in rows}, _collect_ok(store, date))
    return "data_issue" if overall == "data_issue" else "published"
