from __future__ import annotations

import hashlib
import time
from collections import Counter
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from core.collect.parse import _local_day
from core.public_feeds import reader
from core.public_feeds.catalog import confirmed_feeds

from .public_feed_rows import feed_protocol, normalise


ROW = "2.12"
ROUTE = "public_feed"
LANE = "public_feed"
PARAMS_HASH = hashlib.sha256(b"public_feed").hexdigest()
SERIES_BY_KIND = {"news": "news_rss", "chart": "board_music_country", "playlist": "radio_playlist"}


class ResponseTooLarge(ValueError):
    """A response body over the reader's limit, named so a failed feed says why."""


class _RunGuard(reader.FeedReadError):
    def __init__(self, feed_id, status, reason):
        self.status = status
        super().__init__(feed_id, reason)


def https_transport(url, *, timeout, max_bytes):
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("request URL must be anonymous HTTPS")

    import requests

    session = requests.Session()
    session.trust_env = False
    session.auth = None
    session.cookies.clear()
    response = None

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("request exceeded its total deadline")
        return seconds

    try:
        deadline = time.monotonic() + timeout
        connect_timeout = min(timeout, remaining())
        response = session.get(
            url,
            headers={"User-Agent": reader.USER_AGENT},
            timeout=(connect_timeout, remaining()),
            allow_redirects=False,
            stream=True,
        )
        remaining()
        status = response.status_code
        headers = dict(response.headers)
        if status != 200:
            return status, headers, b""
        declared_length = next((value for key, value in headers.items()
                                if str(key).lower() == "content-length"), "")
        if declared_length:
            try:
                declared_bytes = int(declared_length)
            except (TypeError, ValueError):
                declared_bytes = 0
            if declared_bytes > max_bytes:
                raise ResponseTooLarge("response exceeds the body limit")
        body = bytearray()
        chunks = iter(response.iter_content(chunk_size=64 * 1024))
        while True:
            read_timeout = remaining()
            _set_read_timeout(response, read_timeout)
            try:
                chunk = next(chunks)
            except StopIteration:
                break
            remaining()
            if not chunk:
                continue
            if len(body) + len(chunk) > max_bytes:
                raise ResponseTooLarge("response exceeds the body limit")
            body.extend(chunk)
        return status, headers, bytes(body)
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            session.close()


def _set_read_timeout(response, timeout):
    raw = getattr(response, "raw", None)
    candidates = (
        getattr(getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None), "_sock", None),
        getattr(getattr(raw, "_connection", None), "sock", None),
    )
    sock = next((candidate for candidate in candidates if candidate is not None), None)
    if sock is not None and callable(getattr(sock, "settimeout", None)):
        sock.settimeout(timeout)


class _Robots:
    RULE_DENY = "robots.txt did not allow the feed URL"
    MAX_REDIRECTS = 5

    def __init__(self):
        self.rules = {}
        self.reasons = {}

    @staticmethod
    def _same_site_target(url, headers):
        """The HTTPS URL a robots.txt redirect points at on the same site (www or bare host), else None."""
        location = next((str(value) for key, value in (headers or {}).items()
                         if str(key).lower() == "location"), "").strip()
        if not location:
            return None
        here, there = urlsplit(url), urlsplit(urljoin(url, location))
        if there.scheme.lower() not in ("http", "https") or there.username or there.password or not there.hostname:
            return None
        try:
            port = there.port
        except ValueError:
            return None
        if port not in (None, 80, 443):
            return None
        site = lambda host: host.lower().removeprefix("www.")
        if site(there.hostname) != site(here.hostname):
            return None
        return urlunsplit(("https", there.hostname.lower(), there.path or "/", there.query, ""))

    @staticmethod
    def _origin(feed):
        parts = urlsplit(feed.url)
        return f"{parts.scheme}://{parts.netloc}"

    def allowed(self, feed, get):
        origin = self._origin(feed)
        if origin not in self.rules:
            self.rules[origin], self.reasons[origin] = self._read(feed, origin + "/robots.txt", get)
        rules = self.rules[origin]
        return rules if isinstance(rules, bool) else rules.can_fetch(reader.USER_AGENT, feed.url)

    def reason(self, feed):
        """Why the feed was not fetched: the robots.txt answer that could not be read, else a rule deny."""
        return self.reasons.get(self._origin(feed)) or self.RULE_DENY

    @classmethod
    def _read(cls, feed, url, get):
        seen = {url}
        for _ in range(cls.MAX_REDIRECTS + 1):
            try:
                status, headers, body = get(url, timeout=reader.TIMEOUT_SECONDS, max_bytes=reader.MAX_RESPONSE_BYTES)
            except _RunGuard:
                raise
            except Exception as error:
                return False, f"robots.txt could not be read ({type(error).__name__}), so the feed was not fetched"
            if not 300 <= status < 400:
                break
            target = cls._same_site_target(url, headers)
            if target is None:
                return False, (f"robots.txt answered HTTP {status} (redirect not followed), "
                               "so the feed was not fetched")
            if target in seen:
                # A loop never resolves (SABC, 4 Oct 2026: https answers 301 to http and http back to https).
                return True, None
            seen.add(target)
            url = target
        else:
            # RFC 9309 2.3.1.2: more than five redirects means robots.txt is unavailable, treated as a 404.
            return True, None
        if status in (401, 403, 429) or status >= 500:
            return False, f"robots.txt answered HTTP {status}, so the feed was not fetched"
        if status != 200:
            return True, None
        if not isinstance(body, bytes) or len(body) > reader.MAX_RESPONSE_BYTES:
            return False, "robots.txt body was unusable, so the feed was not fetched"
        rules = RobotFileParser()
        rules.parse(body.decode("utf-8", errors="replace").splitlines())
        return rules, None


