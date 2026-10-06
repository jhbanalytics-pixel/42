"""Discovery loop seed_candidates ranker (V3 Track C).

Python stage: bounded BQ reads (seed_graph row grain, hot trend_scores,
28-day distinctiveness baseline), aggregate per (market, term), score, filter,
insert. Weekly pool Mondays (7-day window); fast lane daily.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from google.cloud import bigquery as bq

from src.analysis.generate_briefs import (
    _RISK_SHORT_MARKERS,
    _RISK_SIGNALS,
    _RISK_TEXT_MARKERS,
)
from src.utils.bigquery import get_client, get_dataset
from src.utils.geo_blocklist import has_foreign_latin_density, has_non_ssa_script
from src.utils.language_guard import is_hard_foreign_text

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_MARKETS = ("za", "ng", "ke")

VISUAL_AUDIO_PLATFORMS = frozenset(
    {"tiktok", "instagram", "threads", "twitter", "youtube", "music"}
)
WEEKLY_CAP = 10
FAST_WEEK_CAP = 5
WEEK_TOTAL_CAP = 15
NOVELTY_DAYS_WEEKLY = 7
NOVELTY_DAYS_FAST = 14
REJECTION_MEMORY_DAYS = 28
MIN_FREQUENCY_WEEKLY = 5
MIN_FREQUENCY_FAST = 3
DEFAULT_FAST_FLOOR = 0.45

# Audience-neutral C2 weights. The former audience term controlled candidate
# selection before Open Intelligence could remove it. Its weight is split
# between observed distinctiveness and slang-type specificity. Historical
# avg_genz_score remains readable for legacy audit but cannot enter this score.
# Historical hot-topic co-occurrence remains in the storage shape at zero.
C2_W_CO_OCCUR = 0.00
C2_W_VISUAL_AUDIO = 0.05
C2_W_DISTINCTIVENESS = 0.70
C2_W_SLANG = 0.25


def _day_one_specificity(n_topics: int) -> float:
    """Day-one distinctiveness proxy: a term tied to few topics is more
    distinctive than one sprinkled across many. An orphan term (no topical
    grounding) earns nothing; 1 topic scores 1.0, 6 or more scores 0.0."""
    if n_topics <= 0:
        return 0.0
    return _clamp01(1.0 - (n_topics - 1) / 5.0)


_SEED_GRAPH_COLS = (
    "market",
    "term",
    "term_type",
    "platform",
    "trend_date",
    "event_date",
    "row_count",
    "avg_genz_score",
    "slang_row_share",
    "topic_groups",
    "near_topics",
    "sample_row_ids",
)


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "")
    return (
        "".join(c for c in unicodedata.normalize("NFD", folded) if unicodedata.category(c) != "Mn")
        .lower()
        .strip()
    )


def is_enabled() -> bool:
    return os.environ.get("SEED_CANDIDATES_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def fast_lane_floor(market: str) -> float:
    key = f"SEED_CANDIDATES_FAST_FLOOR_{market.upper()}"
    raw = os.environ.get(key) or os.environ.get("SEED_CANDIDATES_FAST_FLOOR", "")
    if raw:
        try:
            return max(0.35, min(0.65, float(raw)))
        except ValueError:
            pass
    return DEFAULT_FAST_FLOOR


def make_candidate_id(
    market: str,
    candidate_value: str,
    candidate_type: str,
    proposed_date: date,
) -> str:
    key = f"{market}|{candidate_value}|{candidate_type}|{proposed_date.isoformat()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def term_to_candidate_type(term_type: str) -> str:
    if term_type == "slang":
        return "slang"
    return "keyword"


def monday_on_or_before(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _yaml_strings(node: Any, out: set[str]) -> None:
    if isinstance(node, str):
        folded = _fold(node.lstrip("#").strip())
        if folded:
            out.add(folded)
    elif isinstance(node, list):
        for item in node:
            _yaml_strings(item, out)
    elif isinstance(node, dict):
        for val in node.values():
            _yaml_strings(val, out)


def load_config_terms(market: str) -> set[str]:
    """All discovery-config vocabulary for novelty / config-absence checks."""
    mk = market.lower()
    terms: set[str] = set()

    tg_path = _ROOT / "configs" / "topic_groups" / f"{mk}.yaml"
    if tg_path.is_file():
        doc = yaml.safe_load(tg_path.read_text(encoding="utf-8")) or {}
        for group in (doc.get("topic_groups") or {}).values():
            if isinstance(group, dict):
                _yaml_strings(group.get("keywords"), terms)

    kw_path = _ROOT / "configs" / "keywords" / f"{mk}.yaml"
    if kw_path.is_file():
        doc = yaml.safe_load(kw_path.read_text(encoding="utf-8")) or {}
        _yaml_strings(doc.get("topic_groups"), terms)
        _yaml_strings(doc.get("regional_markers"), terms)
        # The default demographic marker list generates the historical audience
        # proxy and nothing else. It is not discovery vocabulary, so it cannot
        # decide which observed terms general seeding treats as already known.
        slang = doc.get("slang") or {}
        if isinstance(slang, dict):
            for block in slang.values():
                if isinstance(block, dict):
                    _yaml_strings(block.get("terms"), terms)
                else:
                    _yaml_strings(block, terms)

    anchor_path = _ROOT / "configs" / "topic_anchors" / f"{mk}.yaml"
    if anchor_path.is_file():
        doc = yaml.safe_load(anchor_path.read_text(encoding="utf-8")) or {}
        anchors = doc.get("topic_anchors") or doc
        _yaml_strings(anchors, terms)

    creators_path = _ROOT / "configs" / "creators" / f"{mk}.yaml"
    if creators_path.is_file():
        doc = yaml.safe_load(creators_path.read_text(encoding="utf-8")) or {}
        watch = doc.get("watchlists") or {}
        for platform_lists in watch.values():
            if isinstance(platform_lists, dict):
                for tier_list in platform_lists.values():
                    if isinstance(tier_list, list):
                        for handle in tier_list:
                            if isinstance(handle, str):
                                terms.add(_fold(handle.lstrip("@")))

    return terms


_STOPLIST_CACHE: dict[str, frozenset[str]] | None = None


def _stoplist_for(market: str) -> frozenset[str]:
    """Market stoplist from seed_graph's loader, cached per process."""
    global _STOPLIST_CACHE
    if _STOPLIST_CACHE is None:
        from src.analysis.seed_graph import _load_stoplist

        _STOPLIST_CACHE = _load_stoplist()
    return _STOPLIST_CACHE.get(market.lower(), _STOPLIST_CACHE["global"])


