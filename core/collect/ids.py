"""Stable post identity (DATA.md section 1: post_id is observation_id() with market='').

Lifted from engine/src/ingestion/observation_id.py, keeping the two keys a SocialCrawl post
has: the vendor's native id, else the canonical http or https URL. The digest is the old
scheme's byte for byte, with source 'socialcrawl' and an empty market, so one post read in
ZA, NG and KE, on any run and through any route, is one post_id, and a legacy row keyed the
same way meets it. The old text key (text plus author plus time) is not lifted: a post with
neither an id nor a URL cannot be cited as evidence, so the parser skips it.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal
from numbers import Integral
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SCHEME = "obs1"
SOURCE = "socialcrawl"

_TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|igshid|mc_cid|mc_eid)$", re.IGNORECASE)
_SPACE = re.compile(r"\s+")


def _clean(value: object) -> str:
    """A source identity label: NFKC normalised, whitespace collapsed and casefolded."""
    if value is None:
        return ""
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", str(value))).strip().casefold()


def _native(value: object) -> str | None:
    """The native id as a string, from a string or an integer only (a float cannot hold 19 digits)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Integral):
        return str(int(value))
    if isinstance(value, Decimal):
        return str(int(value)) if value.is_finite() and value == value.to_integral_value() else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def canonical_url(value: object) -> str | None:
    """Scheme and host lowercased, default port and tracking parameters dropped, query sorted by name."""
    if not isinstance(value, str) or not value.strip():
        return None
    parts = urlsplit(value.strip())
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
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
    query = urlencode(sorted(
        ((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)),
        key=lambda pair: pair[0]))
    return urlunsplit((scheme, host, parts.path or "/", query, parts.fragment))


def post_id(platform: str, native_id: object = None, url: object = None) -> str | None:
    """obs1_ plus 32 hex characters, or None when the post has neither a native id nor an http URL."""
    native = _native(native_id)
    if native is not None:
        key = ["native", native]
    else:
        link = canonical_url(url)
        if link is None:
            return None
        key = ["url", link]
    identity = {"scheme": SCHEME, "source": SOURCE, "platform": _clean(platform), "market": "", "key": key}
    body = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{SCHEME}_{hashlib.sha256(body.encode('utf-8')).hexdigest()[:32]}"
