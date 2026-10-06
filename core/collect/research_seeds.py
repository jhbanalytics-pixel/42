from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date
import unicodedata
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

from core.collect.curated_creators import _profile


MARKETS = ("ZA", "NG", "KE")
PROFILE_FIELDS = ("platform", "handle", "url", "market")
RECORD_FIELDS = (*PROFILE_FIELDS, "source", "active")
PROFILE_TYPES = ("creator", "outlet")
SOURCE_RESEARCH = "research_confirmed"
SOURCE_UEFA = "ogilvy_uefa_list"
AUDITED_ACCOUNT_TAG = "ogilvy_audit_2026-09-30"
SOURCE_TAGS = {SOURCE_RESEARCH, SOURCE_UEFA, AUDITED_ACCOUNT_TAG}
THABANG_CONFIRMED_TAG = "ogilvy_thabang_confirmed"
THAPELO_CONFIRMED_TAG = "ogilvy_thapelo_confirmed"
EXPANDED_UNVERIFIED_TAG = "ogilvy_expanded_unverified"
AUDITED_ACCOUNT_FIELDS = ("handle", "platform", "url", "market", "source_tag")
DATED_TOPIC_FIELDS = ("start_date", "end_date", "market", "topic", "event", "venue", "source_tag")
SOURCE_MARKETS = {
    "south africa": "ZA", "rsa": "ZA", "za": "ZA",
    "nigeria": "NG", "ngr": "NG", "ng": "NG",
    "kenya": "KE", "kny": "KE", "ke": "KE",
}
MONTHS = (
    "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    "jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)


def _split_table_row(line):
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    cells, buffer = [], []
    escaped = in_code = False
    brackets = parentheses = 0
    for char in value:
        if escaped:
            buffer.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "`":
            in_code = not in_code
            buffer.append(char)
            continue
        if not in_code:
            if char == "[":
                brackets += 1
            elif char == "]" and brackets:
                brackets -= 1
            elif char == "(" and not brackets:
                parentheses += 1
            elif char == ")" and parentheses:
                parentheses -= 1
            if char == "|" and not brackets and not parentheses:
                cells.append("".join(buffer).strip())
                buffer = []
                continue
        buffer.append(char)
    if escaped:
        buffer.append("\\")
    cells.append("".join(buffer).strip())
    return cells


def _is_separator(cells):
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def _heading(line):
    match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
    return (len(match.group(1)), match.group(2).strip()) if match else None


def _market_from_heading(title):
    text = title.casefold()
    found = set()
    if re.search(r"\bza\b|south africa|south african", text):
        found.add("ZA")
    if re.search(r"\bng\b|nigeria|nigerian", text):
        found.add("NG")
    if re.search(r"\bke\b|kenya|kenyan", text):
        found.add("KE")
    return next(iter(found)) if len(found) == 1 else None


def _kind_from_heading(title):
    text = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    if "moment" in text or "calendar" in text:
        return "moment"
    if any(word in text for word in ("outlet", "publisher", "publication")):
        return "outlet"
    if all(word in text for word in ("news", "culture", "entertainment", "account")):
        return "outlet"
    if any(word in text for word in ("creator", "influencer", "profile")):
        return "creator"
    return None


def _header_roles(headers):
    roles = {}
    normalized = []
    for index, header in enumerate(headers):
        text = re.sub(r"[^a-z0-9]+", " ", header.casefold()).strip()
        normalized.append(text)
        if "platform" in text:
            roles.setdefault("platform", index)
        elif re.search(r"\b(handle|username|user name|account handle)\b", text):
            roles.setdefault("handle", index)
        elif re.search(r"\b(market|country|region)\b", text):
            roles.setdefault("market", index)
        elif re.search(r"\b(date|when|timing|period)\b", text):
            roles.setdefault("date", index)
        elif re.search(r"\b(moment|event)\b", text):
            roles.setdefault("moment", index)
        if re.search(r"\b(profile|social)\s+(url|link)\b|\burl\b|\blink\b", text):
            roles.setdefault("url", index)
    return roles, normalized


