import re
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

import yaml

from core.collect.socialcrawl_client import Refused, quote_for


FIELDS = ("platform", "handle", "url", "market")
METADATA_FIELDS = ("source", "active", "source_tags")
AUDITED_ACCOUNT_SOURCE = "ogilvy_audit_2026-09-30"
SOURCES = ("research_confirmed", "ogilvy_uefa_list", AUDITED_ACCOUNT_SOURCE)
LEGACY_SOURCE = "ogilvy_uefa_list"
MARKETS = ("ZA", "NG", "KE")
# Curated profiles read a day per market, cut further by limit_from_residual when the collect share is tight.
# Panel posts are what lift a trend into the candidates (3 creators in 3 days). 160 covers every active record of
# each market's list, so when the share allows, the whole list is read each day as one stable panel whose
# protocol stays the same from day to day and builds a baseline.
DAILY_LIMIT = 160
MANIFEST = Path(__file__).resolve().parents[1] / "config" / "uefa_creators.yaml"
HOSTS = {
    "instagram.com": "instagram",
    "www.instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "www.tiktok.com": "tiktok",
    "m.tiktok.com": "tiktok",
    "facebook.com": "facebook",
    "www.facebook.com": "facebook",
    "m.facebook.com": "facebook",
}
CANONICAL_HOST = {"instagram": "www.instagram.com", "tiktok": "www.tiktok.com",
                  "facebook": "www.facebook.com"}
TRACKING_QUERY = {"instagram": {"hl", "igsh"}, "tiktok": {"_r", "_t"}, "facebook": set()}
UNPRICED_HOSTS = {"x.com": "twitter", "www.x.com": "twitter", "twitter.com": "twitter",
                  "www.twitter.com": "twitter", "www.youtube.com": "youtube"}
UNPRICED_CANONICAL_HOST = {"twitter": "x.com", "youtube": "www.youtube.com"}
UNPRICED_RESERVED_SEGMENTS = {
    "twitter": {"home", "explore", "search", "notifications", "messages", "i", "intent", "settings",
                "share", "hashtag", "status", "logout"},
}
LOGIN_SEGMENTS = {"login", "log-in", "signin", "sign-in", "accounts", "challenge", "recover"}
PROFILE_SEGMENTS = {
    "instagram": {"p", "reel", "reels", "tv", "stories", "direct", "explore", "accounts"},
    "tiktok": {"login", "signup", "explore", "tag", "video", "photo", "discover"},
    "facebook": {"login", "recover", "help", "watch", "groups", "events", "marketplace", "gaming"},
}


def _market(sheet_name):
    title = str(sheet_name).strip().casefold()
    if title.startswith("sa - client focused - uefa"):
        return "ZA"
    if title.startswith("nigeria - client focused - uefa"):
        return "NG"
    if title.startswith("kenya - client focused - uefa"):
        return "KE"
    return None


def _platforms(value):
    text = str(value or "").casefold()
    text = re.sub(r"\btik[\s-]+tok\b", "tiktok", text)
    text = re.sub(r"\byou[\s-]*tube\b", "youtube", text)
    aliases = {"tt": "tiktok", "ig": "instagram", "insta": "instagram", "fb": "facebook",
               "fbook": "facebook", "yt": "youtube", "sc": "snapchat", "x": "twitter"}
    return {aliases.get(token, token) for token in re.findall(r"[a-z0-9]+", text)}


def _urls(cell):
    if cell is None:
        return []
    hyperlink = getattr(cell, "hyperlink", None)
    target = getattr(hyperlink, "target", None) if hyperlink else None
    if target:
        return [str(target)]
    value = cell.value
    if not isinstance(value, str):
        return []
    if getattr(cell, "data_type", None) == "f":
        found = re.findall(r'(?i)HYPERLINK\s*\(\s*"([^"]+)"', value)
    else:
        found = re.findall(r"https?://[^\s\"'<>]+", value, re.I)
    return [item.rstrip(".,;)") for item in found]