def _target_day(day):
    if isinstance(day, datetime):
        return day.date().isoformat()
    return day.isoformat() if isinstance(day, date) else str(day)


def _stop_reason(stopped):
    value = stopped() if callable(stopped) else stopped
    if not value:
        return None
    return value if isinstance(value, str) else "collection stopped"


def _guard(feed, now, target_day, stopped):
    reason = _stop_reason(stopped)
    if reason:
        return "stopped", reason
    actual_day = _local_day(now, feed.market)
    if actual_day != target_day:
        return "day_changed", f"{feed.market} local day changed to {actual_day}"
    return None


def _protocol(feed):
    return feed_protocol(feed)


def _next_pull(feed, last_pulls):
    series = SERIES_BY_KIND[feed.kind]
    return last_pulls.get((feed.market, series, _protocol(feed)), 0) + 1


def _platform(feed, normalized):
    for collection in ((normalized or {}).get("observations", []), (normalized or {}).get("counters", [])):
        if collection:
            return collection[0]["platform"]
    if feed.kind == "news":
        return "news"
    if feed.kind == "playlist":
        return "radio"
    return {"ke_mdundo_top_songs": "mdundo", "ng_turntable_top_100": "turntable"}.get(feed.feed_id)


def _raw_row(feed, run_id, fetched_at, http_status, *, status, safe_entries=(), decoded=0, accepted=0,
             held=0, dropped=None, reason=""):
    body = {
        "safe_entries": list(safe_entries),
        "decoded_count": decoded,
        "accepted_count": accepted,
        "held_count": held,
        "dropped": dict(dropped or {}),
        "status": status,
    }
    if reason:
        body["reason"] = reason
    return {
        "run_id": run_id,
        "job": "collect",
        "market": feed.market,
        "route": ROUTE,
        "params_hash": PARAMS_HASH,
        "lane": LANE,
        "seed_key": feed.feed_id,
        "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
        "http_status": http_status,
        "credits_quoted": 0,
        "credits_charged": 0,
        "cache_hit": False,
        "body": body,
    }


