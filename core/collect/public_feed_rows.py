from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
import html
import re
from urllib.parse import urlencode, urlsplit, urlunsplit

from core.collect.ids import post_id
from core.collect.local_sources import _item, _row, song_key
from core.collect.parse import COUNTER_COLUMNS, OBSERVATION_COLUMNS, POST_COLUMNS, _local_day, parse_time
from core.public_feeds.catalog import confirmed_feeds


_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]*>")
_EMBEDDED_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"\b(?:access[_-]?token|api[_-]?key|authorization|password|secret|token)\s*[:=]\s*[^\s&\"']+", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)$", re.IGNORECASE)
_CHART_PLATFORMS = {
    "ke_mdundo_top_songs": "mdundo",
    "ng_turntable_top_100": "turntable",
}


def feed_protocol(feed):
    if feed not in confirmed_feeds():
        raise ValueError("feed must be a confirmed catalog entry")
    return "public_feed?" + urlencode({"feed_id": feed.feed_id, "url": feed.url})


def _plain(value):
    if not isinstance(value, str):
        return ""
    text = html.unescape(value)
    text = _SCRIPT_STYLE.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    text = _EMBEDDED_URL.sub("[url omitted]", text)
    text = _SECRET_VALUE.sub("[redacted]", text)
    text = _BEARER.sub("[redacted]", text)
    return " ".join(text.split())


def _safe_url(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if (scheme not in {"http", "https"} or not parts.hostname or "@" in parts.netloc or parts.query):
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parts.path or "/", "", ""))


def _aware_time(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value.strip()):
        return None
    parsed = parse_time(value.strip())
    return parsed.astimezone(UTC) if parsed is not None else None


def normalise(feed, entries, *, observed_at, run_id, pull_seq, item_id_fn):
    if feed not in confirmed_feeds():
        raise ValueError("feed must be a confirmed catalog entry")
    observed = _aware_time(observed_at)
    if observed is None:
        raise ValueError("observed_at must include a timezone")
    observed_iso = observed.isoformat()
    observed_date = _local_day(observed, feed.market)
    source_url = _safe_url(feed.url)
    if source_url is None:
        raise ValueError("catalog feed URL must be anonymous HTTP or HTTPS")

    platform = "news" if feed.kind == "news" else "radio" if feed.kind == "playlist" else _CHART_PLATFORMS.get(feed.feed_id)
    series = {"news": "news_rss", "chart": "board_music_country", "playlist": "radio_playlist"}[feed.kind]
    posts, observations, counters, items = {}, {}, {}, {}
    safe_entries = []
    dropped = Counter()

    for entry in entries:
        if not isinstance(entry, Mapping):
            dropped["malformed_entry"] += 1
            continue

        text = _plain(entry.get("text"))
        artist = _plain(entry.get("artist"))
        raw_url = entry.get("url")
        url = _safe_url(raw_url)
        reason = None
        item = None
        if feed.kind == "news":
            if not text:
                reason = "missing_news_title"
            elif not url:
                reason = "missing_url" if not raw_url else "unsafe_url"
            pid = post_id("news", url=url) if url and reason is None else None
            published = _aware_time(entry.get("published_at"))
            published_iso = published.isoformat() if published is not None else None
            date_basis = "published_at" if published is not None else "observed_at"
            post_date = _local_day(published, feed.market) if published is not None else observed_date
            item_key = None
            rank = None
            time_text = ""
        else:
            pid = None
            raw_song = song_key({"artist": artist, "title": text}) if text else ""
            item_key = raw_song or None
            rank_value = entry.get("rank")
            rank = rank_value if isinstance(rank_value, int) and not isinstance(rank_value, bool) and rank_value > 0 else None
            time_text = _plain(entry.get("time_text"))
            published_iso = None
            date_basis = None
            post_date = None
            if feed.kind == "chart" and platform is None:
                reason = "unknown_chart_feed"
            elif not raw_song:
                reason = "missing_song_identity"
            elif feed.kind == "chart" and rank is None:
                reason = "missing_rank"
            if reason is None:
                item = _item(item_id_fn, "sound", raw_song, platform)
                if not isinstance(item, str) or not item:
                    reason = "item_identity_rejected"

        safe = {
            "kind": feed.kind,
            "platform": platform,
            "post_id": pid,
            "url": url,
            "text": text,
            "published_at": published_iso,
            "source_market": feed.market,
            "source_feed_id": feed.feed_id,
            "source_url": source_url,
            "observed_at": observed_iso,
            "artist": artist or None,
            "item_key": item_key,
            "rank": rank,
            "time_text": time_text or None,
            "reason": reason,
        }
        safe_entries.append(safe)
        if reason is not None:
            dropped[reason] += 1
            continue

        protocol = feed_protocol(feed)
        if feed.kind == "news":
            post = _row(
                POST_COLUMNS,
                post_id=pid,
                platform="news",
                url=url,
                text=text,
                hashtags=[],
                published_at=published_iso,
                post_date=post_date,
                geo_market=None,
                geo_confidence=None,
                geo_source=None,
                geo_scope=None,
                vendor="public_feed",
                endpoint="public_feed",
                source_regime="public_feed",
                vendor_labels={"date_basis": date_basis},
                run_id=run_id,
            )
            if pid in posts:
                dropped["duplicate_post"] += 1
            else:
                posts[pid] = post
            observation_key = (pid, feed.feed_id, protocol, pull_seq)
            if observation_key in observations:
                dropped["duplicate_observation"] += 1
            else:
                observations[observation_key] = _row(
                    OBSERVATION_COLUMNS,
                    post_id=pid,
                    observed_at=observed_iso,
                    observed_date=observed_date,
                    market=feed.market,
                    source_market=feed.market,
                    source_region=None,
                    platform="news",
                    route="public_feed",
                    series="news_rss",
                    protocol=protocol,
                    lane="public_feed",
                    lane_class="context",
                    seed_key=feed.feed_id,
                    pull_seq=pull_seq,
                    run_id=run_id,
                )
        else:
            items.setdefault(item, ("sound", item_key, platform, item_key))
            counter_key = (item, series, protocol, pull_seq, observed_date)
            unit = "rank" if feed.kind == "chart" else "appearances"
            value = float(rank) if feed.kind == "chart" else 1.0
            counter = _row(
                COUNTER_COLUMNS,
                obs_date=observed_date,
                market=feed.market,
                platform=platform,
                item_id=item,
                series=series,
                route="public_feed",
                protocol=protocol,
                is_board=feed.kind == "chart",
                lane_class="unbiased_rank" if feed.kind == "chart" else "context",
                unit=unit,
                pull_seq=pull_seq,
                value=value,
                source="live",
                observed_at=observed_iso,
                available_at=observed_iso,
                run_id=run_id,
            )
            existing = counters.get(counter_key)
            if existing is None:
                counters[counter_key] = counter
            elif feed.kind == "chart" and value < existing["value"]:
                counters[counter_key] = counter
                dropped["duplicate_counter"] += 1
            else:
                dropped["duplicate_counter"] += 1

    return {
        "posts": list(posts.values()),
        "observations": list(observations.values()),
        "counters": list(counters.values()),
        "items": items,
        "safe_entries": safe_entries,
        "dropped": dict(dropped),
    }