_COMMON_ENGLISH_PATH = _ROOT / "configs" / "common_english_words.txt"
_COMMON_ENGLISH_CACHE: frozenset[str] | None = None


def _common_english() -> frozenset[str]:
    """Common English words (wordfreq zipf >= 2.8) used to gate generic tokens
    that carry no cultural signal. Cached per process; empty if the file is
    absent so the gate simply no-ops rather than crashing."""
    global _COMMON_ENGLISH_CACHE
    if _COMMON_ENGLISH_CACHE is None:
        words: set[str] = set()
        if _COMMON_ENGLISH_PATH.is_file():
            for line in _COMMON_ENGLISH_PATH.read_text(encoding="utf-8").splitlines():
                w = line.strip()
                if w and not w.startswith("#"):
                    words.add(w)
        _COMMON_ENGLISH_CACHE = frozenset(words)
    return _COMMON_ENGLISH_CACHE


def _seq_field(val):
    if val is None:
        return []
    if isinstance(val, float) and pd.isna(val):
        return []
    if hasattr(val, "tolist"):
        val = val.tolist()
    return list(val)


def aggregate_terms(
    rows: list[dict[str, Any]],
    market: str,
    *,
    first_seen: dict[tuple[str, str], date] | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate seed_graph row-grain rows per term for one market."""
    mk = market.lower()
    first_seen = first_seen or {}
    agg: dict[str, dict[str, Any]] = {}

    for raw in rows:
        if str(raw.get("market") or "").lower() != mk:
            continue
        term = str(raw.get("term") or "").lower()
        if not term:
            continue
        plat = str(raw.get("platform") or "").lower()
        term_type = str(raw.get("term_type") or "token")
        rc = int(raw.get("row_count") or 0)
        genz = raw.get("avg_genz_score")
        slang = raw.get("slang_row_share")
        ev = raw.get("event_date")
        ev_date: date | None = None
        if ev is not None and not (isinstance(ev, float) and pd.isna(ev)):
            if hasattr(ev, "date"):
                d = ev.date()
                ev_date = d() if callable(d) else d
            else:
                try:
                    ev_date = pd.Timestamp(ev).date()
                except (TypeError, ValueError):
                    ev_date = None

        bucket = agg.get(term)
        if bucket is None:
            bucket = {
                "market": mk,
                "term": term,
                "term_types": set(),
                "row_ids": set(),
                "_genz_sum": 0.0,
                "_genz_w": 0,
                "_slang_sum": 0.0,
                "_slang_w": 0,
                "topic_groups": set(),
                "near_topics": set(),
                "platform_counts": {},
                "sample_row_ids": [],
                "first_seen_event_date": None,
            }
            agg[term] = bucket

        bucket["term_types"].add(term_type)
        for rid in _seq_field(raw.get("sample_row_ids")):
            if rid:
                bucket["row_ids"].add(str(rid))
                if len(bucket["sample_row_ids"]) < 5 and str(rid) not in bucket["sample_row_ids"]:
                    bucket["sample_row_ids"].append(str(rid))
        if rc > 0:
            if genz is not None and not (isinstance(genz, float) and pd.isna(genz)):
                bucket["_genz_sum"] += float(genz) * rc
                bucket["_genz_w"] += rc
            if slang is not None and not (isinstance(slang, float) and pd.isna(slang)):
                bucket["_slang_sum"] += float(slang) * rc
                bucket["_slang_w"] += rc
        for tg in _seq_field(raw.get("topic_groups")):
            if tg:
                bucket["topic_groups"].add(str(tg))
        for nt in _seq_field(raw.get("near_topics")):
            if nt:
                bucket["near_topics"].add(str(nt))
        bucket["platform_counts"][plat] = bucket["platform_counts"].get(plat, 0) + rc

        fs_key = (mk, term)
        fs = first_seen.get(fs_key)
        if fs is not None and pd.isna(fs):
            fs = None
        if fs is None and ev_date:
            fs = ev_date
        if fs is not None and not pd.isna(fs):
            cur = bucket["first_seen_event_date"]
            bucket["first_seen_event_date"] = fs if cur is None else min(cur, fs)

    for _term, bucket in agg.items():
        bucket["frequency"] = len(bucket["row_ids"])
        bucket["avg_genz_score"] = (
            bucket["_genz_sum"] / bucket["_genz_w"] if bucket["_genz_w"] else 0.0
        )
        bucket["slang_row_share"] = (
            bucket["_slang_sum"] / bucket["_slang_w"] if bucket["_slang_w"] else 0.0
        )
        bucket["candidate_type"] = term_to_candidate_type(
            "slang" if "slang" in bucket["term_types"] else "token"
        )
        del bucket["_genz_sum"]
        del bucket["_genz_w"]
        del bucket["_slang_sum"]
        del bucket["_slang_w"]
        del bucket["term_types"]
        del bucket["row_ids"]

    return agg


def _clamp01(val: float) -> float:
    return max(0.0, min(1.0, val))


def compute_c2_score(
    term_agg: dict[str, Any],
    hot_topics: set[str],
    baseline: dict[str, float],
) -> tuple[float, dict[str, float]]:
    topics = set(term_agg.get("topic_groups") or [])
    near = set(term_agg.get("near_topics") or [])
    co_occur = 0.0

    plat_counts = term_agg.get("platform_counts") or {}
    total = sum(plat_counts.values()) or 1
    va_num = sum(plat_counts.get(p, 0) for p in VISUAL_AUDIO_PLATFORMS)
    visual_audio = _clamp01(va_num / total)

    term = str(term_agg.get("term") or "")
    freq = int(term_agg.get("frequency") or 0)
    base = baseline.get(term)
    # Distinctiveness. A real above-baseline spike (freq / 28-day baseline) is
    # the mature signal. Day one there is no baseline, so fall back to topical
    # specificity: a term tied to few topics is more distinctive than a generic
    # word sprinkled across many. This directly counters co_occur, which
    # rewarded breadth and so floated generic filler (vibe, tech) to the top.
    breadth = len(topics | near)
    distinctiveness = _clamp01(freq / base) if base and base > 0 else _day_one_specificity(breadth)

    # slang_row_share measures the slang density of the posts a term appears
    # in, not the term itself; only slang-type terms may claim it, else a
    # generic token riding slang-heavy posts inherits an unearned slang score.
    is_slang_term = str(term_agg.get("candidate_type") or "") == "slang"
    slang = _clamp01(float(term_agg.get("slang_row_share") or 0.0)) if is_slang_term else 0.0
    score = _clamp01(
        C2_W_CO_OCCUR * co_occur
        + C2_W_VISUAL_AUDIO * visual_audio
        + C2_W_DISTINCTIVENESS * distinctiveness
        + C2_W_SLANG * slang
    )
    # distinctiveness is stripped before persist (the BQ seed_fit STRUCT has no
    # field for it yet) but feeds the human-readable rationale so the full
    # arithmetic behind every score is defensible from the stored row.
    seed_fit = {
        "genz": 0.0,
        "slang": slang,
        "visual_audio": visual_audio,
        "co_occur": co_occur,
        "distinctiveness": distinctiveness,
    }
    return score, seed_fit


def score_rationale(score: float, seed_fit: dict[str, float]) -> str:
    """One-line arithmetic receipt for a pending candidate."""
    co = seed_fit.get("co_occur", 0.0)
    va = seed_fit.get("visual_audio", 0.0)
    dist = seed_fit.get("distinctiveness", 0.0)
    slang = _clamp01(seed_fit.get("slang", 0.0))
    return (
        f"score {score:.2f} = co_occur {co:.2f}x{C2_W_CO_OCCUR}"
        f" + visual_audio {va:.2f}x{C2_W_VISUAL_AUDIO}"
        f" + distinctiveness {dist:.2f}x{C2_W_DISTINCTIVENESS}"
        f" + slang {slang:.2f}x{C2_W_SLANG}"
    )


def check_safety(term_agg: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    term = str(term_agg.get("term") or "")
    hay = term
    topics = " ".join(term_agg.get("topic_groups") or [])
    hay_full = f"{term} {topics}"

    if has_non_ssa_script(hay_full) or has_foreign_latin_density(hay_full):
        flags.append("foreign_script")
    if is_hard_foreign_text(hay_full):
        flags.append("langdetect_foreign")

    for marker, _note in _RISK_TEXT_MARKERS:
        if marker in _RISK_SHORT_MARKERS:
            if re.search(rf"\b{re.escape(marker)}\b", hay):
                flags.append(f"risk_marker:{marker}")
        elif marker in hay:
            flags.append(f"risk_marker:{marker}")

    for signal, _note in _RISK_SIGNALS:
        for tg in term_agg.get("topic_groups") or []:
            tg_l = str(tg).lower()
            if signal == tg_l.split("_")[0] or signal in tg_l:
                flags.append(f"risk_signal:{signal}")
                break
        if signal in hay:
            flags.append(f"risk_signal:{signal}")

    return flags


def is_eligible(
    term_agg: dict[str, Any],
    config_terms: set[str],
    rejection_memory: set[str],
    lane: str,
    *,
    cap_remaining: int,
    trend_date: date,
) -> bool:
    if cap_remaining <= 0:
        return False

    term = str(term_agg.get("term") or "").lower()
    if not term or term in config_terms:
        return False
    if term in rejection_memory:
        return False
    # Defense in depth against stale graph rows: the stoplist gates extraction
    # in seed_graph, but the weekly lane reads up to 7 days of history built by
    # older code, so a stoplisted term can still arrive here.
    if term in _stoplist_for(str(term_agg.get("market") or "")):
        return False
    # Same defense for structural junk (bare numbers, phone-number handles,
    # gibberish hashes, GDELT theme codes): the extraction-time quality gate
    # only covers days rebuilt after it shipped, so filter here too.
    from src.analysis.seed_graph import _low_quality_term

    if _low_quality_term(term):
        return False
    # Generic-English gate. On day one there is no distinctiveness baseline, so
    # common English words (zero, ideas, greater) ride co_occur to the top over
    # real SSA signal. genz_score and slang_row_share are properties of the
    # POSTS a term rides, not the term, so a generic word in slang-heavy posts
    # inherits a high slang share and cannot be used as an escape hatch. On day
    # one we therefore drop common English outright; SSA vernacular, neologisms,
    # and entities are not in the list so they are untouched. This is a
    # deliberate stopgap ahead of the C2 reweight once the 28-day baseline
    # exists, at which point signal-based nuance can return.
    if term in _common_english():
        return False

    safety = check_safety(term_agg)
    if safety:
        return False

    freq = int(term_agg.get("frequency") or 0)
    min_freq = MIN_FREQUENCY_WEEKLY if lane == "weekly" else MIN_FREQUENCY_FAST
    if freq < min_freq:
        return False

    fs = term_agg.get("first_seen_event_date")
    if fs is None:
        return False
    if isinstance(fs, str):
        fs = date.fromisoformat(fs)
    novelty_days = NOVELTY_DAYS_WEEKLY if lane == "weekly" else NOVELTY_DAYS_FAST
    return not (trend_date - fs).days > novelty_days


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 1.0
    ordered = sorted(values)
    idx = round((len(ordered) - 1) * pct)
    return ordered[max(0, min(len(ordered) - 1, idx))]


def build_candidate_rows(
    term_agg: dict[str, Any],
    *,
    lane: str,
    proposed_date: date,
    score: float,
    seed_fit: dict[str, float],
    safety_flags: list[str],
    source: str = "seed_graph",
) -> dict[str, Any]:
    market = str(term_agg["market"])
    term = str(term_agg["term"])
    ctype = str(term_agg.get("candidate_type") or "keyword")
    status = "rejected" if safety_flags else "pending"
    now = datetime.now(UTC).isoformat()
    # The BQ seed_fit STRUCT holds four fields; distinctiveness rides in the
    # rationale receipt until a schema migration adds its field.
    persisted_fit = {k: seed_fit.get(k, 0.0) for k in ("genz", "slang", "visual_audio", "co_occur")}
    return {
        "candidate_id": make_candidate_id(market, term, ctype, proposed_date),
        "proposed_date": proposed_date.isoformat(),
        "market": market,
        "candidate_type": ctype,
        "candidate_value": term,
        "source": source,
        "lane": lane,
        "score": score,
        "seed_fit": persisted_fit,
        "safety_flags": safety_flags,
        "evidence_topics": sorted(term_agg.get("topic_groups") or []),
        "sample_row_ids": list(term_agg.get("sample_row_ids") or []),
        "status": status,
        "status_by": "system" if safety_flags else None,
        "status_at": now if safety_flags else None,
        "rationale": "safety_auto_reject" if safety_flags else score_rationale(score, seed_fit),
    }


def fetch_seed_graph_window(
    trend_date: date,
    *,
    window_days: int = 1,
    client: bq.Client | None = None,
    dataset: str | None = None,
) -> list[dict[str, Any]]:
    client = client or get_client()
    dataset = dataset or get_dataset()
    start = trend_date - timedelta(days=window_days - 1)
    cols = ", ".join(_SEED_GRAPH_COLS)
    sql = f"""
    SELECT {cols}
    FROM `{client.project}.{dataset}.seed_graph`
    WHERE trend_date BETWEEN @start AND @end
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("start", "DATE", start),
            bq.ScalarQueryParameter("end", "DATE", trend_date),
        ]
    )
    df = client.query(sql, job_config=job_config).to_dataframe()
    if df is None or df.empty:
        return []
    return df.to_dict("records")