def _profile(url, declared, *, allow_unpriced=False):
    try:
        parsed = urlsplit(str(url).strip())
        host = (parsed.hostname or "").casefold()
        unpriced_platform = UNPRICED_HOSTS.get(host)
        if unpriced_platform and allow_unpriced:
            platform = unpriced_platform
        elif unpriced_platform:
            return None, "unsupported_priced_route"
        else:
            platform = HOSTS.get(host)
        if not platform:
            return None, "unsupported_priced_route"
        if parsed.scheme.casefold() not in {"http", "https"} or parsed.username or parsed.password:
            return None, "invalid_url"
        if parsed.port not in (None, 80, 443) or parsed.fragment:
            return None, "invalid_url"
        segments = [unquote(part) for part in parsed.path.split("/") if part]
        lowered = [part.casefold() for part in segments]
        if any(part in LOGIN_SEGMENTS for part in lowered):
            return None, "login_url"
        if (lowered and (lowered[0] in PROFILE_SEGMENTS.get(platform, set())
                         or lowered[0] in UNPRICED_RESERVED_SEGMENTS.get(platform, set()))):
            return None, "invalid_profile_url"
        declared = _platforms(declared) if declared is not None and str(declared).strip() else set()
        if declared and platform not in declared:
            return None, "platform_mismatch"

        if platform in UNPRICED_CANONICAL_HOST:
            if parsed.query:
                return None, "invalid_profile_url"
        else:
            query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
            if query_keys - TRACKING_QUERY[platform]:
                return None, "invalid_profile_url"

        if platform == "tiktok":
            if len(segments) == 1 and segments[0].startswith("@"):
                handle = segments[0][1:]
            elif (len(segments) == 3 and segments[0].startswith("@") and lowered[1] == "video"
                  and segments[2].isdigit()):
                handle = segments[0][1:]
            else:
                return None, "invalid_profile_url"
        elif platform == "instagram":
            if len(segments) == 1:
                handle = segments[0].removeprefix("@")
            elif len(segments) == 2 and lowered[1] == "reels":
                handle = segments[0].removeprefix("@")
            else:
                return None, "invalid_profile_url"
        elif platform == "facebook" and len(segments) == 1:
            handle = segments[0]
        elif platform == "twitter" and len(segments) == 1:
            handle = segments[0]
        elif platform == "youtube" and len(segments) == 1 and segments[0].startswith("@"):
            handle = segments[0][1:]
        else:
            return None, "invalid_profile_url"

        if platform == "twitter":
            valid_handle = bool(re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle))
        elif platform == "youtube":
            valid_handle = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,28}[A-Za-z0-9]", handle))
        else:
            valid_handle = bool(re.fullmatch(r"[A-Za-z0-9._-]{1,80}", handle))
        if not handle or not valid_handle:
            return None, "invalid_profile_url"
        if platform in UNPRICED_CANONICAL_HOST:
            prefix = "@" if platform == "youtube" else ""
            canonical_path = f"/{prefix}{quote(handle, safe='._-')}"
            canonical = urlunsplit(("https", UNPRICED_CANONICAL_HOST[platform], canonical_path, "", ""))
            return {"platform": platform, "handle": handle, "url": canonical}, None
        try:
            quote_for("prism/profiles", "POST", {
                "items": [{"platform": platform, "handle": handle}], "include": "posts"})
        except Refused:
            return None, "unsupported_priced_route"

        path = f"/@{quote(handle, safe='._-')}" if platform == "tiktok" else f"/{quote(handle, safe='._-')}/"
        canonical = urlunsplit(("https", CANONICAL_HOST[platform], path, "", ""))
        return {"platform": platform, "handle": handle, "url": canonical}, None
    except ValueError:
        return None, "invalid_url"


def _header(rows):
    platform_col = link_col = None
    platform_row = link_row = None
    for row_index, row in enumerate(rows[:4]):
        for column, cell in enumerate(row):
            value = str(cell.value or "").strip().casefold() if cell.value is not None else ""
            if "platform" in value:
                platform_col, platform_row = column, row_index
            if "link" in value:
                link_col, link_row = column, row_index
        if platform_col is not None and link_col is not None:
            break
    if platform_col is None or link_col is None:
        return None
    return platform_col, link_col, max(platform_row, link_row) + 1


def parse_workbook(workbook):
    records, skipped, seen, logged = [], [], set(), set()
    source_rows, url_rows = [], {}
    for sheet in workbook.worksheets:
        market = _market(sheet.title)
        if market is None:
            continue
        rows = list(sheet.iter_rows(values_only=False))
        header = _header(rows)
        if header is None:
            continue
        platform_col, link_col, first_data_row = header
        for row_number, row in enumerate(rows[first_data_row:], first_data_row + 1):
            platform_cell = row[platform_col] if platform_col < len(row) else None
            link_cell = row[link_col] if link_col < len(row) else None
            declared = platform_cell.value if platform_cell is not None else None
            urls = _urls(link_cell)
            if declared is None and not urls:
                continue
            row_index = len(source_rows)
            url_keys = set()
            for url in urls:
                url_key = ("raw", url.strip())
                url_keys.add(url_key)
                url_rows.setdefault(url_key, set()).add(row_index)
            source_rows.append({"market": market, "row": row_number, "declared": declared,
                                "urls": urls, "url_keys": url_keys})

    duplicate_urls = {url_key for url_key, row_indexes in url_rows.items() if len(row_indexes) > 1}
    for source_row in source_rows:
        market, row_number = source_row["market"], source_row["row"]
        urls, declared = source_row["urls"], source_row["declared"]
        if not urls:
            entry = {"market": market, "row": row_number, "reason": "missing_url"}
            key = market, row_number, entry["reason"]
            if key not in logged:
                skipped.append(entry)
                logged.add(key)
            continue
        if source_row["url_keys"] & duplicate_urls:
            entry = {"market": market, "row": row_number, "reason": "duplicate_url"}
            key = market, row_number, entry["reason"]
            if key not in logged:
                skipped.append(entry)
                logged.add(key)
            continue
        for url in urls:
            profile, reason = _profile(url, declared)
            if reason:
                entry = {"market": market, "row": row_number, "reason": reason}
                key = market, row_number, reason
                if key not in logged:
                    skipped.append(entry)
                    logged.add(key)
                continue
            record = {**profile, "market": market}
            key = market, record["platform"], record["handle"].casefold()
            if key in seen:
                entry = {"market": market, "row": row_number, "reason": "duplicate"}
                skip_key = market, row_number, entry["reason"]
                if skip_key not in logged:
                    skipped.append(entry)
                    logged.add(skip_key)
                continue
            seen.add(key)
            records.append(record)
    return records, skipped


