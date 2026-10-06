"""Deterministic observation ids for collected rows.

The producer gave every raw row a fresh uuid4, so one item collected twice read as two
unknown observations. ``observation_id`` derives the id from the row's source identity
(source, platform and market) and one canonical content key, chosen in this order:

``native``
    the vendor's own id for the item, where the row carries ``native_id`` as a string
    or an integer. An integer (a Python or numpy integer, or a Decimal with an integral
    value) is spelled as its decimal string, so 7301 and "7301" are one key. A float, a
    bool, a non integral Decimal or any other type is no native key and the row falls
    through to the URL: a float cannot carry a 19 digit id exactly, and two ids that
    round to one float must never collide;
``url``
    the canonical http or https URL: scheme and host lowercased, the default port
    dropped, tracking parameters dropped and the remaining query parameters sorted by
    name, stably, so ?a=1&b=2 and ?b=2&a=1 are one key while a repeated name keeps the
    order of its values. A non empty fragment is kept, because a collector can locate
    an item by it: a comment without a permalink is {post url}#comment-{native id},
    and dropping the fragment would make every comment on one post one observation.
    Only an empty fragment is dropped;
``text``
    title and text with whitespace collapsed and nothing else changed, together with
    the author, tagged as a handle or a name, and the published time in UTC. Case is
    kept, so "US policy" and "us policy" stay two keys. A published time of exactly 10
    ASCII digits is read as epoch seconds and one of exactly 13 as epoch milliseconds,
    the same way for a string and an integer. An 8 digit string that is a valid date
    is read as YYYYMMDD at midnight UTC. Any other all digit value is kept verbatim
    as its digit string, never guessed into a time. A missing time of any kind
    (None, NaN, NaT) reads as absent, and a present time never reads as absent. A row
    with neither an author nor a published time has no stable key here: two posts
    reading "gm" must never meet.

Some URL forms are NOT merged: a trailing slash, a www prefix, the http or https
scheme and percent-encoding each stay part of the key. Each can name a different
resource on some server, and nothing in the row says which servers treat them as one.
Merging them would turn a wrong guess into two distinct items read as one observation,
which is worse than one item read as two, so a re-collection that changes one of these
forms reads as a new observation.

A row with none of the three has no stable key and ``observation_id`` returns None;
the caller keeps a random id for it, which reads as an unknown observation, as before.

The id names its derivation scheme as a prefix, ``obs1_`` followed by 32 hexadecimal
characters of a SHA256 over the canonical key. A later scheme takes a new prefix and so
never collides with this one, and a stored uuid4 stays a valid id under its own form:
nothing already written is rewritten.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from numbers import Integral, Real
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

OBSERVATION_ID_SCHEME = "obs1"
LEGACY_UUID4_SCHEME = "uuid4"

_DIGEST_CHARACTERS = 32
_DERIVED = re.compile(r"obs1_[0-9a-f]{32}")
_TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|igshid|mc_cid|mc_eid)$", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_DIGITS = re.compile(r"[0-9]+")
_SECOND_DIGITS = 10
_MILLISECOND_DIGITS = 13
_DATE_DIGITS = 8


def _absent(value: object) -> bool:
    """None, or a missing value that is not equal to itself (NaN, NaT, pandas NA)."""
    if value is None or isinstance(value, str):
        return value is None
    try:
        return bool(value != value)
    except (TypeError, ValueError):
        return True


def _clean(value: object) -> str:
    """A source identity label: NFKC normalised, whitespace collapsed and casefolded."""
    if _absent(value):
        return ""
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", str(value))).strip().casefold()


def _spaced(value: object) -> str:
    """Content: whitespace collapsed and trimmed, with case and characters kept."""
    if _absent(value):
        return ""
    return _SPACE.sub(" ", str(value)).strip()


def canonical_url(value: object) -> str | None:
    """The canonical form of an absolute http or https URL, or None for anything else."""
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
    query = urlencode(
        sorted(
            (
                (k, v)
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if not _TRACKING.match(k)
            ),
            key=lambda pair: pair[0],
        )
    )
    return urlunsplit((scheme, host, parts.path or "/", query, parts.fragment))


def _epoch(value: int | float) -> str:
    """A 10 digit value as epoch seconds, a 13 digit one as milliseconds, else verbatim."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    digits = len(str(abs(int(value))))
    try:
        if digits == _SECOND_DIGITS:
            return datetime.fromtimestamp(value, UTC).isoformat()
        if digits == _MILLISECOND_DIGITS:
            if isinstance(value, int):
                seconds, millis = divmod(value, 1000)
                moment = datetime.fromtimestamp(seconds, UTC) + timedelta(milliseconds=millis)
                return moment.isoformat()
            return datetime.fromtimestamp(value / 1000, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        pass
    return str(value) if isinstance(value, int) else repr(value)


def _digit_string(value: str) -> str:
    """An all digit time: epoch at 10 or 13 digits, YYYYMMDD at 8, else kept verbatim."""
    if len(value) == _DATE_DIGITS:
        try:
            moment = datetime(int(value[:4]), int(value[4:6]), int(value[6:]), tzinfo=UTC)
        except ValueError:
            return value
        return moment.isoformat()
    return _epoch(int(value))


def _published(value: object) -> str:
    """The published time in UTC ISO form, or "" only when no time is present."""
    if isinstance(value, bool) or _absent(value):
        return ""
    if isinstance(value, Integral):
        return _epoch(int(value))
    if isinstance(value, Real) and not isinstance(value, datetime):
        return _epoch(float(value))
    if isinstance(value, str):
        if not value.strip():
            return ""
        if _DIGITS.fullmatch(value.strip()):
            return _digit_string(value.strip())
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            return _spaced(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return _spaced(value)


def _native(value: object) -> str | None:
    """The native id as a string, from a string or an integer only."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Integral):
        return str(int(value))
    if isinstance(value, Decimal):
        if value.is_finite() and value == value.to_integral_value():
            return str(int(value))
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def content_key(row: Mapping) -> list[str] | None:
    """The canonical content key of one row, or None when it has no stable key."""
    native = _native(row.get("native_id"))
    if native is not None:
        return ["native", native]
    url = canonical_url(row.get("url"))
    if url is not None:
        return ["url", url]
    title, text = _spaced(row.get("title")), _spaced(row.get("text"))
    if not title and not text:
        return None
    handle, name = _spaced(row.get("author_handle")), _spaced(row.get("author_name"))
    author = ["handle", handle] if handle else ["name", name] if name else None
    published = _published(row.get("published_at"))
    if author is None and not published:
        return None
    return ["text", title, text, author, published]


def observation_id(row: Mapping) -> str | None:
    """The deterministic id of one collected row, or None when it has no stable key."""
    key = content_key(row)
    if key is None:
        return None
    identity = {
        "scheme": OBSERVATION_ID_SCHEME,
        "source": _clean(row.get("source")),
        "platform": _clean(row.get("platform")),
        "market": _clean(row.get("market")),
        "key": key,
    }
    body = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:_DIGEST_CHARACTERS]
    return f"{OBSERVATION_ID_SCHEME}_{digest}"


def observation_id_scheme(value: object) -> str | None:
    """Name the scheme an id was minted under: this scheme, a stored uuid4, or None."""
    if not isinstance(value, str) or not value:
        return None
    if _DERIVED.fullmatch(value):
        return OBSERVATION_ID_SCHEME
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    canonical = str(parsed) == value.lower()
    return LEGACY_UUID4_SCHEME if canonical and parsed.version == 4 else None