def fetch_distinctiveness_baseline(
    trend_date: date,
    *,
    lookback_days: int = 28,
    client: bq.Client | None = None,
    dataset: str | None = None,
) -> dict[str, dict[str, float]]:
    client = client or get_client()
    dataset = dataset or get_dataset()
    start = trend_date - timedelta(days=lookback_days)
    end = trend_date - timedelta(days=1)
    sql = f"""
    SELECT market, term, AVG(row_count) AS avg_rows
    FROM `{client.project}.{dataset}.seed_graph`
    WHERE trend_date BETWEEN @start AND @end
    GROUP BY market, term
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("start", "DATE", start),
            bq.ScalarQueryParameter("end", "DATE", end),
        ]
    )
    out: dict[str, dict[str, float]] = {m: {} for m in _MARKETS}
    for r in client.query(sql, job_config=job_config).result():
        mk = str(r.market).lower()
        if mk in out:
            out[mk][str(r.term)] = float(r.avg_rows or 0.0)
    return out


def fetch_first_seen_map(
    market: str,
    *,
    client: bq.Client | None = None,
    dataset: str | None = None,
) -> dict[tuple[str, str], date]:
    client = client or get_client()
    dataset = dataset or get_dataset()
    sql = f"""
    SELECT market, term, first_seen_event_date
    FROM `{client.project}.{dataset}.v_seed_first_seen`
    WHERE market = @market
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("market", "STRING", market.lower())]
    )
    out: dict[tuple[str, str], date] = {}
    for r in client.query(sql, job_config=job_config).result():
        fs = r.first_seen_event_date
        if fs is not None and not pd.isna(fs):
            out[(str(r.market).lower(), str(r.term))] = (
                fs if isinstance(fs, date) else pd.Timestamp(fs).date()
            )
    return out


