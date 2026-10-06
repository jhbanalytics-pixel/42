"""The one definition of item identity in 42.

Every item in cultural_map, post_items, item_daily and item_counter_daily is named by
item_id = hex sha256 of "{kind}|{canonical_key}" (DATA.md section 1). This module is the
only place that key is built, so the collect job, the backfill and detection all agree.
It uses the standard library only, except is_generic, which reads stoplist.yaml with PyYAML on first use.

Kinds: topic, hashtag, sound, format, meme, creator, brand, event.

canonical_key(kind, raw, platform=None)
    hashtag: NFKC, leading # removed, casefolded, trimmed. '#Amapiano', 'amapiano' and
        the full-width form are one key, 'amapiano'.
    sound: "{platform}:{native sound id}". The id is kept as the vendor spells it,
        because sound ids are native ids; an integer id reads as its decimal string.
    creator: "{platform}:{handle}", the handle NFKC, casefolded and without a leading @.
    topic, meme, brand, event, format: NFKC, casefolded, whitespace collapsed.
    The platform is NFKC and casefolded. A sound or creator without a platform, an
    unknown kind, or a raw value that is empty after folding raises ValueError.

item_id(kind, canonical_key)
    Hex sha256 of "{kind}|{canonical_key}", 64 characters. Raises ValueError for an
    unknown kind. Pass a key from canonical_key; this function does not fold it again.

items_for_post(post)
    Items read straight from a posts row: a mapping with platform, hashtags (a list of
    strings), sound_id and creator_id. Returns one dict per item with item_id, kind,
    canonical_key, label and via (hashtag, sound or creator), in that order: hashtags
    as listed, then the sound, then the creator. A hashtag that folds to a key already
    seen in the post is kept once, with the label of its first spelling. Junk hashtags
    are skipped: empty, not a string, no letter or digit, containing whitespace, or
    longer than 100 characters. Without a platform only hashtags are returned, since a
    sound or creator key needs one. The label is the display form: '#' plus the
    hashtag as first written, or the raw sound id or creator handle.

is_generic(kind, canonical_key)
    True when the item is platform-generic per stoplist.yaml: a hashtag such as #fyp or #viral
    that rides on every kind of post. The key is folded again, so a raw spelling works too.
    Only hashtags are listed; any other kind is never generic. cultural_map carries such items
    with status 'generic', so they keep their states but are never eligible (item_state.eligible
    false); the brief skips ineligible items.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from functools import cache
from pathlib import Path

KINDS = ("topic", "hashtag", "sound", "format", "meme", "creator", "brand", "event")
HASHTAG_MAX = 100
STOPLIST_PATH = Path(__file__).parent / "stoplist.yaml"

_SPACE = re.compile(r"\s+")


def _nfkc(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip()


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value.casefold()).strip()


def canonical_key(kind: str, raw: object, platform: str | None = None) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown item kind {kind!r}")
    if raw is None:
        raise ValueError("an item needs a raw value")
    text = _nfkc(raw)
    if kind == "hashtag":
        key = _fold(text.lstrip("#"))
    elif kind in ("sound", "creator"):
        if not platform or not _nfkc(platform):
            raise ValueError(f"a {kind} key needs a platform")
        ident = text if kind == "sound" else _fold(text.lstrip("@"))
        if not ident:
            raise ValueError(f"empty {kind} id")
        return f"{_fold(_nfkc(platform))}:{ident}"
    else:
        key = _SPACE.sub(" ", _fold(text))
    if not key:
        raise ValueError(f"empty {kind} key")
    return key


def item_id(kind: str, canonical_key: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown item kind {kind!r}")
    return hashlib.sha256(f"{kind}|{canonical_key}".encode()).hexdigest()


def _hashtag_key(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        key = canonical_key("hashtag", raw)
    except ValueError:
        return None
    if len(key) > HASHTAG_MAX or _SPACE.search(key) or not any(ch.isalnum() for ch in key):
        return None
    return key


def _row(kind: str, key: str, label: str, via: str) -> dict:
    return {"item_id": item_id(kind, key), "kind": kind, "canonical_key": key, "label": label, "via": via}


def items_for_post(post: Mapping) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    hashtags = post.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = [hashtags]
    for raw in hashtags:
        key = _hashtag_key(raw)
        if key is None or key in seen:
            continue
        seen.add(key)
        rows.append(_row("hashtag", key, "#" + _nfkc(raw).lstrip("#"), "hashtag"))
    platform = post.get("platform")
    if not platform:
        return rows
    for kind, field in (("sound", "sound_id"), ("creator", "creator_id")):
        raw = post.get(field)
        if raw is None or not _nfkc(raw):
            continue
        try:
            key = canonical_key(kind, raw, platform)
        except ValueError:
            continue
        rows.append(_row(kind, key, _nfkc(raw), kind))
    return rows


@cache
def _stoplist() -> frozenset[str]:
    import yaml

    raw = yaml.safe_load(STOPLIST_PATH.read_text(encoding="utf-8")) or {}
    return frozenset(canonical_key("hashtag", t) for t in raw.get("hashtag") or [])


def is_generic(kind: str, canonical_key: str) -> bool:
    return kind == "hashtag" and _hashtag_key(canonical_key) in _stoplist()
