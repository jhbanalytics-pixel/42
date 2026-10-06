"""The Telegram public-channel phase of collect: posts from the vetted channels' web previews (core/public_feeds/
telegram.py), 0 credits. It runs only when telegram.TELEGRAM_READY is True; the collect job never calls run()
otherwise.

For each vetted channel, at most MAX_CHANNELS_PER_RUN a run: robots.txt of t.me is read once a run with the public
feed reader's rules (public_feed_collect._Robots: a disallow, 401, 403, 429, 5xx or no answer means no fetch; a
404 allows every path), then https://t.me/s/<handle> is read through the public feed transport (anonymous HTTPS,
20 second total deadline, 2 MiB body limit, redirects refused, so a channel whose preview is off, which redirects
to its join page, fails as no_preview). Reads are POLITE_DELAY_SECONDS apart, and once the phase has run
PHASE_BUDGET_SECONDS or written MAX_POSTS_PER_RUN posts the rest are recorded not_made.

Rows. Each post published on the run day or the day before (market time) becomes a posts row (platform telegram,
the channel's handle as creator_id, views, no location) and one post_observations row in series panel_telegram,
lane and lane_class panel, with the channel's vetted market as both market and source_market: a sighting in that
market's own feeds, the same mechanism as a gossip page the curated lists hold (local_sources.py, the gossip
panel) and the culture desk (job.py row 23). So the post counts as local by source, one step below a located
post (TRUST.md, market by source; core/trust/claims.py K5), and nothing infers a location from the channel.
One Coverage record and one raw_responses row (parsed safe entries only, never the page) per channel.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from core.collect.ids import post_id
from core.collect.local_sources import _row
from core.collect.parse import CREATOR_COLUMNS, OBSERVATION_COLUMNS, POST_COLUMNS, _local_day
from core.collect.public_feed_collect import _guard, _RunGuard, _Robots, _target_day
from core.collect.public_feed_rows import _plain
from core.public_feeds import reader, telegram

ROW = "telegram"  # not 2.12: the local sources' own counts (job.py local_records) stay as they were
ROUTE = "telegram_preview"
LANE = "panel"
LANE_CLASS = "panel"
SERIES = "panel_telegram"
PLATFORM = "telegram"
SOURCE_REGIME = "telegram_preview"
PARAMS_HASH = hashlib.sha256(b"telegram_preview").hexdigest()
WINDOW_DAYS = 1  # the run day and the day before, like the gossip panel's since=yesterday


def protocol(channel):
    return f"{ROUTE}?channel={channel.handle.casefold()}"


def _raw_row(channel, run_id, fetched_at, http_status, *, status, safe_entries=(), decoded=0, accepted=0,
             dropped=None, reason=""):
    body = {"safe_entries": list(safe_entries), "decoded_count": decoded, "accepted_count": accepted,
            "dropped": dict(dropped or {}), "status": status}
    if reason:
        body["reason"] = reason
    return {"run_id": run_id, "job": "collect", "market": channel.market, "route": ROUTE,
            "params_hash": PARAMS_HASH, "lane": LANE, "seed_key": channel.handle.casefold(),
            "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(), "http_status": http_status,
            "credits_quoted": 0, "credits_charged": 0, "cache_hit": False, "body": body}


def rows(channel, entries, *, observed_at, run_id, target_day):
    """(posts, observations, creator, safe_entries, dropped) for one channel's parsed entries. A post outside the
    window (published before the day before target_day, or after target_day, in the channel's market time) is
    dropped."""
    observed_iso = observed_at.astimezone(timezone.utc).isoformat()
    observed_date = _local_day(observed_at, channel.market)
    first_day = (date.fromisoformat(target_day) - timedelta(days=WINDOW_DAYS)).isoformat()
    handle = channel.handle.casefold()
    posts, observations, safe, dropped = [], [], [], Counter()
    for entry in entries:
        post_date = _local_day(entry["published_at"], channel.market)
        if post_date < first_day:
            dropped["before_window"] += 1
            continue
        if post_date > target_day:
            dropped["after_run_day"] += 1
            continue
        if len(posts) >= telegram.MAX_POSTS_PER_CHANNEL:
            dropped["over_channel_cap"] += 1
            continue
        pid = post_id(PLATFORM, native_id=entry["native_id"])
        text = _plain(entry["text"])
        if pid is None or not text:
            dropped["no_identity_or_text"] += 1
            continue
        published_iso = entry["published_at"].isoformat()
        posts.append(_row(
            POST_COLUMNS, post_id=pid, platform=PLATFORM, native_id=entry["native_id"], url=entry["url"],
            creator_id=handle, text=text, hashtags=list(entry["hashtags"]), published_at=published_iso,
            post_date=post_date, views=entry["views"], geo_market=None, geo_confidence=None, geo_source=None,
            geo_scope=None, vendor="telegram", endpoint="t.me/s", source_regime=SOURCE_REGIME,
            vendor_labels={"forwarded": bool(entry["forwarded"])}, run_id=run_id))
        observations.append(_row(
            OBSERVATION_COLUMNS, post_id=pid, observed_at=observed_iso, observed_date=observed_date,
            market=channel.market, source_market=channel.market, source_region=None, platform=PLATFORM,
            route=ROUTE, series=SERIES, protocol=protocol(channel), lane=LANE, lane_class=LANE_CLASS,
            seed_key=handle, pull_seq=None, rank=None, views=entry["views"], run_id=run_id))
        safe.append({"post_id": pid, "native_id": entry["native_id"], "url": entry["url"],
                     "published_at": published_iso, "views": entry["views"], "source_market": channel.market,
                     "forwarded": bool(entry["forwarded"])})
    creator = _row(CREATOR_COLUMNS, creator_id=handle, platform=PLATFORM, handle=channel.handle,
                   display_name=channel.name, followers=None, verified=None, profile_location=None,
                   first_seen=observed_iso, last_seen=observed_iso) if posts else None
    return posts, observations, creator, safe, dict(dropped)


def run(day, run_id, *, transport, clock, sleep=time.sleep, stopped=None, channels=None) -> dict:
    """Every vetted channel's preview read once: posts, observations, creators, Coverage records, raw rows and a
    summary. Nothing is written here; the collect job appends the rows through its writers."""
    channels = tuple(telegram.CHANNELS if channels is None else channels)[:telegram.MAX_CHANNELS_PER_RUN]
    target_day = _target_day(day)
    robots = _Robots()
    started = clock()
    out = {"posts": [], "observations": [], "creators": [], "records": [], "raw_rows": [], "summary": {}}
    statuses = Counter()
    fetched_pages = 0

    for channel in channels:
        fetched_at = clock()
        http_status, page_attempted = None, False
        entries, parsed_dropped, kept = [], {}, None
        status, reason = "not_made", ""
        state = _guard(channel, fetched_at, target_day, stopped)
        if state:
            status, reason = state
        elif transport is None:
            reason = "transport not configured"
        elif (fetched_at - started).total_seconds() > telegram.PHASE_BUDGET_SECONDS:
            reason = f"the phase ran past its {telegram.PHASE_BUDGET_SECONDS} second budget"
        elif len(out["posts"]) >= telegram.MAX_POSTS_PER_RUN:
            reason = f"the run already holds {telegram.MAX_POSTS_PER_RUN} Telegram posts"
        elif not telegram.valid_handle(channel.handle):
            status, reason = "error", "invalid channel handle"
        else:
            def checked(url, *, timeout, max_bytes):
                nonlocal http_status, page_attempted
                now_state = _guard(channel, clock(), target_day, stopped)
                if now_state:
                    raise _RunGuard(channel.handle, *now_state)
                is_page = url == channel.url
                if is_page:
                    page_attempted = True
                answer, headers, body = transport(url, timeout=timeout, max_bytes=max_bytes)
                if is_page:
                    http_status = answer
                return answer, headers, body

            try:
                if not robots.allowed(channel, checked):
                    status, reason = "robots_disallowed", robots.reason(channel)
                else:
                    if fetched_pages:
                        sleep(telegram.POLITE_DELAY_SECONDS)
                    fetched_pages += 1
                    answer, headers, body = checked(channel.url, timeout=reader.TIMEOUT_SECONDS,
                                                    max_bytes=reader.MAX_RESPONSE_BYTES)
                    if 300 <= answer < 400:
                        status, reason = "no_preview", f"HTTP {answer}: the channel's web preview is off or gone"
                    elif answer != 200:
                        status, reason = f"http_{answer}", f"HTTP status {answer}"
                    elif not reader._is_html(headers) or not isinstance(body, bytes) or not body.strip():
                        status, reason = "error", "response is not an HTML page"
                    elif len(body) > reader.MAX_RESPONSE_BYTES:
                        status, reason = "error", "response exceeds the 2 MiB body limit"
                    else:
                        html = body.decode("utf-8", errors="replace")
                        entries, parsed_dropped = telegram.parse_preview(channel, html)
                        if not entries and "tgme_widget_message" not in html:
                            status, reason = "no_entries", "the page holds no channel messages the reader reads"
                        else:
                            kept = rows(channel, entries, observed_at=fetched_at, run_id=run_id,
                                        target_day=target_day)
                            after = _guard(channel, clock(), target_day, stopped)
                            if after:
                                status, reason, kept = after[0], after[1], None
                            else:
                                status = "ok"
            except _RunGuard as error:
                status, reason = error.status, error.reason
            except Exception as error:
                status, reason = "error", type(error).__name__

        posts, observations, creator, safe, row_dropped = kept or ([], [], None, [], {})
        room = max(0, telegram.MAX_POSTS_PER_RUN - len(out["posts"]))
        if len(posts) > room:
            row_dropped = {**row_dropped, "over_run_cap": len(posts) - room}
            posts, observations, safe = posts[:room], observations[:room], safe[:room]
        out["posts"] += posts
        out["observations"] += observations
        if creator is not None and posts:
            out["creators"].append(creator)
        dropped = dict(Counter(parsed_dropped) + Counter(row_dropped))
        record_now = clock()
        out["records"].append({
            "row": ROW, "route": ROUTE, "market": channel.market, "day": _local_day(record_now, channel.market),
            "platform": PLATFORM, "series": SERIES, "protocol": protocol(channel), "lane_class": LANE_CLASS,
            "status": status, "ok": status == "ok", "calls": int(page_attempted), "units_planned": 1,
            "units_ok": int(status == "ok"), "items": len(observations),
            "post_ids": [p["post_id"] for p in posts], "reason": reason})
        out["raw_rows"].append(_raw_row(channel, run_id, fetched_at, http_status, status=status, safe_entries=safe,
                                        decoded=len(entries), accepted=len(observations), dropped=dropped,
                                        reason=reason))
        statuses[status] += 1

    out["summary"] = {
        "channels_planned": len(channels),
        "channels_attempted": sum(r["calls"] for r in out["records"]),
        "channels_ok": sum(r["ok"] for r in out["records"]),
        "posts": len(out["posts"]),
        "posts_by_market": dict(Counter(o["market"] for o in out["observations"])),
        "statuses": dict(statuses),
        "credits": 0,
    }
    return out