def fetch_rejection_memory(
    market: str,
    as_of: date,
    *,
    client: bq.Client | None = None,
    dataset: str | None = None,
) -> set[str]:
    client = client or get_client()
    dataset = dataset or get_dataset()
    since = as_of - timedelta(days=REJECTION_MEMORY_DAYS)
    sql = f"""
    SELECT DISTINCT candidate_value
    FROM `{client.project}.{dataset}.seed_candidates`
    WHERE market = @market
      AND proposed_date >= @since
      AND status IN ('pending', 'applied', 'rejected')
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("market", "STRING", market.lower()),
            bq.ScalarQueryParameter("since", "DATE", since),
        ]
    )
    return {
        str(r.candidate_value).lower() for r in client.query(sql, job_config=job_config).result()
    }


def fetch_week_proposed_counts(
    week_start: date,
    *,
    client: bq.Client | None = None,
    dataset: str | None = None,
) -> dict[str, int]:
    client = client or get_client()
    dataset = dataset or get_dataset()
    sql = f"""
    SELECT market, COUNT(*) AS n
    FROM `{client.project}.{dataset}.seed_candidates`
    WHERE proposed_date >= @week_start
    GROUP BY market
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("week_start", "DATE", week_start)]
    )
    out = dict.fromkeys(_MARKETS, 0)
    for r in client.query(sql, job_config=job_config).result():
        mk = str(r.market).lower()
        if mk in out:
            out[mk] = int(r.n)
    return out