def run(day, run_id, *, transport, clock, item_id_fn, last_pulls=None, stopped=None) -> dict:
    feeds = confirmed_feeds()
    target_day = _target_day(day)
    pulls = dict(last_pulls or {})
    robots = _Robots()
    out = {
        "posts": [], "observations": [], "counters": [], "items": {}, "records": [], "raw_rows": [],
        "summary": {},
    }
    status_counts = Counter()

    for feed in feeds:
        fetched_at = clock()
        state = _guard(feed, fetched_at, target_day, stopped)
        page_attempted = False
        http_status = None
        entries = []
        normalized = None
        keep_rows = False
        reason = ""
        status = "not_made"

        if state:
            status, reason = state
        elif transport is None:
            reason = "transport not configured"
        else:
            def checked_transport(url, *, timeout, max_bytes):
                nonlocal page_attempted, http_status
                state_now = _guard(feed, clock(), target_day, stopped)
                if state_now:
                    raise _RunGuard(feed.feed_id, state_now[0], state_now[1])
                is_page = url == feed.url
                if is_page:
                    page_attempted = True
                response_status, headers, body = transport(url, timeout=timeout, max_bytes=max_bytes)
                if is_page:
                    http_status = response_status
                if isinstance(body, bytes) and len(body) > max_bytes:
                    raise reader.FeedReadError(feed.feed_id, "response exceeds the 2 MiB body limit")
                return response_status, headers, body

            try:
                if not robots.allowed(feed, checked_transport):
                    state_after_robots = _guard(feed, clock(), target_day, stopped)
                    if state_after_robots:
                        status, reason = state_after_robots
                    else:
                        status, reason = "robots_disallowed", robots.reason(feed)
                else:
                    entries = reader.read_feed(feed, fetched_at=fetched_at, transport=checked_transport)
                    normalized = normalise(
                        feed,
                        entries,
                        observed_at=fetched_at,
                        run_id=run_id,
                        pull_seq=_next_pull(feed, pulls),
                        item_id_fn=item_id_fn,
                    )
                    state_after_processing = _guard(feed, clock(), target_day, stopped)
                    if state_after_processing:
                        status, reason = state_after_processing
                    else:
                        status = "ok"
                        keep_rows = True
            except _RunGuard as error:
                status, reason = error.status, error.reason
            except reader.FeedReadError as error:
                state_after_error = _guard(feed, clock(), target_day, stopped)
                if state_after_error:
                    status, reason = state_after_error
                elif http_status is not None and http_status != 200:
                    status, reason = f"http_{http_status}", error.reason
                else:
                    status, reason = "error", error.reason
            except Exception as error:
                state_after_error = _guard(feed, clock(), target_day, stopped)
                if state_after_error:
                    status, reason = state_after_error
                else:
                    status, reason = "error", type(error).__name__

        if normalized is None:
            safe_entries = []
            dropped = {}
            accepted = 0
            held = max(0, len(entries) - accepted)
        else:
            safe_entries = normalized["safe_entries"]
            dropped = normalized["dropped"]
            accepted = len(normalized["observations"]) + len(normalized["counters"]) if keep_rows else 0
            held = max(sum(dropped.values()), len(entries) - accepted)
        record_now = clock()
        if normalized is not None and keep_rows:
            final_state = _guard(feed, record_now, target_day, stopped)
            if final_state:
                status, reason = final_state
                keep_rows = False
                accepted = 0
                held = max(sum(normalized["dropped"].values()), len(entries))
        if normalized is not None and keep_rows:
            out["posts"].extend(normalized["posts"])
            out["observations"].extend(normalized["observations"])
            out["counters"].extend(normalized["counters"])
            out["items"].update(normalized["items"])

        post_ids = [post["post_id"] for post in (normalized or {}).get("posts", [])] if keep_rows else []
        call_count = int(page_attempted)
        record = {
            "row": ROW,
            "route": ROUTE,
            "market": feed.market,
            "day": _local_day(record_now, feed.market),
            "platform": _platform(feed, normalized),
            "series": SERIES_BY_KIND[feed.kind],
            "protocol": _protocol(feed),
            "lane_class": "unbiased_rank" if feed.kind == "chart" else "context",
            "status": status,
            "ok": status == "ok",
            "calls": call_count,
            "units_planned": 1,
            "units_ok": int(status == "ok"),
            "items": accepted,
            "post_ids": post_ids,
            "reason": reason,
        }
        out["records"].append(record)
        out["raw_rows"].append(_raw_row(
            feed,
            run_id,
            fetched_at,
            http_status,
            status=status,
            safe_entries=safe_entries,
            decoded=len(entries),
            accepted=accepted,
            held=held,
            dropped=dropped,
            reason=reason,
        ))
        status_counts[status] += 1

    out["summary"] = {
        "feeds_planned": len(feeds),
        "feeds_attempted": sum(record["calls"] for record in out["records"]),
        "feeds_ok": sum(record["ok"] for record in out["records"]),
        "feeds_failed": sum(not record["ok"] for record in out["records"]),
        "decoded": sum(row["body"]["decoded_count"] for row in out["raw_rows"]),
        "accepted": sum(row["body"]["accepted_count"] for row in out["raw_rows"]),
        "held": sum(row["body"]["held_count"] for row in out["raw_rows"]),
        "statuses": dict(status_counts),
    }
    return out