def _kind_from_headers(normalized_headers, heading_kind):
    header_text = " ".join(normalized_headers)
    if re.search(r"\bsocial\s+(url|link)\b", header_text):
        return "outlet"
    if re.search(r"\bprofile\s+(url|link)\b", header_text):
        return "creator"
    if re.search(r"\bdate\b", header_text) and re.search(r"\b(moment|event)\b", header_text):
        return "moment"
    return heading_kind


def _market_from_cell(value):
    text = value.casefold().strip()
    return _market_from_heading(text)


def _extract_urls(value):
    found = re.findall(r"https?://[^\s<>\]\[()|]+", value, re.IGNORECASE)
    urls = []
    for item in found:
        cleaned = item.rstrip(".,;:!?")
        if cleaned and cleaned not in urls:
            urls.append(cleaned)
    return urls


def _source_csv_rows(text, fields):
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    try:
        headers = next(reader)
    except StopIteration as error:
        raise ValueError("source CSV is empty") from error
    indexes = {header.strip(): index for index, header in enumerate(headers)}
    missing = set(fields) - set(indexes)
    if missing:
        raise ValueError("source CSV is missing required columns")
    for source_row, row in enumerate(reader, 2):
        if not row or not any(value.strip() for value in row):
            continue
        yield source_row, {
            field: row[indexes[field]].strip() if indexes[field] < len(row) else ""
            for field in fields
        }


def _source_profile(url):
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").casefold()
        if host in {"instagram.com", "www.instagram.com"}:
            platform = "instagram"
        elif host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            platform = "twitter"
        else:
            return None, "unsupported_platform"
        if (parsed.scheme.casefold() not in {"http", "https"} or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443) or parsed.fragment):
            return None, "invalid_url"
        segments = [unquote(part) for part in parsed.path.split("/") if part]
        if platform == "instagram":
            if len(segments) != 1 or segments[0].casefold() in {
                    "p", "reel", "reels", "tv", "stories", "direct", "explore", "accounts"}:
                return None, "invalid_profile_url"
            query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
            if query_keys - {"hl", "igsh"}:
                return None, "invalid_profile_url"
            handle = segments[0].removeprefix("@")
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", handle):
                return None, "invalid_profile_url"
            canonical = urlunsplit(("https", "www.instagram.com", f"/{quote(handle, safe='._-')}/", "", ""))
        else:
            if len(segments) != 1 or parsed.query or segments[0].casefold() in {
                    "home", "explore", "search", "notifications", "messages", "i", "intent", "settings",
                    "share", "hashtag", "status", "logout"}:
                return None, "invalid_profile_url"
            handle = segments[0]
            if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
                return None, "invalid_profile_url"
            canonical = urlunsplit(("https", "x.com", f"/{quote(handle, safe='._-')}", "", ""))
        return {"platform": platform, "handle": handle, "url": canonical}, None
    except ValueError:
        return None, "invalid_url"


def parse_audited_accounts_csv(text):
    records = []
    skipped = []
    row_map = []
    fields = ("Market", "Handle / page", "Platform", "Evidence")
    for source_row, values in _source_csv_rows(text, fields):
        market = SOURCE_MARKETS.get(values["Market"].casefold())
        declared_platform = {"instagram": "instagram", "x": "twitter", "twitter": "twitter"}.get(
            values["Platform"].casefold())
        profile, reason = _source_profile(values["Evidence"])
        if not market:
            reason = "unknown_market"
        elif not declared_platform:
            reason = "unsupported_platform"
        elif not reason and declared_platform != profile["platform"]:
            reason = "platform_url_mismatch"
        elif not reason and values["Handle / page"].removeprefix("@").casefold() != profile["handle"].casefold():
            reason = "handle_url_mismatch"
        if reason:
            skipped.append({"market": market, "source_row": source_row, "reason": reason})
            row_map.append({"market": market, "source_row": source_row, "outcome": reason})
            continue
        record = {**profile, "market": market, "source_tag": AUDITED_ACCOUNT_TAG}
        records.append(record)
        row_map.append({"market": market, "source_row": source_row, "outcome": "candidate"})
    return {"records": records, "skipped": skipped, "row_map": row_map}


