"""Pure canonical observation adapters for dynamic discovery."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime

from src.utils.geo_blocklist import text_matches_geo_blocklist

APPROVED_MARKETS = frozenset({"za", "ng", "ke"})
SOURCE_FAMILY_MAP_VERSION = "channel_family_v2"
WAVE1_SOURCE_FAMILY_MAP_VERSION = SOURCE_FAMILY_MAP_VERSION
APPROVED_SOURCE_IDENTITY_PAIRS = frozenset(
    {
        ("rss", "news"),
        ("gdelt", "news"),
        ("google_youtube", "youtube"),
        ("socialcrawl", "short_video"),
        ("socialcrawl", "youtube"),
        ("socialcrawl", "reddit"),
        ("socialcrawl", "twitter"),
        ("socialcrawl", "threads"),
        ("socialcrawl", "news"),
        ("google_trends", "search"),
        ("apple_music", "music"),
        ("brand24", "brand24"),
    }
)
QUALIFYING_SOURCE_FAMILIES = frozenset(
    {
        "apps",
        "bluesky",
        "brand24",
        "ensemble",
        "music",
        "news",
        "reddit",
        "search",
        "web_attention",
        "wikipedia",
        "youtube",
        "short_video",
        "twitter",
        "threads",
    }
)
_FAM = {
    "article": "news",
    "rss": "news",
    "gdelt": "news",
    "gdelt_gkg": "news",
    "trends": "search",
    "bigquery_trends": "search",
    "google trends": "search",
    "google_trends_rss": "search",
    "semrush": "search",
    # SocialCrawl channel families since 4 Sep 2026; "ensemble" survives only
    # as the retired vendor's own name.
    "tiktok": "short_video",
    "instagram": "short_video",
    "threads": "threads",
    "twitter": "twitter",
    "ensembledata": "ensemble",
    "brand24_legacy": "brand24",
    "apple_music": "music",
    "audiomack": "music",
    "video": "youtube",
    "forum": "reddit",
}
_PLAT = {"forum": "reddit", "video": "youtube", "google_search": "search"}
_PLATFORMS = frozenset(
    {
        "apps",
        "short_video",
        "bluesky",
        "brand24",
        "ensemble",
        "gdelt",
        "facebook",
        "instagram",
        "music",
        "news",
        "reddit",
        "rss",
        "search",
        "seed_candidates",
        "threads",
        "tiktok",
        "twitter",
        "web",
        "web_attention",
        "wikipedia",
        "youtube",
    }
)
_SEED_GRAPH_IDENTITY = {
    "youtube": ("google_youtube", "youtube"),
    "tiktok": ("socialcrawl", "short_video"),
    "instagram": ("socialcrawl", "short_video"),
    "reddit": ("socialcrawl", "reddit"),
    "twitter": ("socialcrawl", "twitter"),
    "threads": ("socialcrawl", "threads"),
}
_SEED_GRAPH_PLATFORMS = frozenset(
    {
        "reddit",
        "news",
        "tiktok",
        "youtube",
        "instagram",
        "web",
        "twitter",
        "threads",
        "search",
        "music",
        "facebook",
    }
)
_EVENT = {"entity": "entity", "search_term": "keyword", "headline": "headline"}
_GRAPH = {
    "token": "keyword",
    "slang": "slang",
    "hashtag": "hashtag",
    "handle": "creator",
    "channel": "creator",
    "music": "sound",
    "entity": "entity",
}
_CAND = {
    "keyword": "keyword",
    "slang": "slang",
    "hashtag": "hashtag",
    "handle": "creator",
    "channel": "creator",
    "creator": "creator",
    "music": "sound",
    "sound": "sound",
    "entity": "entity",
}
_TYPES = frozenset({"keyword", "slang", "hashtag", "entity", "headline", "creator", "sound"})
_GENERIC = frozenset(
    {
        "africa",
        "breaking",
        "facebook",
        "government",
        "instagram",
        "kenya",
        "lagos",
        "minister",
        "music",
        "nairobi",
        "news",
        "nigeria",
        "official",
        "party",
        "people",
        "president",
        "south",
        "state",
        "team",
        "tiktok",
        "today",
        "trending",
        "video",
        "viral",
        "whatsapp",
        "youtube",
    }
)


def _seq(v: object, field: str, *, nulls=False, empties=False, none=True) -> tuple[str, ...]:
    if v is None and none:
        return ()
    if not isinstance(v, (tuple, list, set, frozenset)):
        raise ValueError(f"{field} must be a collection of exact strings")
    out = set()
    for x in v:
        if x is None and nulls:
            continue
        if isinstance(x, str) and not x.strip() and empties:
            continue
        if not isinstance(x, str) or not x.strip():
            raise ValueError(f"{field} contains a nonstring or empty value")
        out.add(x.strip())
    return tuple(sorted(out))


def _text(v: object, f: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{f} must be a nonempty string")
    return v.strip()


def _term(v: object) -> str:
    s = unicodedata.normalize("NFKC", _text(v, "term"))
    s = "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    ).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", s)).strip()


def _optional_terms(v: object, field: str, *, nulls=False, empties=False) -> tuple[str, ...]:
    terms = (_term(item) for item in _seq(v, field, nulls=nulls, empties=empties))
    return tuple(sorted({term for term in terms if term}))


def _creator(v: object) -> str:
    s = unicodedata.normalize("NFKC", _text(v, "creator id")).casefold().lstrip("@")
    s = re.sub(r"[\s.-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s or re.fullmatch(r"[a-z0-9_]+", s) is None:
        raise ValueError("invalid creator id")
    return s


def _market(v: object) -> str:
    if not isinstance(v, str) or v not in APPROVED_MARKETS:
        raise ValueError("market must be an approved lower-case market")
    return v


def _family(v: object, nonq=False) -> str:
    k = _text(v, "source family").lower()
    f = _FAM.get(k, k)
    if f not in QUALIFYING_SOURCE_FAMILIES and not (nonq and f == "seed_graph"):
        raise ValueError(f"unsupported source family: {k}")
    return f


def _families(v: object) -> tuple[str, ...]:
    return tuple(sorted({_family(x) for x in _seq(v, "source family", none=False)}))


def _platform(v: object) -> str:
    k = _text(v, "platform").lower()
    p = _PLAT.get(k, k)
    if p not in _PLATFORMS:
        raise ValueError(f"unsupported platform: {k}")
    return p


def validate_source_identity(
    vendor_family: object, channel_family: object, *, fixture: bool = False
) -> tuple[str, str]:
    vendor = _text(vendor_family, "vendor family").lower()
    raw_channel = _text(channel_family, "channel family").lower()
    # A value that already names a qualifying family is a channel family and stays as it is;
    # anything else is a platform or source name and goes through the engine family map.
    channel = raw_channel if raw_channel in QUALIFYING_SOURCE_FAMILIES else _family(raw_channel)
    if fixture and vendor.startswith("staging_fixture_") and channel.startswith("staging_fixture_"):
        return vendor, channel
    if (vendor, channel) not in APPROVED_SOURCE_IDENTITY_PAIRS:
        raise ValueError("vendor and channel family pair is unsupported")
    return vendor, channel


def resolve_v1_fixture_source_identity(source_family: object) -> tuple[str, str]:
    family = _text(source_family, "source family").lower()
    if not family.startswith("staging_fixture_"):
        raise ValueError("V1 family-only identity is fixture only")
    return family, family


def resolve_source_identity(
    *,
    source: object,
    platform: object,
    content_type: object = None,
    endpoint: object = None,
    vendor_family: object = None,
    channel_family: object = None,
) -> tuple[str, str]:
    if vendor_family is not None or channel_family is not None:
        if vendor_family is None or channel_family is None:
            raise ValueError("source identity requires vendor and channel")
        return validate_source_identity(vendor_family, channel_family)
    source_value = _text(source, "source").lower()
    platform_value = _text(platform, "platform").lower()
    content_value = str(content_type or "").strip().lower()
    endpoint_value = str(endpoint or "").strip().lower().removeprefix("/v1/")
    if content_value == "gdelt_gkg":
        return validate_source_identity("gdelt", "news")
    if source_value == "brand24":
        # Retained Brand24 evidence is legacy on both axes and never an independent channel.
        return validate_source_identity("brand24", "brand24")
    if source_value == "socialcrawl":
        route_platform = endpoint_value.split("/", 1)[0] if endpoint_value else platform_value
        channel_by_platform = {
            "tiktok": "short_video",
            "instagram": "short_video",
            "youtube": "youtube",
            "reddit": "reddit",
            "twitter": "twitter",
            "threads": "threads",
            "news": "news",
        }
        channel = channel_by_platform.get(route_platform)
        if channel is None:
            raise ValueError("source identity is unavailable")
        return validate_source_identity("socialcrawl", channel)
    if source_value.startswith("gdelt"):
        return validate_source_identity("gdelt", "news")
    if source_value in {"google trends", "bigquery_trends", "google_trends_rss"} or (
        platform_value == "google_search"
    ):
        return validate_source_identity("google_trends", "search")
    if source_value == "apple_music":
        return validate_source_identity("apple_music", "music")
    if source_value == "rss" or (
        platform_value in {"web", "news"} and content_value in {"article", "news"}
    ):
        return validate_source_identity("rss", "news")
    if platform_value == "youtube":
        return validate_source_identity("google_youtube", "youtube")
    if platform_value == "reddit" or source_value == "reddit":
        return validate_source_identity("socialcrawl", "reddit")
    raise ValueError("source identity is unavailable")


def resolve_wave1_market(
    *,
    vendor_market: object,
    retained_geo_market: object,
    geo_method_id: object,
    geo_receipt_id: object,
) -> str | None:
    if retained_geo_market is None:
        return None
    if geo_method_id is None or geo_receipt_id is None:
        return None
    market = _market(retained_geo_market)
    _text(geo_method_id, "geo method id")
    _text(geo_receipt_id, "geo receipt id")
    if vendor_market is not None and vendor_market != market:
        raise ValueError("vendor and retained geography disagree")
    return market


def deduplicate_wave1_content(rows) -> tuple[dict[str, object], ...]:
    try:
        ordered = sorted((dict(row) for row in rows), key=lambda row: str(row.get("row_id")))
    except (TypeError, ValueError) as exc:
        raise ValueError("Wave 1 content rows are invalid") from exc
    seen_native: set[tuple[str, str]] = set()
    seen_urls: set[str] = set()
    output = []
    for row in ordered:
        platform = _text(row.get("platform"), "platform").lower()
        native_id = _text(row.get("native_id"), "native id")
        url = _text(row.get("url"), "url").strip().casefold()
        native_key = (platform, native_id)
        if native_key in seen_native or url in seen_urls:
            continue
        seen_native.add(native_key)
        seen_urls.add(url)
        output.append(row)
    return tuple(output)


def adapt_wave1_evidence_rows(rows) -> tuple[dict[str, object], ...]:
    resolved = []
    for raw in rows:
        row = dict(raw)
        market = resolve_wave1_market(
            vendor_market=row.get("vendor_market"),
            retained_geo_market=row.get("retained_geo_market"),
            geo_method_id=row.get("geo_method_id"),
            geo_receipt_id=row.get("geo_receipt_id"),
        )
        if market is None:
            continue
        vendor, channel = resolve_source_identity(
            source=row.get("source"),
            platform=row.get("platform"),
            content_type=row.get("content_type"),
            endpoint=row.get("endpoint"),
            vendor_family=row.get("vendor_family"),
            channel_family=row.get("channel_family"),
        )
        row["market"] = market
        row["vendor_family"] = vendor
        row["channel_family"] = channel
        row["source_family"] = channel
        row["source_family_map_version"] = WAVE1_SOURCE_FAMILY_MAP_VERSION
        resolved.append(row)
    return deduplicate_wave1_content(resolved)


def _num(v: object, f: str, opt=False):
    if v is None and opt:
        return None
    if (
        isinstance(v, bool)
        or not isinstance(v, (int, float))
        or not math.isfinite(float(v))
        or float(v) < 0
    ):
        raise ValueError(f"{f} must be finite nonnegative numeric")
    return float(v)


def _date(v: object) -> str:
    return (
        (v.date() if isinstance(v, datetime) else v).isoformat()
        if isinstance(v, (date, datetime))
        else date.fromisoformat(_text(v, "date")).isoformat()
    )


def _time(v: object) -> str:
    d = (
        v
        if isinstance(v, datetime)
        else datetime.fromisoformat(_text(v, "timestamp").replace("Z", "+00:00"))
    )
    d = d if d.tzinfo else d.replace(tzinfo=UTC)
    return d.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Observation:
    market: str
    term: str
    candidate_type: str
    row_ids: object
    source_families: object = None
    platforms: object = None
    aliases: object = None
    observed_dates: object = None
    observed_timestamps: object = None
    co_occurring_terms: object = None
    topic_tags: object = None
    near_topics: object = None
    creator_ids: object = None
    weight: float = 1.0
    candidate_score: float | None = None
    verified_search_spike: bool = False
    geo_rejected: bool = False
    vendor_family: str | None = None
    channel_family: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "market", _market(self.market))
        raw_term = self.term
        ct = _text(self.candidate_type, "candidate type").lower()
        if ct not in _TYPES:
            raise ValueError(f"unsupported candidate type: {ct}")
        object.__setattr__(self, "candidate_type", ct)
        canonical_term = _creator(raw_term) if ct == "creator" else _term(raw_term)
        if not canonical_term:
            raise ValueError("invalid term")
        object.__setattr__(self, "term", canonical_term)
        rows = _seq(self.row_ids, "stable row receipt", none=False)
        if not rows:
            raise ValueError("stable row receipt")
        object.__setattr__(self, "row_ids", rows)
        fs = _families(self.source_families) if self.source_families else ()
        object.__setattr__(self, "source_families", fs)
        ps = tuple(sorted({_platform(x) for x in _seq(self.platforms, "platform")}))
        if not ps:
            raise ValueError("platform")
        object.__setattr__(self, "platforms", ps)
        object.__setattr__(
            self,
            "aliases",
            _optional_terms(self.aliases, "alias", nulls=True, empties=True),
        )
        object.__setattr__(
            self,
            "observed_dates",
            tuple(sorted({_date(x) for x in _seq(self.observed_dates, "observed date")})),
        )
        object.__setattr__(
            self,
            "observed_timestamps",
            tuple(sorted({_time(x) for x in _seq(self.observed_timestamps, "observed timestamp")})),
        )
        object.__setattr__(
            self,
            "co_occurring_terms",
            _optional_terms(self.co_occurring_terms, "co-occurring term"),
        )
        object.__setattr__(
            self,
            "creator_ids",
            tuple(sorted({_creator(x) for x in _seq(self.creator_ids, "creator id")})),
        )
        object.__setattr__(self, "near_topics", _seq(self.near_topics, "near topic"))
        object.__setattr__(self, "topic_tags", _seq(self.topic_tags, "topic tag"))
        object.__setattr__(self, "weight", _num(self.weight, "weight"))
        object.__setattr__(self, "candidate_score", _num(self.candidate_score, "score", True))
        if type(self.verified_search_spike) is not bool or type(self.geo_rejected) is not bool:
            raise ValueError("flags must be boolean")
        if (self.vendor_family is None) != (self.channel_family is None):
            raise ValueError("vendor and channel family must be supplied together")
        if self.vendor_family is not None:
            vendor, channel = validate_source_identity(self.vendor_family, self.channel_family)
            if fs != (channel,):
                raise ValueError("source family must equal channel family")
            object.__setattr__(self, "vendor_family", vendor)
            object.__setattr__(self, "channel_family", channel)


def _key(o):
    score = (0, 0.0) if o.candidate_score is None else (1, o.candidate_score)
    return (
        o.market,
        o.term,
        o.candidate_type,
        o.row_ids,
        o.source_families,
        o.vendor_family,
        o.channel_family,
        o.platforms,
        o.aliases,
        o.observed_dates,
        o.observed_timestamps,
        o.co_occurring_terms,
        o.topic_tags,
        o.near_topics,
        o.creator_ids,
        o.weight,
        score,
        o.verified_search_spike,
        o.geo_rejected,
    )


@dataclass(frozen=True)
class CandidateInputs:
    event_ledger: object = ()
    seed_graph: object = ()
    seed_candidates: object = ()

    def __post_init__(self):
        for f in ("event_ledger", "seed_graph", "seed_candidates"):
            v = getattr(self, f)
            if not isinstance(v, (tuple, list)) or not all(isinstance(x, Observation) for x in v):
                raise ValueError(f)
            object.__setattr__(self, f, tuple(sorted(v, key=_key)))

    @property
    def observations(self):
        return tuple(
            sorted((*self.event_ledger, *self.seed_graph, *self.seed_candidates), key=_key)
        )


def _type(v, m):
    k = _text(v, "candidate type").lower()
    if k not in m:
        raise ValueError(f"unsupported candidate type: {k}")
    return m[k]


def _opt(row, *keys):
    out = set()
    for k in keys:
        if row.get(k) is not None:
            out.update(_seq(row[k], k))
    return tuple(sorted(out))


def extract_event_observations(rows):
    out = []
    for r in rows:
        ct = _type(r.get("event_kind"), _EVENT)
        fs = _families(r.get("corroborating_sources"))
        rid = _text(r.get("ledger_id"), "stable row receipt")
        if not fs:
            raise ValueError("source family")
        score = _num(r.get("max_search_velocity"), "score", True)
        sp = (
            r.get("verified_search_spike", False)
            or (ct == "keyword" and fs == ("search",))
            or (score is not None and score >= 0.5 and "search" in fs)
        )
        out.append(
            Observation(
                market=r.get("market"),
                term=r.get("entity_key"),
                candidate_type=ct,
                row_ids=(rid,),
                platforms=(fs[0],),
                source_families=fs,
                aliases=_seq(r.get("entity_aliases"), "alias", nulls=True, empties=True),
                observed_dates=tuple(_date(x) for x in [r.get("trend_date")] if x is not None),
                observed_timestamps=tuple(_time(x) for x in [r.get("as_of")] if x is not None),
                co_occurring_terms=_opt(r, "co_occurring_terms"),
                topic_tags=_opt(r, "topic_tags", "topic_groups"),
                near_topics=_opt(r, "near_topics"),
                creator_ids=_opt(r, "creator_ids"),
                weight=_num(r.get("source_count", 1), "weight"),
                candidate_score=score,
                verified_search_spike=sp,
                geo_rejected=r.get("geo_rejected", False),
            )
        )
    return tuple(sorted(out, key=_key))


def extract_seed_graph_observations(rows):
    out = []
    for r in rows:
        ct = _type(r.get("term_type"), _GRAPH)
        term = _creator(r.get("term")) if ct == "creator" else _term(r.get("term"))
        if not term:
            continue
        ids = _seq(r.get("sample_row_ids"), "stable row receipt", none=False)
        if not ids:
            raise ValueError("stable row receipt")
        p = _text(r.get("platform"), "platform").lower()
        if p not in _SEED_GRAPH_PLATFORMS:
            raise ValueError("platform")
        # A seed on a SocialCrawl surface carries that vendor and channel identity
        # (opened 4 Sep 2026); youtube keeps its own API identity; surfaces the seed
        # graph cannot attribute to one vendor stay unattributed.
        vendor_family, channel_family = _SEED_GRAPH_IDENTITY.get(p, (None, None))
        creators = set(_opt(r, "creator_ids"))
        creators.add(term) if ct == "creator" else None
        dates = tuple(
            sorted({_date(x) for x in (r.get("trend_date"), r.get("event_date")) if x is not None})
        )
        out.append(
            Observation(
                market=r.get("market"),
                term=term,
                candidate_type=ct,
                row_ids=ids,
                source_families=((channel_family,) if channel_family is not None else ()),
                platforms=(p,),
                observed_dates=dates,
                co_occurring_terms=_opt(r, "co_occur_terms"),
                topic_tags=_opt(r, "topic_groups"),
                near_topics=_opt(r, "near_topics"),
                creator_ids=tuple(creators),
                weight=_num(r.get("row_count", 1), "weight"),
                candidate_score=_num(r.get("candidate_score"), "score", True),
                verified_search_spike=r.get("verified_search_spike", False),
                geo_rejected=r.get("geo_rejected", False),
                vendor_family=vendor_family,
                channel_family=channel_family,
            )
        )
    return tuple(sorted(out, key=_key))


def extract_seed_candidate_observations(rows):
    out = []
    for r in rows:
        ct = _type(r.get("candidate_type"), _CAND)
        cid = _text(r.get("candidate_id"), "stable row receipt")
        ids = _seq(r.get("sample_row_ids"), "stable row receipt", none=False)
        if not ids:
            raise ValueError("stable row receipt")
        if _text(r.get("source"), "source family").lower() != "seed_graph":
            raise ValueError("source family")
        term = (
            _creator(r.get("candidate_value"))
            if ct == "creator"
            else _term(r.get("candidate_value"))
        )
        creators = set(_opt(r, "creator_ids"))
        creators.add(term) if ct == "creator" else None
        out.append(
            Observation(
                market=r.get("market"),
                term=term,
                candidate_type=ct,
                row_ids=tuple(sorted({cid, *ids})),
                source_families=(),
                platforms=("seed_candidates",),
                observed_dates=tuple(_date(x) for x in [r.get("proposed_date")] if x is not None),
                co_occurring_terms=_opt(r, "co_occurring_terms"),
                topic_tags=_opt(r, "evidence_topics"),
                near_topics=_opt(r, "near_topics"),
                creator_ids=tuple(creators),
                weight=_num(r.get("row_count", 1), "weight"),
                candidate_score=_num(r.get("score"), "score", True),
                verified_search_spike=r.get("verified_search_spike", False),
                geo_rejected=r.get("geo_rejected", False),
            )
        )
    return tuple(sorted(out, key=_key))


def _generic(t):
    x = t.split()
    return bool(x) and ((len(x) == 1 and t in _GENERIC) or all(y in _GENERIC for y in x))


def _canonicalize_observations_with_mapping(observations, *, aliases=None):
    items = tuple(observations)
    identity_by_index = [None] * len(items)
    am = {}
    for s, t in (aliases or {}).items():
        if (
            isinstance(s, str)
            and s.strip()
            and t is not None
            and str(t).strip()
            and not _generic(_term(s))
        ):
            am[_term(s)] = _term(t)
    for i in items:
        for a in i.aliases:
            if not _generic(a):
                am.setdefault(a, i.term)
    groups = {}
    for index, i in enumerate(items):
        t = i.term
        seen = set()
        while t in am and t not in seen:
            seen.add(t)
            t = am[t]
        if (
            (i.candidate_type in {"entity", "headline"} and _generic(t))
            or i.geo_rejected
            or any(text_matches_geo_blocklist(t, x) for x in i.topic_tags)
        ):
            continue
        identity_by_index[index] = f"{i.market}|{i.candidate_type}|{t}"
        b = groups.setdefault(
            (i.market, t, i.candidate_type),
            {
                "rows": set(),
                "fams": set(),
                "vendors": set(),
                "channels": set(),
                "plats": set(),
                "aliases": set(),
                "dates": set(),
                "times": set(),
                "co": set(),
                "topics": set(),
                "near": set(),
                "creators": set(),
                "weight": 0.0,
                "scores": [],
                "spike": False,
            },
        )
        for k, v in (
            ("rows", i.row_ids),
            ("fams", i.source_families),
            ("plats", i.platforms),
            ("dates", i.observed_dates),
            ("times", i.observed_timestamps),
            ("co", i.co_occurring_terms),
            ("topics", i.topic_tags),
            ("near", i.near_topics),
            ("creators", i.creator_ids),
        ):
            b[k].update(v)
        b["aliases"].update(a for a in i.aliases if a != t)
        if i.vendor_family is not None:
            b["vendors"].add(i.vendor_family)
            b["channels"].add(i.channel_family)
        b["weight"] += i.weight
        b["scores"] += [] if i.candidate_score is None else [i.candidate_score]
        b["spike"] |= i.verified_search_spike
    out = []
    for (m, t, ct), b in groups.items():
        rows = tuple(sorted(b["rows"]))
        plats = tuple(sorted(b["plats"]))
        one_source_pair = (
            len(b["vendors"]) == len(b["channels"]) == 1 and b["fams"] == b["channels"]
        )
        vendor_family = next(iter(b["vendors"])) if one_source_pair else None
        channel_family = next(iter(b["channels"])) if one_source_pair else None
        out.append(
            Observation(
                market=m,
                term=t,
                candidate_type=ct,
                row_ids=rows,
                source_families=tuple(b["fams"]),
                platforms=plats,
                aliases=tuple(b["aliases"]),
                observed_dates=tuple(b["dates"]),
                observed_timestamps=tuple(b["times"]),
                co_occurring_terms=tuple(b["co"]),
                topic_tags=tuple(b["topics"]),
                near_topics=tuple(b["near"]),
                creator_ids=tuple(b["creators"]),
                weight=b["weight"],
                candidate_score=max(b["scores"]) if b["scores"] else None,
                verified_search_spike=b["spike"],
                vendor_family=vendor_family,
                channel_family=channel_family,
            )
        )
    return tuple(sorted(out, key=_key)), tuple(identity_by_index)


def canonicalize_observations(observations, *, aliases=None):
    canonical, _ = _canonicalize_observations_with_mapping(observations, aliases=aliases)
    return canonical


def canonicalize_observations_with_mapping(observations, *, aliases=None):
    return _canonicalize_observations_with_mapping(observations, aliases=aliases)
