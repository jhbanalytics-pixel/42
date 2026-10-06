"""Separate observation identity, shared origin and independent evidence.

An observation is one producer row, identified by the exact
(market, platform, source_row_id) tuple. Several matching terms, or a raw and an
enriched representation of one row, are the same observation. Two payloads that
claim one observation with different content digests refuse.

An origin is the authoritative source the producer established for a row. A non
null origin needs the producer's origin authority digest, a collector or vendor
name is never an origin, and the kernel never guesses an origin from a URL or a
hostname. Observations without an established origin are counted as unknown,
never as independent.

Origin ids are machine generated, never typed by a person, so the kernel holds
them to a declared grammar and refuses anything outside it, rather than folding
two spellings toward each other. Folding always loses: there is another unicode
class behind every fold, and one origin that folds badly becomes two independent
slots. Under a grammar two spellings are either the same string, which fills one
slot, or both refused by name. The grammar also states a length, because an origin
id is published verbatim as a slot key and inside an answer limitation.

The collector check compares on a separator normalised key. Nothing is stripped,
lowered or unicode folded, so a doctored collector name is still refused by the
grammar before the check ever sees it. The three separators the grammar accepts
carry no meaning of their own, so apple-music and apple_music name one collector
and a collector cannot respell its own name into an independent origin.
"""

import re
import unicodedata

MARKETS = frozenset({"za", "ng", "ke"})

RESULT_FIELDS = (
    "observation_keys",
    "origin_groups",
    "independent_origin_count",
    "unknown_origin_count",
)

# Pinned copy of the source keys in the producer's connector registry, the
# CONNECTORS list in engine/scripts/run_rss_now.py. Importing that module loads
# the environment file, configures logging and imports every connector, so the
# kernel does not import it. The unit suite parses the producer's source and
# compares it to this set, so drift fails a test instead of passing silently.
COLLECTOR_NAMES = frozenset(
    {
        "rss",
        "apple_music",
        "spotify",
        "bigquery_trends",
        "youtube",
        "youtube_scrape",
        "gdelt",
        "ensemble",
        "reddit",
        "brand24",
        "socialcrawl",
        "wikipedia",
        "bluesky",
        "semrush",
        "google_trends_rss",
        "app_charts",
        "audiomack",
        "cloudflare_radar",
        "pulsar",
    }
)

_HEX64 = re.compile(r"[0-9a-f]{64}")
# The declared origin id grammar, stated once for the refusal message. Every read of a
# retained record re-validates its origin ids against this grammar, so a record admitted
# under an older one is refused at read rather than silently trusted.
# An origin id reaches a published slot key and an answer limitation verbatim, so the
# grammar bounds its length rather than letting one row carry a megabyte of limitations
# into the answer and trip the answer's own size cap.
ORIGIN_ID_MAXIMUM = 128
ORIGIN_ID_GRAMMAR = (
    "lowercase ascii letters and digits in runs joined by single . - or _ separators, "
    f"at most {ORIGIN_ID_MAXIMUM} characters"
)
_ORIGIN_ID = re.compile(r"[a-z0-9]+(?:[.\-_][a-z0-9]+)*")
_SEPARATORS = re.compile(r"[.\-]")


def _collector_key(value: str) -> str:
    """The separator normalised key the closed collector set is compared on."""
    return _SEPARATORS.sub("_", value)


_COLLECTOR_KEYS = frozenset(_collector_key(name) for name in COLLECTOR_NAMES)


def _text(record: dict, field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field}: expected a non empty string")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field}: expected an NFC normalised string")
    return value


def observation_key(record: dict) -> tuple[str, str, str]:
    """Return the validated (market, platform, source_row_id) identity of one record."""
    if not isinstance(record, dict):
        raise ValueError("record: expected a dict of producer fields")
    market = _text(record, "market")
    if market not in MARKETS:
        raise ValueError(f"market: {market!r} is not one of za, ng, ke")
    platform = _text(record, "platform")
    source_row_id = _text(record, "source_row_id")
    return (market, platform, source_row_id)