def audited_accounts_for_curated_manifest(records):
    manifest = []
    seen = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != set(AUDITED_ACCOUNT_FIELDS):
            raise ValueError("audited account rows must use the approved profile fields and source tag")
        if any(not isinstance(record[field], str) for field in AUDITED_ACCOUNT_FIELDS):
            raise ValueError("audited account profile fields must be strings")
        if record["source_tag"] != AUDITED_ACCOUNT_TAG or record["market"] not in MARKETS:
            raise ValueError("audited account source tag or market is unknown")
        active = {"instagram": True, "twitter": False}.get(record["platform"])
        if active is None:
            raise ValueError("audited account platform is unsupported")
        profile, reason = _source_profile(record["url"])
        if reason or profile != {field: record[field] for field in ("platform", "handle", "url")}:
            raise ValueError("audited account URL does not match its canonical profile fields")
        key = (record["market"], record["platform"], record["handle"].casefold())
        if key in seen:
            raise ValueError("audited account manifest repeats a profile within a market")
        seen.add(key)
        manifest.append({"platform": record["platform"], "handle": record["handle"], "url": record["url"],
                         "market": record["market"], "source": record["source_tag"], "active": active,
                         "source_tags": [record["source_tag"]]})
    return manifest


def _source_date_range(value):
    parts = re.split(r"\s+to\s+", value.strip(), flags=re.IGNORECASE)
    if not parts or len(parts) > 2:
        return None, None
    try:
        start = date.fromisoformat(parts[0])
        end = date.fromisoformat(parts[-1])
    except ValueError:
        return None, None
    if start.isoformat() != parts[0] or end.isoformat() != parts[-1] or end < start:
        return None, None
    return start.isoformat(), end.isoformat()


def parse_dated_topics_csv(text, source_tag):
    if source_tag not in {THABANG_CONFIRMED_TAG, THAPELO_CONFIRMED_TAG, EXPANDED_UNVERIFIED_TAG}:
        raise ValueError("dated topic source tag is unknown")
    records = []
    skipped = []
    row_map = []
    fields = ("date", "country", "topic", "event", "venue")
    for source_row, values in _source_csv_rows(text, fields):
        market = SOURCE_MARKETS.get(values["country"].casefold())
        start_date, end_date = _source_date_range(values["date"])
        reason = None
        if not market:
            reason = "unknown_market"
        elif not start_date:
            reason = "invalid_date_range"
        elif not values["topic"] or not values["event"]:
            reason = "missing_topic_or_event"
        if reason:
            skipped.append({"market": market, "source_row": source_row, "reason": reason})
            row_map.append({"market": market, "source_row": source_row, "outcome": reason})
            continue
        records.append({
            "start_date": start_date,
            "end_date": end_date,
            "market": market,
            "topic": values["topic"],
            "event": values["event"],
            "venue": values["venue"],
            "source_tag": source_tag,
        })
        row_map.append({"market": market, "source_row": source_row, "outcome": "candidate"})
    return {"records": records, "skipped": skipped, "row_map": row_map}


def _normalise_topic_payload(value):
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    chars = []
    for char in decomposed:
        if unicodedata.combining(char):
            continue
        if unicodedata.category(char)[0] in {"P", "S"}:
            chars.append(" ")
        else:
            chars.append(char)
    return " ".join("".join(chars).split())


def _dated_topic_identity(record):
    event = _normalise_topic_payload(record["event"])
    return record["start_date"], record["end_date"], record["market"], event


def _dated_topic_payload(record):
    return _normalise_topic_payload(record["venue"])