def weekly_lane_already_ran(
    proposed_date: date,
    *,
    client: bq.Client | None = None,
    dataset: str | None = None,
) -> bool:
    client = client or get_client()
    dataset = dataset or get_dataset()
    sql = f"""
    SELECT COUNT(*) AS n
    FROM `{client.project}.{dataset}.seed_candidates`
    WHERE proposed_date = @d
      AND lane = 'weekly'
      AND status = 'pending'
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("d", "DATE", proposed_date)]
    )
    row = next(iter(client.query(sql, job_config=job_config).result()), None)
    return bool(row and int(row.n) > 0)


def fetch_existing_candidate_ids(
    proposed_date: date,
    ids: list[str],
    *,
    client: bq.Client | None = None,
    dataset: str | None = None,
) -> set[str]:
    if not ids:
        return set()
    client = client or get_client()
    dataset = dataset or get_dataset()
    sql = f"""
    SELECT candidate_id
    FROM `{client.project}.{dataset}.seed_candidates`
    WHERE proposed_date = @d
      AND candidate_id IN UNNEST(@ids)
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("d", "DATE", proposed_date),
            bq.ArrayQueryParameter("ids", "STRING", ids),
        ]
    )
    return {str(r.candidate_id) for r in client.query(sql, job_config=job_config).result()}