def import_workbook(path):
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        return parse_workbook(workbook)
    finally:
        workbook.close()


def load_manifest(path=MANIFEST):
    path = Path(path)
    records = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("creator manifest must be a list")
    legacy_manifest = path.resolve() == MANIFEST.resolve()
    seen = set()
    normalized = []
    for record in records:
        if (not isinstance(record, dict) or not set(FIELDS).issubset(record)
                or not set(record).issubset(FIELDS + METADATA_FIELDS)):
            raise ValueError("creator manifest records must contain platform, handle, url, market and approved metadata only")
        if any(not isinstance(record[field], str) for field in FIELDS):
            raise ValueError("creator manifest fields must be strings")
        if "source" not in record:
            if not legacy_manifest:
                raise ValueError("creator manifest records must include a source")
            source = LEGACY_SOURCE
        else:
            source = record["source"]
            if source not in SOURCES:
                raise ValueError("creator manifest has an unknown source")
        active = record.get("active", True)
        if not isinstance(active, bool):
            raise ValueError("creator manifest active field must be a boolean")
        source_tags = record.get("source_tags")
        if source_tags is not None:
            if (not isinstance(source_tags, (list, tuple)) or not source_tags
                    or any(not isinstance(tag, str) or tag not in SOURCES for tag in source_tags)
                    or len(set(source_tags)) != len(source_tags) or source not in source_tags):
                raise ValueError("creator manifest source tags must be unique approved values including its source")
        market = record["market"]
        if market not in MARKETS:
            raise ValueError("creator manifest has an unknown market")
        profile, reason = _profile(record["url"], record["platform"], allow_unpriced=not active)
        if reason or profile != {key: record[key] for key in ("platform", "handle", "url")}:
            raise ValueError("creator manifest has an invalid profile")
        key = market, record["platform"], record["handle"].casefold()
        if key in seen:
            raise ValueError("creator manifest repeats a profile within a market")
        seen.add(key)
        normalized_record = {**{field: record[field] for field in FIELDS}, "source": source, "active": active}
        if source_tags is not None:
            normalized_record["source_tags"] = list(source_tags)
        normalized.append(normalized_record)
    return normalized


def _readable(records, market):
    return [record for record in records if record["market"] == market
            and record.get("source") in SOURCES and record.get("active") is True
            and record.get("platform") in CANONICAL_HOST]


def listed_handles(records, market):
    """(platform, folded handle) of every account the rotation may read as market's feeds."""
    if market not in MARKETS:
        raise ValueError("unknown market")
    return {(record["platform"], str(record["handle"]).casefold()) for record in _readable(records, market)}


def daily_rotation(records, market, day, limit=DAILY_LIMIT):
    if market not in MARKETS:
        raise ValueError("unknown market")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    candidates = _readable(records, market)
    count = min(limit, len(candidates))
    if not count:
        return []
    start = (day.toordinal() * count) % len(candidates)
    return [candidates[(start + offset) % len(candidates)] for offset in range(count)]


def limit_from_residual(records, day, residual, limit=DAILY_LIMIT):
    queues = {market: daily_rotation(records, market, day, limit) for market in MARKETS}
    # A market with a shorter list stops adding profiles at its end; the others go on, so each market's
    # whole list fits when the share allows (daily_rotation caps every market at its own list).
    count = max((len(queue) for queue in queues.values()), default=0)
    allowed = 0
    spent = 0
    for index in range(count):
        hold = sum(quote_for("prism/profiles", "POST", {
            "items": [{"platform": queue[index]["platform"], "handle": queue[index]["handle"]}],
            "include": "posts"}) for queue in queues.values() if index < len(queue))
        if spent + hold > residual:
            break
        spent += hold
        allowed += 1
    return allowed