def reconcile_expanded_topics(base, expanded):
    base_meta = [item for item in base["row_map"] if item["outcome"] == "candidate"]
    expanded_meta = [item for item in expanded["row_map"] if item["outcome"] == "candidate"]
    if len(base["records"]) != len(base_meta) or len(expanded["records"]) != len(expanded_meta):
        raise ValueError("dated topic row mapping is incomplete")

    base_by_identity = {}
    for record, meta in zip(base["records"], base_meta):
        identity = _dated_topic_identity(record)
        if identity in base_by_identity:
            raise ValueError("base dated topics repeat an identity")
        base_by_identity[identity] = (record, meta)

    groups = defaultdict(list)
    for record, meta in zip(expanded["records"], expanded_meta):
        groups[_dated_topic_identity(record)].append((record, meta))

    new_records = []
    tag_additions = []
    held_variants = []
    sidecar = []
    row_map = []

    def add_sidecar(record, meta, classification, held, target_source_row=None):
        item = {"source_row": meta["source_row"], "market": record["market"],
                "classification": classification, "held": held, "record": dict(record)}
        if target_source_row is not None:
            item["target_base_source_row"] = target_source_row
        sidecar.append(item)
        if held:
            held_variants.append(item)
        mapping = {"source_row": meta["source_row"], "market": record["market"], "outcome": classification}
        if target_source_row is not None:
            mapping["target_base_source_row"] = target_source_row
        row_map.append(mapping)

    for identity, group in groups.items():
        base_entry = base_by_identity.get(identity)
        payloads = {_dated_topic_payload(record) for record, _ in group}
        if len(group) > 1 and len(payloads) > 1:
            target_source_row = base_entry[1]["source_row"] if base_entry else None
            for record, meta in group:
                add_sidecar(record, meta, "repeated_identity_payload_conflict_held", True, target_source_row)
            continue

        if base_entry:
            base_record, base_source = base_entry
            if _dated_topic_payload(group[0][0]) == _dated_topic_payload(base_record):
                tag_additions.append({**base_record, "source_tag": EXPANDED_UNVERIFIED_TAG})
                for index, (record, meta) in enumerate(group):
                    if _normalise_topic_payload(record["topic"]) != _normalise_topic_payload(base_record["topic"]):
                        classification = "base_identity_topic_variant_preserved"
                    else:
                        classification = ("base_identity_payload_equal" if index == 0
                                          else "exact_payload_duplicate_within_expanded")
                    add_sidecar(record, meta, classification, False, base_source["source_row"])
            else:
                for record, meta in group:
                    add_sidecar(record, meta, "base_identity_payload_conflict_held", True,
                                base_source["source_row"])
            continue

        new_records.append(dict(group[0][0]))
        for index, (record, meta) in enumerate(group):
            if index == 0:
                classification = "new_unverified_identity"
            elif _normalise_topic_payload(record["topic"]) != _normalise_topic_payload(group[0][0]["topic"]):
                classification = "topic_variant_preserved"
            else:
                classification = "exact_payload_duplicate_within_expanded"
            add_sidecar(record, meta, classification, False)

    return {"new_records": new_records, "tag_additions": tag_additions,
            "held_variants": held_variants, "sidecar": sidecar, "row_map": row_map}


