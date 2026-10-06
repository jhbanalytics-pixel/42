"""Discovery loop seed_graph builder (V3 Track A2).

Extracts slang, hashtags, and tokens from enriched_content rows for one
market/day, aggregates per (term, term_type, platform), and persists with a
whole-trend_date delete+insert (mirrors event_ledger daily rebuild).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from google.cloud import bigquery as bq

from src.scoring.driving_hashtags import _PREFIX_RE, DEFAULT_STOPLIST
from src.utils.bigquery import get_client, get_dataset
from src.utils.geo_blocklist import (
    has_foreign_latin_density,
    has_non_ssa_script,
    row_matches_geo_blocklist,
)
from src.utils.language_guard import is_hard_foreign_text
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_STOPLIST_PATH = _ROOT / "configs" / "seed_graph_stoplist.yaml"

HASHTAG_RE = re.compile(r"#([a-z]\w{2,29})", re.IGNORECASE)
MENTION_RE = re.compile(r"@([a-zA-Z0-9_]{2,30})")
TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)
VALID_TERM_CHARS = re.compile(r"^[a-z0-9_]+$")

BRAND24_SYNTHETIC = frozenset({"link", "author", "aggregate"})
NEAR_MISS_LOW = 0.50
NEAR_MISS_HIGH = 0.65
TERM_CAP_PER_MARKET = 5000
TOP_CO_OCCUR = 10
MAX_SAMPLE_IDS = 5
MAX_CO_OCCUR_KEYS = 64
MAX_CO_OCCUR_ROW_TERMS = 40

_RISK_TEXT_MARKERS = (
    "maandamano",
    "xenophob",
    "afrophob",
    "fraud",
    "scandal",
    "died",
    "death",
)
_RISK_SHORT_MARKERS = frozenset({"died", "death", "fraud"})

ENRICHED_COLUMNS = (
    "id",
    "market",
    "platform",
    "source",
    "title",
    "text",
    "slang_terms",
    "topic_groups",
    "genz_score",
    "slang_score",
    "near_topic",
    "near_cosine",
    "published_at",
    "collected_at",
    "content_type",
    "author_handle",
    "v2gcam",
)


def _load_stoplist() -> dict[str, frozenset[str]]:
    raw: dict[str, list[str]] = {"global": list(DEFAULT_STOPLIST)}
    if _STOPLIST_PATH.is_file():
        loaded = yaml.safe_load(_STOPLIST_PATH.read_text(encoding="utf-8")) or {}
        for key, vals in loaded.items():
            if isinstance(vals, list):
                raw[key] = [str(v).lower() for v in vals]
    out: dict[str, frozenset[str]] = {}
    for mk in ("global", "za", "ng", "ke"):
        merged = set(raw.get("global", []))
        merged.update(raw.get(mk, []))
        out[mk] = frozenset(merged)
    return out


def normalize_seed_platform(source: str, platform: str, content_type: str = "") -> str | None:
    """Map raw connector platform to the seed_graph platform axis."""
    src = (source or "").strip().lower()
    plat = (platform or "").strip().lower()
    ctype = (content_type or "").strip().lower()

    if src == "brand24":
        if plat in BRAND24_SYNTHETIC or ctype in BRAND24_SYNTHETIC:
            return None
        if plat in {"web", "news", "podcast", "linkedin"}:
            return "web"
        if plat in {"facebook", "tiktok", "instagram", "twitter", "youtube", "threads", "reddit"}:
            return plat
        return "web"

    if plat == "web" and (src == "rss" or ctype == "article"):
        return "news"
    if src == "gdelt" or plat == "news":
        return "news"
    if src == "bigquery_trends" or plat == "google_search":
        return "search"
    if src == "apple_music" or plat == "music":
        return "music"
    if src == "reddit" or plat == "reddit":
        return "reddit"
    if src == "youtube" or plat == "youtube":
        return "youtube"
    if src in {"ensembledata", "ensemble"} or plat in {
        "tiktok",
        "instagram",
        "threads",
        "youtube",
        "twitter",
    }:
        return plat or "other"
    return "other"


def _fold_text(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "")
    return "".join(
        c for c in unicodedata.normalize("NFD", folded) if unicodedata.category(c) != "Mn"
    ).lower()


def _risk_hit(term: str) -> bool:
    t = term.lower()
    for marker in _RISK_TEXT_MARKERS:
        if marker in _RISK_SHORT_MARKERS:
            if re.search(rf"\b{re.escape(marker)}\b", t):
                return True
        elif marker in t:
            return True
    return False


def _mention_drop_set(text: str) -> set[str]:
    return {_fold_text(m) for m in MENTION_RE.findall(text or "")}


# GDELT V2Themes dump detector. Some GKG-enriched news rows (source is the
# outlet, not 'gdelt') carry a raw theme string as their text, e.g.
# "CRIME_COMMON_ROBBERY,80 TAX_FNCACT_ROBBERS,80 WB_696_PUBLIC_SECTOR...,120".
# The is_gdelt token guard misses them because the source is the outlet name,
# so the codes were tokenised into wb_/crisislex_/tax_fncact_ terms.
_GDELT_THEME_PAIR_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+,\d+")
_GDELT_TERM_PREFIX_RE = re.compile(
    r"^(?:wb_\d|epu_|crisislex_|ungp_|tax_fncact_|tax_econ_|tax_ethnicity_"
    r"|uspec_|soc_pointsofinterest|manmade_disaster|natural_disaster"
    r"|general_government|security_services|worldlanguages)"
)
_VOWELS = frozenset("aeiou")


def _looks_like_gdelt_theme_dump(text: str) -> bool:
    """True when text is a GDELT V2Themes code dump, not human content."""
    return len(_GDELT_THEME_PAIR_RE.findall(text or "")) >= 3


# Reddit is ingested from r/all keyword search, which pulls foreign-community
# posts the row-level guard misses. Two escape routes are closed here:
#  1. Foreign-LANGUAGE subs (r/FlipTop is Tagalog): the global guard excludes
#     tl / id / ms because TikTok Pidgin collides with them, but SSA Reddit is
#     written in standard English, so those codes are safe to treat as foreign
#     for reddit-source rows.
#  2. Foreign-SUBREDDIT posts written in English (r/IndianFestivals): no
#     language check can catch English prose, so the [r/sub] prefix the reddit
#     connector prepends is the only signal. The set mirrors the connector's
#     keyword_search_exclude_subreddits plus the community subs seen leaking.
_REDDIT_EXTRA_FOREIGN_LANGS = frozenset({"tl", "id", "ms"})
_REDDIT_SUB_RE = re.compile(r"\[r/([A-Za-z0-9_]+)\]", re.IGNORECASE)
_FOREIGN_REDDIT_SUBS = frozenset(
    {
        "fliptop",
        "indianfestivals",
        "indianfestival",
        "hinduism",
        "ankara",
        "askturkey",
        "askmiddleeast",
        "turkey",
        "istanbul",
        "offmychestph",
        "dramarush",
        "cdramasfans",
        "cdrama",
        "donghua",
        "puer",
        "tantrasadhaks",
        "borsavefon",
        "hotwheelssatistakastr",
    }
)


def _foreign_reddit_sub(text: str) -> bool:
    """True when the [r/sub] prefix names a known non-SSA subreddit."""
    m = _REDDIT_SUB_RE.search(text or "")
    return bool(m) and m.group(1).lower() in _FOREIGN_REDDIT_SUBS


def _low_quality_term(term: str) -> bool:
    """Reject structural junk a stoplist cannot enumerate: bare numbers, phone
    numbers and other digit-heavy ids, over-long tokens, low-vowel gibberish
    hashes, and GDELT theme codes. Kept deliberately permissive on short SSA
    acronyms (nsfas, mpesa) so real signal survives."""
    t = term or ""
    n = len(t)
    if n < 4:
        return False
    if _GDELT_TERM_PREFIX_RE.match(t):
        return True
    digits = sum(c.isdigit() for c in t)
    if digits == n:  # bare number or year
        return True
    if digits and re.search(r"\d{4,}", t):  # phone number or embedded id run
        return True
    if digits and digits / n >= 0.35:
        return True
    if n > 20:  # no real trend term is this long
        return True
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 8:
        vowel_ratio = sum(c in _VOWELS for c in letters) / len(letters)
        if vowel_ratio < 0.2:  # consonant-heavy hash, not a word
            return True
    return False


def _extract_hashtags(text: str, drop: set[str], stoplist: frozenset[str]) -> set[str]:
    out: set[str] = set()
    for match in HASHTAG_RE.findall(text or ""):
        tag = match.lower()
        if not VALID_TERM_CHARS.match(tag) or tag.isdigit():
            continue
        if tag in drop or tag in stoplist or _risk_hit(tag) or _low_quality_term(tag):
            continue
        out.add(tag)
    return out


def _extract_slang(slang_terms: str, stoplist: frozenset[str]) -> set[str]:
    out: set[str] = set()
    for part in (slang_terms or "").split(","):
        term = _fold_text(part.strip())
        if len(term) < 2 or term in stoplist or _risk_hit(term):
            continue
        out.add(term)
    return out


def _extract_tokens(text: str, drop: set[str], stoplist: frozenset[str]) -> set[str]:
    cleaned = _PREFIX_RE.sub("", text or "")
    out: set[str] = set()
    for raw in TOKEN_RE.findall(cleaned):
        term = _fold_text(raw)
        if len(term) < 4 or term in drop or term in stoplist or _risk_hit(term):
            continue
        if not VALID_TERM_CHARS.match(term) or _low_quality_term(term):
            continue
        out.add(term)
    return out


def _is_nullish(val: Any) -> bool:
    if val is None:
        return True
    try:
        if pd.isna(val):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _seq_or_empty(val: Any) -> list[Any]:
    """Normalise BQ ARRAY cells (None, pd.NA, [], numpy) to a plain string list."""
    if _is_nullish(val):
        return []
    if isinstance(val, (list, tuple)):
        return [x for x in val if not _is_nullish(x) and str(x).strip()]
    if hasattr(val, "tolist"):
        try:
            items = val.tolist()
        except (TypeError, ValueError, AttributeError):
            return []
        if not isinstance(items, list):
            items = [items]
        return [x for x in items if not _is_nullish(x) and str(x).strip()]
    s = str(val).strip()
    return [val] if s else []


def _row_safe_for_extraction(row: dict[str, Any], *, token_source: bool) -> bool:
    topics = _seq_or_empty(row.get("topic_groups"))
    if topics and any(row_matches_geo_blocklist(row, str(tg)) for tg in topics):
        return False
    if not token_source:
        return True
    hay = f"{row.get('title') or ''} {row.get('text') or ''}"
    if _looks_like_gdelt_theme_dump(hay):
        return False
    if has_non_ssa_script(hay) or has_foreign_latin_density(hay) or is_hard_foreign_text(hay):
        return False
    # Reddit r/all keyword search leaks foreign communities. Apply a stricter
    # language set (SSA reddit is English) plus a foreign-subreddit name check
    # for the English-language foreign subs no language check can catch.
    return not (
        (
            "reddit" in str(row.get("source") or "").lower()
            or normalize_seed_platform(
                str(row.get("source") or ""),
                str(row.get("platform") or ""),
                str(row.get("content_type") or ""),
            )
            == "reddit"
        )
        and (
            _foreign_reddit_sub(hay)
            or is_hard_foreign_text(hay, extra_langs=_REDDIT_EXTRA_FOREIGN_LANGS)
        )
    )


def _is_brand24_synthetic(row: dict[str, Any]) -> bool:
    src = str(row.get("source") or "").lower()
    if src != "brand24":
        return False
    plat = str(row.get("platform") or "").lower()
    ctype = str(row.get("content_type") or "").lower()
    return plat in BRAND24_SYNTHETIC or ctype in BRAND24_SYNTHETIC


def _event_date(row: dict[str, Any]) -> date | None:
    pub = row.get("published_at")
    coll = row.get("collected_at")
    for val in (pub, coll):
        if _is_nullish(val):
            continue
        if hasattr(val, "date"):
            d = val.date()
            return d() if callable(d) else d
        try:
            ts = pd.Timestamp(val)
            if pd.isna(ts):
                continue
            return ts.date()
        except (TypeError, ValueError):
            continue
    return None


def _near_topic_for_row(row: dict[str, Any]) -> str | None:
    nt = row.get("near_topic")
    nc = row.get("near_cosine")
    if nt is None or nc is None or (isinstance(nc, float) and pd.isna(nc)):
        return None
    try:
        score = float(nc)
    except (TypeError, ValueError):
        return None
    if NEAR_MISS_LOW <= score < NEAR_MISS_HIGH:
        return str(nt)
    return None


_STRUCTURED_CAPTURE_PREFIX = "__sc__:"


def _structured_terms_from_row(row: dict[str, Any]) -> set[tuple[str, str]]:
    """Parse ensemble A8 side-channel payloads stored in v2gcam."""
    raw = str(row.get("v2gcam") or "")
    if not raw.startswith(_STRUCTURED_CAPTURE_PREFIX):
        return set()
    try:
        items = json.loads(raw[len(_STRUCTURED_CAPTURE_PREFIX) :])
    except (json.JSONDecodeError, TypeError, ValueError):
        return set()
    out: set[tuple[str, str]] = set()
    if not isinstance(items, list):
        return out
    type_map = {"handle": "handle", "music": "music", "channel": "channel"}
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = type_map.get(str(item.get("type") or ""))
        val = str(item.get("value") or "").strip().lower()
        if kind and val:
            out.add((val, kind))
    return out


def build_seed_graph_rows(
    df: pd.DataFrame,
    market: str,
    trend_date: date,
    *,
    stoplists: dict[str, frozenset[str]] | None = None,
) -> list[dict[str, Any]]:
    """Pure builder: enriched rows in, seed_graph row dicts out."""
    if df is None or df.empty:
        return []

    stoplists = stoplists or _load_stoplist()
    stoplist = stoplists.get(market, stoplists["global"])
    mk = market.lower()

    row_terms: list[tuple[str, set[tuple[str, str]], dict[str, Any], str]] = []
    agg: dict[tuple[str, str, str], dict[str, Any]] = {}

    for _, raw in df.iterrows():
        row = {k: raw.get(k) for k in ENRICHED_COLUMNS if k in raw.index}
        if str(row.get("market") or "").lower() != mk:
            continue
        if _is_brand24_synthetic(row):
            continue

        plat = normalize_seed_platform(
            str(row.get("source") or ""),
            str(row.get("platform") or ""),
            str(row.get("content_type") or ""),
        )
        if plat is None or plat == "other":
            continue

        title = str(row.get("title") or "")
        body = str(row.get("text") or "")
        combined = f"{title} {body}".strip()
        drop = _mention_drop_set(combined)

        is_gdelt = str(row.get("source") or "").lower() == "gdelt"
        token_ok = not is_gdelt and _row_safe_for_extraction(row, token_source=True)
        presence_ok = _row_safe_for_extraction(row, token_source=False)

        terms_on_row: set[tuple[str, str]] = set()
        if presence_ok:
            for t in _extract_slang(str(row.get("slang_terms") or ""), stoplist):
                terms_on_row.add((t, "slang"))
            for t in _extract_hashtags(combined.lower(), drop, stoplist):
                terms_on_row.add((t, "hashtag"))
        if token_ok:
            for t in _extract_tokens(combined, drop, stoplist):
                terms_on_row.add((t, "token"))
        handle = str(row.get("author_handle") or "").strip().lstrip("@").lower()
        if (
            handle
            and presence_ok
            and handle not in drop
            and len(handle) >= 2
            and not _low_quality_term(handle)
        ):
            terms_on_row.add((handle, "handle"))
        if presence_ok:
            terms_on_row.update(_structured_terms_from_row(row))

        if not terms_on_row:
            continue

        row_terms.append((str(row.get("id") or ""), terms_on_row, row, plat))
        topics = [str(t) for t in _seq_or_empty(row.get("topic_groups"))]
        near_t = _near_topic_for_row(row)
        genz = row.get("genz_score")
        genz_f = float(genz) if genz is not None and not pd.isna(genz) else None
        slang_s = row.get("slang_score")
        slang_pos = slang_s is not None and not pd.isna(slang_s) and float(slang_s) > 0
        ev = _event_date(row)

        for term, term_type in terms_on_row:
            key = (term, term_type, plat)
            bucket = agg.get(key)
            if bucket is None:
                bucket = {
                    "market": mk,
                    "term": term,
                    "term_type": term_type,
                    "platform": plat,
                    "trend_date": trend_date.isoformat(),
                    "row_count": 0,
                    "_genz_sum": 0.0,
                    "_genz_n": 0,
                    "_slang_rows": 0,
                    "topic_groups": set(),
                    "near_topics": set(),
                    "co_occur_terms": defaultdict(int),
                    "sample_row_ids": [],
                    "_event_dates": [],
                }
                agg[key] = bucket
            bucket["row_count"] += 1
            if genz_f is not None:
                bucket["_genz_sum"] += genz_f
                bucket["_genz_n"] += 1
            if slang_pos:
                bucket["_slang_rows"] += 1
            bucket["topic_groups"].update(topics)
            if near_t:
                bucket["near_topics"].add(near_t)
            if ev is not None and not pd.isna(ev):
                bucket["_event_dates"].append(ev)
            rid = str(row.get("id") or "")
            if (
                rid
                and len(bucket["sample_row_ids"]) < MAX_SAMPLE_IDS
                and rid not in bucket["sample_row_ids"]
            ):
                bucket["sample_row_ids"].append(rid)

    for _rid, terms_on_row, _row, plat in row_terms:
        term_names = {t for t, _ in terms_on_row}
        if len(term_names) > MAX_CO_OCCUR_ROW_TERMS:
            continue
        for term, term_type in terms_on_row:
            key = (term, term_type, plat)
            if key not in agg:
                continue
            counter = agg[key]["co_occur_terms"]
            for other in term_names:
                if other != term:
                    counter[other] += 1
            if len(counter) > MAX_CO_OCCUR_KEYS:
                trimmed = sorted(counter.items(), key=lambda x: (-x[1], x[0]))[:MAX_CO_OCCUR_KEYS]
                counter.clear()
                counter.update(trimmed)

    generated_at = datetime.now(UTC).isoformat()
    rows_out: list[dict[str, Any]] = []
    for (_term, _tt, _plat), bucket in agg.items():
        co_sorted = sorted(bucket["co_occur_terms"].items(), key=lambda x: (-x[1], x[0]))[
            :TOP_CO_OCCUR
        ]
        genz_n = bucket["_genz_n"]
        ev_dates = [d for d in bucket["_event_dates"] if d is not None and not pd.isna(d)]
        rows_out.append(
            {
                "market": bucket["market"],
                "term": bucket["term"],
                "term_type": bucket["term_type"],
                "platform": bucket["platform"],
                "trend_date": bucket["trend_date"],
                "event_date": min(ev_dates).isoformat() if ev_dates else None,
                "row_count": bucket["row_count"],
                "avg_genz_score": (bucket["_genz_sum"] / genz_n) if genz_n else None,
                "slang_row_share": (
                    bucket["_slang_rows"] / bucket["row_count"] if bucket["row_count"] else 0.0
                ),
                "topic_groups": sorted(bucket["topic_groups"]),
                "near_topics": sorted(bucket["near_topics"]),
                "co_occur_terms": [t for t, _ in co_sorted],
                "sample_row_ids": bucket["sample_row_ids"],
                "generated_at": generated_at,
            }
        )

    if len(rows_out) > TERM_CAP_PER_MARKET:
        rows_out.sort(key=lambda r: (-r["row_count"], r["term"], r["term_type"], r["platform"]))
        rows_out = rows_out[:TERM_CAP_PER_MARKET]

    return rows_out


def _seed_graph_load_schema() -> list[bq.SchemaField]:
    return [
        bq.SchemaField("market", "STRING", mode="REQUIRED"),
        bq.SchemaField("term", "STRING", mode="REQUIRED"),
        bq.SchemaField("term_type", "STRING", mode="REQUIRED"),
        bq.SchemaField("platform", "STRING", mode="REQUIRED"),
        bq.SchemaField("trend_date", "DATE", mode="REQUIRED"),
        bq.SchemaField("event_date", "DATE"),
        bq.SchemaField("row_count", "INT64"),
        bq.SchemaField("avg_genz_score", "FLOAT64"),
        bq.SchemaField("slang_row_share", "FLOAT64"),
        bq.SchemaField("topic_groups", "STRING", mode="REPEATED"),
        bq.SchemaField("near_topics", "STRING", mode="REPEATED"),
        bq.SchemaField("co_occur_terms", "STRING", mode="REPEATED"),
        bq.SchemaField("sample_row_ids", "STRING", mode="REPEATED"),
        bq.SchemaField("generated_at", "TIMESTAMP"),
    ]


def fetch_enriched_for_seed_graph(
    trend_date: date,
    *,
    client: bq.Client | None = None,
    dataset: str | None = None,
) -> pd.DataFrame:
    """BQ read for one trend_date partition (all markets)."""
    client = client or get_client()
    dataset = dataset or get_dataset()
    cols = ", ".join(ENRICHED_COLUMNS)
    sql = f"""
    SELECT {cols}
    FROM `{client.project}.{dataset}.enriched_content`
    WHERE DATE(collected_at) = @trend_date
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
    )
    return client.query(sql, job_config=job_config).to_dataframe(create_bqstorage_client=False)