def validate_origin_id(value: object) -> str:
    """Return one origin id held to the declared grammar, or refuse it by name.

    Nothing is stripped, lowered or unicode folded toward another spelling. A value outside
    the grammar is refused where it stands, so a doctored collector name never reaches the
    collector check below. That check compares on a separator normalised key, because the
    grammar treats . - and _ alike and a collector respelling its own name is the one
    collision the grammar cannot refuse on its own.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("origin_id: expected a non empty string")
    if len(value) > ORIGIN_ID_MAXIMUM:
        raise ValueError(
            f"origin_id: expected at most {ORIGIN_ID_MAXIMUM} characters, not {len(value)}"
        )
    if _ORIGIN_ID.fullmatch(value) is None:
        raise ValueError(
            f"origin_id: {value!r} is outside the declared origin id grammar "
            f"({ORIGIN_ID_GRAMMAR}), so it is refused rather than folded toward another "
            "origin or a collector name"
        )
    if _collector_key(value) in _COLLECTOR_KEYS:
        raise ValueError(f"origin_id: {value!r} names a collector, which is not an origin")
    return value


def validate_origin_authorities(authorities: object) -> dict:
    """Validate an injected origin authority set of declared origin ids to their digests.

    The set is what an origin's authority is checked against. Shape validation of a digest
    proves only that the caller can format hex, so a digest a record asserts for itself is
    never authority; only a digest this set declares for that origin is.
    """
    if not isinstance(authorities, dict):
        raise ValueError("origin_authorities: expected a mapping of origin id to digest")
    validated: dict[str, str] = {}
    for origin, digest in authorities.items():
        origin = validate_origin_id(origin)
        if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
            raise ValueError(f"origin_authorities: {origin!r} expected 64 lowercase hex characters")
        validated[origin] = digest
    return validated


def origin_authority_projection(records: list[dict]) -> dict:
    """Say whether origin authority was projected at all, beside the counts it produced.

    A zero independent origin count means one of two unrelated things: the records carried
    origin authority and none of it grouped, or the records never carried the field. The
    counts publish which, the way the native id projection publishes an absent column, so a
    zero that means the column does not exist never reads as a zero that was measured.
    """
    if not isinstance(records, list):
        raise ValueError("records: expected a list of producer records")
    projected = sum(
        1
        for record in records
        if isinstance(record, dict)
        and ("origin_id" in record or "origin_authority_digest" in record)
    )
    return {
        "state": "projected" if projected else "not_projected",
        "projected_records": projected,
        "unprojected_records": len(records) - projected,
    }


def _origin(record: dict) -> str | None:
    origin_id = record.get("origin_id")
    authority = record.get("origin_authority_digest")
    if origin_id is None:
        if authority is not None:
            raise ValueError("origin_authority_digest: present without an origin_id")
        return None
    origin_id = validate_origin_id(origin_id)
    if not isinstance(authority, str) or _HEX64.fullmatch(authority) is None:
        raise ValueError("origin_authority_digest: expected 64 lowercase hex characters")
    return origin_id


def validate_record(record: dict) -> tuple[str, str, str]:
    """Validate one producer record on its own and return its observation key.

    Identity, content digest and origin claim are all this record's own fields, so a caller
    holding several rows can refuse the one row it cannot establish and keep the rest,
    instead of losing every good row to one malformed one.
    """
    key = observation_key(record)
    _text(record, "content_digest")
    _origin(record)
    return key


def evidence_groups(records: list[dict]) -> dict:
    """Group records by exact observation tuple and by authoritative origin.

    Returns observation_keys (sorted unique tuples), origin_groups (origin id to
    sorted observation keys, keyed in sorted order), independent_origin_count
    (number of origin groups) and unknown_origin_count (observations with no
    established origin). Input order never changes the result.
    """
    if not isinstance(records, list):
        raise ValueError("records: expected a list of producer records")
    digests: dict[tuple[str, str, str], str] = {}
    origins: dict[tuple[str, str, str], str | None] = {}
    for record in records:
        key = observation_key(record)
        digest = _text(record, "content_digest")
        origin = _origin(record)
        if key in digests:
            if digests[key] != digest:
                raise ValueError(f"content_digest: observation {key!r} claims two content digests")
            known = origins[key]
            if origin is not None and known is not None and known != origin:
                raise ValueError(f"origin_id: observation {key!r} claims two origins")
            if known is None:
                origins[key] = origin
        else:
            digests[key] = digest
            origins[key] = origin
    keys = sorted(digests)
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for key in keys:
        origin = origins[key]
        if origin is not None:
            grouped.setdefault(origin, []).append(key)
    origin_groups = {origin: grouped[origin] for origin in sorted(grouped)}
    return {
        "observation_keys": keys,
        "origin_groups": origin_groups,
        "independent_origin_count": len(origin_groups),
        "unknown_origin_count": sum(1 for key in keys if origins[key] is None),
    }


def _slots(receipts: dict, origin_of: dict, families: set) -> dict:
    """Build the deterministic slot projection from bound receipts and their origins."""
    verified: dict[str, set[str]] = {}
    unknown: dict[tuple[str, str, str], set[str]] = {}
    for receipt_id, key in receipts.items():
        origin = origin_of[key]
        if origin is None:
            unknown.setdefault(key, set()).add(receipt_id)
        else:
            verified.setdefault(origin, set()).add(receipt_id)
    slots = [
        {
            "origin_id": origin,
            "independence": "verified_origin",
            "observation_keys": sorted({receipts[receipt_id] for receipt_id in ids}),
            "receipt_ids": sorted(ids),
        }
        for origin, ids in sorted(verified.items())
    ]
    slots.extend(
        {
            "origin_id": None,
            "independence": "unknown",
            "observation_keys": [key],
            "receipt_ids": sorted(ids),
        }
        for key, ids in sorted(unknown.items())
    )
    return {
        "slots": slots,
        "independent_support_slots": len(verified),
        "unknown_support_slots": len(unknown),
        "units": {
            "collected_records": len(receipts),
            "unique_observations": len(set(receipts.values())),
            "source_families": len(families),
            "verified_independent_origins": len(verified),
            "unknown_origins": len(unknown),
        },
    }


def _bind_receipts(records: list[dict], known: set | None) -> tuple[dict, set]:
    """Bind each record to its receipt id and observation key, refusing a conflicting claim."""
    if not isinstance(records, list):
        raise ValueError("records: expected a list of producer records")
    receipts: dict[str, tuple[str, str, str]] = {}
    families: set[str] = set()
    for record in records:
        receipt_id = _text(record, "receipt_id")
        key = observation_key(record)
        if known is not None and key not in known:
            raise ValueError(f"observation_keys: {key!r} is not a derived observation")
        if receipts.setdefault(receipt_id, key) != key:
            raise ValueError(f"receipt_id: {receipt_id!r} claims two observations")
        if record.get("source_family") is not None:
            families.add(_text(record, "source_family"))
    return receipts, families


def support_slots(records: list[dict]) -> dict:
    """Collapse cited records into the support slots the evidence can actually fill.

    Each record carries its `receipt_id` beside the producer fields. Records that share
    one verified origin fill a single slot, records of one observation fill a single
    slot, and an observation without established origin proof keeps its own slot marked
    unknown. Unknown slots never count as independent support, so several copies of one
    story cannot present themselves as several independent sources. Input order never
    changes the result.
    """
    if not isinstance(records, list):
        raise ValueError("records: expected a list of producer records")
    groups = evidence_groups(records)
    origin_of: dict[tuple[str, str, str], str | None] = dict.fromkeys(groups["observation_keys"])
    for origin, keys in groups["origin_groups"].items():
        for key in keys:
            origin_of[key] = origin
    receipts, families = _bind_receipts(records, None)
    return _slots(receipts, origin_of, families)


def _derived_keys(observation_keys) -> set:
    if not isinstance(observation_keys, list):
        raise ValueError("observation_keys: expected a list of derived observation keys")
    keys = set()
    for key in observation_keys:
        if not isinstance(key, (list, tuple)) or len(key) != 3:
            raise ValueError("observation_keys: expected market, platform and source row id")
        keys.add(
            observation_key(dict(zip(("market", "platform", "source_row_id"), key, strict=True)))
        )
    if len(keys) != len(observation_keys):
        raise ValueError("observation_keys: expected distinct observation keys")
    return keys


def support_slots_from_groups(records: list[dict], observation_keys, origin_groups) -> dict:
    """Slots for records whose groups the record itself already carries.

    A stored record keeps the derived observation keys and origin groups it was admitted
    with, so independence is read from them rather than inferred again from the rows. A
    record naming an observation the groups never derived, an origin group over an
    unknown observation, one observation under two origins, or a collector as an origin
    refuses instead of scoring support.
    """
    keys = _derived_keys(observation_keys)
    if not isinstance(origin_groups, dict):
        raise ValueError("origin_groups: expected a mapping of origin id to observations")
    origin_of: dict[tuple[str, str, str], str | None] = dict.fromkeys(keys)
    for origin, group in origin_groups.items():
        origin = validate_origin_id(origin)
        if not isinstance(group, list) or not group:
            raise ValueError(f"origin_groups: {origin!r} carries no observation")
        for member in group:
            if not isinstance(member, (list, tuple)) or len(member) != 3:
                raise ValueError("origin_groups: expected market, platform and source row id")
            key = tuple(member)
            if key not in keys:
                raise ValueError(f"origin_groups: {key!r} is not a derived observation")
            if origin_of[key] is not None and origin_of[key] != origin:
                raise ValueError(f"origin_id: observation {key!r} claims two origins")
            origin_of[key] = origin
    receipts, families = _bind_receipts(records, keys)
    return _slots(receipts, origin_of, families)