def _date_format(value):
    text = re.sub(r"\[[^]]*\]\([^)]*\)", " ", value).strip().casefold()
    if not text:
        return "missing"
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
        return "iso_date"
    month_day = rf"\b(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b"
    day_month = rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})(?:\s+\d{{4}})?\b"
    if re.search(day_month, text):
        return "day_month_name"
    if re.search(month_day, text):
        return "month_day_name"
    if re.fullmatch(r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?", text):
        return "numeric_date"
    if re.search(r"\b(first|second|third|fourth|last|annual|annually|yearly|recurring|every year)\b", text):
        return "calendar_rule"
    return "unrecognized"


def _skip(market, kind, line, reason):
    return {"market": market, "type": kind, "line": line, "reason": reason}


def parse_research_markdown(markdown):
    lines = markdown.splitlines()
    market = None
    heading_kind = None
    profiles = []
    moments = []
    skipped = []
    line_index = 0

    while line_index < len(lines):
        line = lines[line_index]
        heading = _heading(line)
        if heading:
            level, title = heading
            if level <= 2:
                market = _market_from_heading(title) if level == 2 else None
                heading_kind = None
            elif level == 3:
                heading_kind = _kind_from_heading(title)

        if market not in MARKETS or "|" not in line or line_index + 1 >= len(lines):
            line_index += 1
            continue

        headers = _split_table_row(line)
        separator = _split_table_row(lines[line_index + 1])
        if len(headers) != len(separator) or not _is_separator(separator):
            line_index += 1
            continue

        roles, normalized_headers = _header_roles(headers)
        kind = _kind_from_headers(normalized_headers, heading_kind)
        if kind not in (*PROFILE_TYPES, "moment"):
            line_index += 2
            while line_index < len(lines) and "|" in lines[line_index] and lines[line_index].strip():
                line_index += 1
            continue

        line_index += 2
        while line_index < len(lines) and "|" in lines[line_index] and lines[line_index].strip():
            raw = lines[line_index]
            cells = _split_table_row(raw)
            source_line = line_index + 1
            if len(cells) != len(headers):
                skipped.append(_skip(market, kind, source_line, "malformed_table_row"))
                line_index += 1
                continue
            if "unconfirmed" in raw.casefold():
                skipped.append(_skip(market, kind, source_line, "unconfirmed"))
                line_index += 1
                continue

            explicit_market = cells[roles["market"]] if "market" in roles else ""
            row_market = _market_from_cell(explicit_market) if explicit_market else market
            if row_market != market:
                skipped.append(_skip(market, kind, source_line, "market_mismatch"))
                line_index += 1
                continue

            if kind == "moment":
                date_value = cells[roles["date"]] if "date" in roles else ""
                date_format = _date_format(date_value)
                reason = "seed_comparison_deferred" if date_format != "unrecognized" else "unsupported_date_format"
                moments.append({"market": market, "line": source_line, "date_format": date_format,
                                "reason": reason})
                line_index += 1
                continue

            url_cell = cells[roles["url"]] if "url" in roles else raw
            urls = _extract_urls(url_cell)
            if not urls:
                skipped.append(_skip(market, kind, source_line, "missing_url"))
                line_index += 1
                continue
            if len(urls) != 1:
                skipped.append(_skip(market, kind, source_line, "multiple_profile_urls"))
                line_index += 1
                continue

            declared_platform = cells[roles["platform"]] if "platform" in roles else None
            profile, reason = _profile(urls[0], declared_platform)
            if reason:
                skipped.append(_skip(market, kind, source_line, reason))
                line_index += 1
                continue
            handle_value = cells[roles["handle"]].strip().removeprefix("@") if "handle" in roles else ""
            if handle_value and handle_value.casefold() != profile["handle"].casefold():
                skipped.append(_skip(market, kind, source_line, "handle_url_mismatch"))
                line_index += 1
                continue

            record = {**profile, "market": market, "source": SOURCE_RESEARCH, "active": True}
            profiles.append({"record": record, "type": kind, "line": source_line})
            line_index += 1
    url_groups = defaultdict(list)
    for candidate in profiles:
        record = candidate["record"]
        url_groups[(record["platform"], record["url"].casefold())].append(candidate)
    duplicate_ids = {id(candidate) for group in url_groups.values() if len(group) > 1 for candidate in group}
    kept_profiles = []
    for candidate in profiles:
        if id(candidate) in duplicate_ids:
            skipped.append(_skip(candidate["record"]["market"], candidate["type"], candidate["line"],
                                 "duplicate_canonical_url"))
        else:
            kept_profiles.append(candidate)

    skipped.sort(key=lambda item: (MARKETS.index(item["market"]), item["line"], item["type"]))
    moments.sort(key=lambda item: (MARKETS.index(item["market"]), item["line"]))
    return {"profiles": kept_profiles, "moments": moments, "skipped": skipped}


def _normalise_existing(record):
    if not isinstance(record, dict):
        raise ValueError("existing profile records must be mappings")
    keys = set(record)
    if keys == set(PROFILE_FIELDS):
        result = {**record, "source": SOURCE_UEFA, "active": True}
    elif keys == set(RECORD_FIELDS) or keys == set(RECORD_FIELDS) | {"source_tags"}:
        result = dict(record)
    else:
        raise ValueError("profile records must use the approved fields")
    if any(not isinstance(result[field], str) for field in PROFILE_FIELDS):
        raise ValueError("profile fields must be strings")
    if result["market"] not in MARKETS:
        raise ValueError("profile market is unknown")
    if result["source"] not in SOURCE_TAGS:
        raise ValueError("profile source tag is unknown")
    if not isinstance(result["active"], bool):
        raise ValueError("profile active status must be boolean")
    source_tags = result.get("source_tags")
    if source_tags is not None:
        if (not isinstance(source_tags, (list, tuple)) or not source_tags
                or any(not isinstance(tag, str) or tag not in SOURCE_TAGS for tag in source_tags)
                or len(set(source_tags)) != len(source_tags) or result["source"] not in source_tags):
            raise ValueError("profile source tags must be unique approved values including its source")
        result["source_tags"] = list(source_tags)
    profile, reason = _profile(result["url"], result["platform"], allow_unpriced=not result["active"])
    if reason:
        raise ValueError("profile URL is invalid")
    result.update(profile)
    return result


def _validate_candidate(candidate):
    if not isinstance(candidate, dict) or set(candidate) != {"record", "type", "line"}:
        raise ValueError("research candidates must contain only record, type and line")
    if candidate["type"] not in PROFILE_TYPES or not isinstance(candidate["line"], int):
        raise ValueError("research candidate type or line is invalid")
    record = candidate["record"]
    if not isinstance(record, dict) or set(record) not in (set(RECORD_FIELDS), set(RECORD_FIELDS) | {"source_tags"}):
        raise ValueError("research profile must use the approved fields")
    if any(not isinstance(record[field], str) for field in PROFILE_FIELDS):
        raise ValueError("research profile fields must be strings")
    if record["market"] not in MARKETS or record["source"] not in SOURCE_TAGS:
        raise ValueError("research profile market or source tag is invalid")
    if not isinstance(record["active"], bool):
        raise ValueError("research profile active status is invalid")
    source_tags = record.get("source_tags")
    if source_tags is not None and (
            not isinstance(source_tags, (list, tuple)) or not source_tags
            or any(not isinstance(tag, str) or tag not in SOURCE_TAGS for tag in source_tags)
            or len(set(source_tags)) != len(source_tags) or record["source"] not in source_tags):
        raise ValueError("research profile source tags must be unique approved values including its source")
    profile, reason = _profile(record["url"], record["platform"], allow_unpriced=not record["active"])
    if reason or profile != {field: record[field] for field in ("platform", "handle", "url")}:
        raise ValueError("research profile URL does not match its canonical fields")
    return {**candidate, "record": dict(record)}


def merge_profiles(existing, candidates):
    records = [_normalise_existing(record) for record in existing]
    parsed_candidates = [_validate_candidate(candidate) for candidate in candidates]
    skipped = []
    statuses = []
    accepted = []

    existing_url_groups = defaultdict(list)
    for index, record in enumerate(records):
        existing_url_groups[(record["platform"], record["url"].casefold())].append(index)
    blocked_urls = set()
    for url_key, indexes in existing_url_groups.items():
        if len(indexes) > 1:
            blocked_urls.add(url_key)
            for index in indexes:
                records[index]["active"] = False

    existing_identity_market = defaultdict(list)
    existing_markets = defaultdict(set)
    for index, record in enumerate(records):
        identity = (record["platform"].casefold(), record["handle"].casefold())
        existing_identity_market[(identity, record["market"])].append(index)
        existing_markets[identity].add(record["market"])

    candidate_url_groups = defaultdict(list)
    for candidate in parsed_candidates:
        record = candidate["record"]
        candidate_url_groups[(record["platform"], record["url"].casefold())].append(candidate)
    duplicate_candidate_ids = {
        id(candidate) for group in candidate_url_groups.values() if len(group) > 1 for candidate in group
    }
    unique_candidates = []
    for candidate in parsed_candidates:
        if id(candidate) in duplicate_candidate_ids:
            record = candidate["record"]
            skipped.append(_skip(record["market"], candidate["type"], candidate["line"],
                                 "duplicate_canonical_url"))
        else:
            unique_candidates.append(candidate)

    candidate_identity_groups = defaultdict(list)
    for candidate in unique_candidates:
        record = candidate["record"]
        identity = (record["platform"].casefold(), record["handle"].casefold())
        candidate_identity_groups[identity].append(candidate)

    blocked_candidate_ids = set()
    for identity, group in candidate_identity_groups.items():
        markets = {candidate["record"]["market"] for candidate in group}
        existing_for_identity = existing_markets.get(identity, set())
        if len(markets) > 1 and not existing_for_identity:
            for candidate in group:
                blocked_candidate_ids.add(id(candidate))
                record = candidate["record"]
                skipped.append(_skip(record["market"], candidate["type"], candidate["line"],
                                     "ambiguous_cross_market"))
            continue
        by_market = defaultdict(list)
        for candidate in group:
            by_market[candidate["record"]["market"]].append(candidate)
        for market, same_market in by_market.items():
            if len(same_market) > 1 and len({candidate["record"]["url"] for candidate in same_market}) > 1:
                for candidate in same_market:
                    blocked_candidate_ids.add(id(candidate))
                    skipped.append(_skip(market, candidate["type"], candidate["line"],
                                         "identity_url_conflict"))

    for candidate in unique_candidates:
        if id(candidate) in blocked_candidate_ids:
            continue
        record = candidate["record"]
        identity = (record["platform"].casefold(), record["handle"].casefold())
        market = record["market"]
        url_key = (record["platform"], record["url"].casefold())
        if url_key in blocked_urls:
            skipped.append(_skip(market, candidate["type"], candidate["line"], "duplicate_canonical_url"))
            continue
        matching = existing_identity_market.get((identity, market), [])
        if len(matching) > 1:
            for index in matching:
                records[index]["active"] = False
            skipped.append(_skip(market, candidate["type"], candidate["line"], "duplicate_existing_identity"))
            continue
        if len(matching) == 1:
            index = matching[0]
            if record["source"] == AUDITED_ACCOUNT_TAG:
                existing_record = records[index]
                if existing_record["url"].casefold() != record["url"].casefold():
                    skipped.append(_skip(market, candidate["type"], candidate["line"], "existing_identity_conflict"))
                    continue
                existing_tags = list(existing_record.get("source_tags") or [existing_record["source"]])
                incoming_tags = list(record.get("source_tags") or [record["source"]])
                updated = dict(existing_record)
                updated["source_tags"] = list(dict.fromkeys(existing_tags + incoming_tags))
                records[index] = updated
                accepted.append(dict(updated))
                statuses.append({"market": market, "type": candidate["type"], "line": candidate["line"],
                                 "status": "already_present"})
                continue
            active = records[index]["active"] and record["active"]
            records[index] = {**record, "active": active}
            accepted.append(dict(records[index]))
            statuses.append({"market": market, "type": candidate["type"], "line": candidate["line"],
                             "status": "already_present"})
            continue
        if existing_markets.get(identity):
            skipped.append(_skip(market, candidate["type"], candidate["line"], "ambiguous_cross_market"))
            continue
        records.append(dict(record))
        accepted.append(dict(record))
        statuses.append({"market": market, "type": candidate["type"], "line": candidate["line"],
                         "status": "new"})

    statuses.sort(key=lambda item: (MARKETS.index(item["market"]), item["line"], item["type"]))
    skipped.sort(key=lambda item: (MARKETS.index(item["market"]), item["line"], item["type"]))
    return {"records": records, "accepted": accepted, "statuses": statuses, "skipped": skipped}


def build_preview_report(parsed, merged):
    counts = {
        market: {
            "creator": {"new": 0, "already_present": 0, "skipped_unconfirmed": 0, "skipped_other": 0},
            "outlet": {"new": 0, "already_present": 0, "skipped_unconfirmed": 0, "skipped_other": 0},
            "moment": {"new": 0, "already_present": 0, "skipped_unconfirmed": 0, "skipped_other": 0,
                       "seed_comparison_deferred": 0, "unsupported_date_format": 0},
        }
        for market in MARKETS
    }
    all_skipped = list(parsed["skipped"]) + list(merged["skipped"])
    for item in all_skipped:
        field = "skipped_unconfirmed" if item["reason"] == "unconfirmed" else "skipped_other"
        counts[item["market"]][item["type"]][field] += 1
    for item in merged["statuses"]:
        counts[item["market"]][item["type"]][item["status"]] += 1
    for item in parsed["moments"]:
        field = "seed_comparison_deferred" if item["reason"] == "seed_comparison_deferred" else "unsupported_date_format"
        counts[item["market"]]["moment"][field] += 1
    all_skipped.sort(key=lambda item: (MARKETS.index(item["market"]), item["line"], item["type"]))
    safe_moments = [{key: item[key] for key in ("market", "line", "date_format", "reason")}
                    for item in parsed["moments"]]
    return {
        "profiles": [dict(record) for record in merged["accepted"]],
        "counts": counts,
        "skipped": [{key: item[key] for key in ("market", "type", "line", "reason")} for item in all_skipped],
        "moments": safe_moments,
        "url_availability_checked": False,
    }