def _delete_today_seed_graph(client: bq.Client, dataset: str, trend_date: date) -> None:
    sql = f"""
    DELETE FROM `{client.project}.{dataset}.seed_graph`
    WHERE trend_date = @trend_date
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
    )
    client.query(sql, job_config=job_config).result()


def _json_safe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        ga = r.get("generated_at")
        if hasattr(ga, "isoformat"):
            r["generated_at"] = ga.isoformat()
        ed = r.get("event_date")
        if hasattr(ed, "isoformat"):
            r["event_date"] = ed.isoformat()
        out.append(r)
    return out


def persist_seed_graph(
    rows: list[dict[str, Any]],
    trend_date: date,
    *,
    client: bq.Client | None = None,
    dataset: str | None = None,
) -> int:
    """Replace one trend_date partition then insert (idempotent rerun).

    Uses a partition load with WRITE_TRUNCATE so reruns work even when prior
    rows were streamed in (DELETE would hit the streaming buffer).
    """
    client = client if client is not None else get_client()
    dataset = dataset if dataset is not None else get_dataset()
    partition_id = trend_date.strftime("%Y%m%d")
    table_ref = f"{client.project}.{dataset}.seed_graph${partition_id}"
    payload = _json_safe_rows(rows)
    ndjson = "\n".join(json.dumps(r) for r in payload).encode("utf-8")
    job_config = bq.LoadJobConfig(
        source_format=bq.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bq.WriteDisposition.WRITE_TRUNCATE,
        schema=_seed_graph_load_schema(),
    )
    client.load_table_from_file(BytesIO(ndjson), table_ref, job_config=job_config).result()
    return len(payload)


def build_and_persist_seed_graph(
    trend_date: date,
    *,
    client: bq.Client | None = None,
    dataset: str | None = None,
    row_sink: Callable[[list[dict[str, Any]]], int] | None = None,
) -> dict[str, int]:
    """Build all three markets and persist in one delete+insert pass."""
    try:
        df = fetch_enriched_for_seed_graph(trend_date, client=client, dataset=dataset)
        if client is not None or dataset is not None or row_sink is not None:
            missing = set(ENRICHED_COLUMNS) - set(df.columns)
            if missing:
                raise ValueError(f"seed_graph source_columns missing: {','.join(sorted(missing))}")
        all_rows: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for market in ("za", "ng", "ke"):
            built = build_seed_graph_rows(df, market, trend_date)
            counts[market] = len(built)
            all_rows.extend(built)
        if row_sink is None:
            persist_seed_graph(all_rows, trend_date, client=client, dataset=dataset)
        else:
            written = row_sink(all_rows)
            if type(written) is not int or written != len(all_rows):
                raise ValueError("seed_graph row sink incomplete")
        logger.info(
            "seed_graph: persisted %d rows for %s (za=%d ng=%d ke=%d)",
            len(all_rows),
            trend_date,
            counts.get("za", 0),
            counts.get("ng", 0),
            counts.get("ke", 0),
        )
        return counts
    except Exception:
        logger.exception("seed_graph build failed for %s", trend_date)
        raise


def is_enabled() -> bool:
    import os

    return os.environ.get("SEED_GRAPH_ENABLED", "false").strip().lower() in ("1", "true", "yes")