def persist_candidates(
    rows: list[dict[str, Any]],
    *,
    client: bq.Client | None = None,
    dataset: str | None = None,
) -> int:
    if not rows:
        return 0
    client = client if client is not None else get_client()
    dataset = dataset if dataset is not None else get_dataset()
    df = pd.DataFrame(rows)
    if "proposed_date" in df.columns:
        df["proposed_date"] = pd.to_datetime(df["proposed_date"]).dt.date
    table_ref = f"{client.project}.{dataset}.seed_candidates"
    table = client.get_table(table_ref)
    job_config = bq.LoadJobConfig(
        schema=table.schema, write_disposition=bq.WriteDisposition.WRITE_APPEND
    )
    client.load_table_from_dataframe(df, table_ref, job_config=job_config).result()
    return len(rows)


def run_lane(
    trend_date: date,
    lane: str,
    *,
    client: bq.Client | None = None,
    dataset: str | None = None,
    row_sink: Callable[[list[dict[str, Any]]], int] | None = None,
) -> int:
    client = client or get_client()
    dataset = dataset or get_dataset()
    week_start = monday_on_or_before(trend_date)
    week_counts = fetch_week_proposed_counts(week_start, client=client, dataset=dataset)

    window = 7 if lane == "weekly" else 1
    sg_rows = fetch_seed_graph_window(
        trend_date, window_days=window, client=client, dataset=dataset
    )
    if not sg_rows:
        return 0

    baseline_all = fetch_distinctiveness_baseline(trend_date, client=client, dataset=dataset)

    cap_for_lane = WEEKLY_CAP if lane == "weekly" else FAST_WEEK_CAP
    inserted = 0

    for market in _MARKETS:
        week_used = week_counts.get(market, 0)
        if week_used >= WEEK_TOTAL_CAP:
            continue
        lane_cap = min(cap_for_lane, WEEK_TOTAL_CAP - week_used)

        fs_map = fetch_first_seen_map(market, client=client, dataset=dataset)
        rejection = fetch_rejection_memory(market, trend_date, client=client, dataset=dataset)
        config_terms = load_config_terms(market)
        terms = aggregate_terms(sg_rows, market, first_seen=fs_map)
        base = baseline_all.get(market, {})

        scored: list[tuple[float, dict[str, Any], dict[str, float], list[str]]] = []
        eligible_scores: list[float] = []

        for term_agg in terms.values():
            safety = check_safety(term_agg)
            if safety:
                continue
            if not is_eligible(
                term_agg,
                config_terms,
                rejection,
                lane,
                cap_remaining=lane_cap,
                trend_date=trend_date,
            ):
                continue
            score, seed_fit = compute_c2_score(term_agg, set(), base)
            eligible_scores.append(score)
            scored.append((score, term_agg, seed_fit, safety))

        if lane == "fast":
            gate = max(_percentile(eligible_scores, 0.9), fast_lane_floor(market))
            scored = [s for s in scored if s[0] >= gate]

        scored.sort(key=lambda x: (-x[0], x[1]["term"]))
        proposed: list[dict[str, Any]] = []
        for score, term_agg, seed_fit, safety in scored[:lane_cap]:
            proposed.append(
                build_candidate_rows(
                    term_agg,
                    lane=lane,
                    proposed_date=trend_date,
                    score=score,
                    seed_fit=seed_fit,
                    safety_flags=safety,
                )
            )

        if not proposed:
            continue

        ids = [r["candidate_id"] for r in proposed]
        existing = fetch_existing_candidate_ids(trend_date, ids, client=client, dataset=dataset)
        fresh = [r for r in proposed if r["candidate_id"] not in existing]
        if fresh:
            if row_sink is None:
                n = persist_candidates(fresh, client=client, dataset=dataset)
            else:
                n = row_sink(fresh)
                if type(n) is not int or n != len(fresh):
                    raise ValueError("seed_candidates row sink incomplete")
            inserted += n
            week_counts[market] = week_counts.get(market, 0) + n

    return inserted


def run_seed_candidates_stage(
    trend_date: date,
    *,
    lane: str | None = None,
    client: bq.Client | None = None,
    dataset: str | None = None,
    row_sink: Callable[[list[dict[str, Any]]], int] | None = None,
) -> int:
    """Daily stage: weekly pool on Mondays (first), then fast lane."""
    if isinstance(trend_date, str):
        trend_date = date.fromisoformat(trend_date)

    read_options = {}
    if client is not None:
        read_options["client"] = client
    if dataset is not None:
        read_options["dataset"] = dataset
    lane_options = dict(read_options)
    if row_sink is not None:
        lane_options["row_sink"] = row_sink

    total = 0
    if lane in (None, "weekly"):
        if trend_date.weekday() == 0:
            if not weekly_lane_already_ran(trend_date, **read_options):
                total += run_lane(trend_date, "weekly", **lane_options)
            else:
                logger.info(
                    "seed_candidates weekly skip: pending weekly rows exist for %s", trend_date
                )
        elif lane == "weekly":
            return 0

    if lane in (None, "fast"):
        total += run_lane(trend_date, "fast", **lane_options)

    logger.info("seed_candidates: inserted %d rows for %s", total, trend_date)
    return total
