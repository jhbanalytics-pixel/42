"""X source discovery is a pointer lane. Only native tweet lookups yield evidence."""

import re
from datetime import date, datetime, timezone
from urllib.parse import urlsplit


HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
STATUS = re.compile(r"/([A-Za-z0-9_]{1,15})/status/([0-9]+)(?:/)?")
HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}


def _window(params):
    values = [params.get(key) for key in ("from_date", "to_date")]
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) for value in values):
        raise ValueError("X discovery needs exact ISO date bounds")
    start, end = (date.fromisoformat(value) for value in values)
    if start > end:
        raise ValueError("X discovery date bounds must not be reversed")
    return start, end


def status_url(value):
    if not isinstance(value, str):
        return None
    try:
        link = urlsplit(value)
        match = STATUS.fullmatch(link.path)
        if link.scheme != "https" or link.netloc.lower() not in HOSTS or not match:
            return None
        handle, native = match.groups()
        return f"https://x.com/{handle}/status/{native}"
    except ValueError:
        return None


def discovery_quote(params):
    from core.collect.socialcrawl_client import Refused

    query = params.get("query")
    handles = str(params.get("from_handles") or "").split(",")
    if not isinstance(query, str) or not query.strip():
        raise Refused("X discovery needs a query")
    if not 1 <= len(handles) <= 10 or any(not HANDLE.fullmatch(h.strip()) for h in handles):
        raise Refused("X discovery needs one to ten explicit handles")
    if params.get("exclude_handles"):
        raise Refused("from_handles and exclude_handles are mutually exclusive")
    try:
        _window(params)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    return 5


def project_discovery(body):
    body = body if isinstance(body, dict) else {}
    data = body.get("data")
    data = data if isinstance(data, dict) else {}
    sources, seen = [], set()
    for source in data.get("sources", []) if isinstance(data.get("sources"), list) else []:
        url = status_url(source.get("url")) if isinstance(source, dict) else None
        native = url.rsplit("/", 1)[1] if url else None
        if native and native not in seen:
            seen.add(native)
            sources.append({"url": url})
    projected = {key: body[key] for key in ("success", "request_id", "credits_used", "cached") if key in body}
    projected["data"] = {"sources": sources}
    count = data.get("tool_calls_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        projected["data"]["tool_calls_count"] = count
    return projected


def verified_lookup(body, url, handles, *, window=None):
    canonical = status_url(url)
    if not canonical or not isinstance(body, dict) or body.get("success") is False:
        return None
    data = body.get("data")
    post = data.get("post") if isinstance(data, dict) else None
    if not isinstance(post, dict):
        return None
    author = post.get("author")
    actual = author.get("username") if isinstance(author, dict) else None
    expected, native = canonical.split("/")[-3], canonical.rsplit("/", 1)[1]
    allowed = {str(h).strip().lower() for h in handles}
    native_url = status_url(post.get("url"))
    if (not isinstance(actual, str) or actual.lower() != expected.lower() or actual.lower() not in allowed
            or str(post.get("id")) != native or not native_url or native_url.lower() != canonical.lower()):
        return None
    if window is not None:
        published = post.get("published_at")
        if not isinstance(published, str):
            return None
        try:
            moment = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if moment.tzinfo is None or not window[0] <= moment.astimezone(timezone.utc).date() <= window[1]:
                return None
        except ValueError:
            return None
    return body


def discover_and_lookup(client, params, *, market, max_lookups=3):
    """Prepared for the approved probe or offline replay; the daily job does not activate this lane."""
    if not isinstance(max_lookups, int) or isinstance(max_lookups, bool) or not 1 <= max_lookups <= 3:
        raise ValueError("max_lookups must be one to three")
    try:
        window = _window(params)
    except ValueError:
        return [], [{"route": "twitter/ai-search", "status": "forbidden", "failure": "date_window"}]
    discovery = client.discover_x(params, market=market)
    records = [{"route": discovery.route, "status": discovery.status, "failure": discovery.failure}]
    results = []
    if discovery.status not in ("ok", "cached", "empty") or not discovery.body:
        return results, records
    handles = params["from_handles"].split(",")
    for source in discovery.body["data"]["sources"][:max_lookups]:
        url = source["url"]
        result = client.call("twitter/tweet", {"url": url}, market=market, lane="expansion")
        valid = result.status in ("ok", "cached") and verified_lookup(result.body, url, handles, window=window) is not None
        records.append({"route": result.route, "status": result.status if valid else "lookup_refused",
                        "failure": result.failure, "native_id": url.rsplit("/", 1)[1]})
        if valid:
            results.append(result)
        if result.status in ("cap_reached", "balance_floor", "insufficient_credits"):
            break
    return results, records
