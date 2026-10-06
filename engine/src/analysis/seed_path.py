"""Behaviour path builder from seed_graph (V3 Track A5, dark)."""

from __future__ import annotations

from datetime import date
from typing import Any

from google.cloud import bigquery as bq

from src.utils.bigquery import get_client, get_dataset

MEASURED_MIN_PLATFORMS = 2
MEASURED_MIN_SPAN_DAYS = 2
MEASURED_MIN_ROWS = 10

# Platforms we do not ingest today; named explicitly in coverage_note.
NOT_INGESTED = frozenset({"twitter"})

# Generic geo / breadth / platform-virality terms. These ride raw volume in
# every topic by being common, so ordering the headline by row_count alone
# floats a bare place name ("nairobi", 129 rows) over the topical term
# ("mpesa", 32 rows) for a ke/fintech_mpesa brief. The gate demotes them:
# a topical term always outranks a generic one, and a generic term only
# surfaces as the headline when nothing else is available.
_GENERIC_SEED_TERMS: frozenset[str] = frozenset(
    {
        # SSA capitals + major cities across the three live markets.
        "nairobi",
        "mombasa",
        "kisumu",
        "nakuru",
        "eldoret",
        "thika",
        "lagos",
        "abuja",
        "ibadan",
        "kano",
        "portharcourt",
        "benin",
        "johannesburg",
        "joburg",
        "jozi",
        "capetown",
        "durban",
        "pretoria",
        "soweto",
        "sandton",
        "gauteng",
        "tshwane",
        # Country / region names (also in the seed_graph build stoplist;
        # repeated here so the gate stands alone for older graph rows).
        "nigeria",
        "nigerian",
        "naija",
        "kenya",
        "kenyan",
        "southafrica",
        "southafrican",
        "mzansi",
        "africa",
        "african",
        # Bare compass fragments left when a place name tokenises apart
        # ("south" out of "south africa" was the headline on six za topics).
        "south",
        "north",
        "east",
        "west",
        # Platform + search + virality breadth tokens.
        "google",
        "search",
        "trend",
        "trends",
        "trending",
        "viral",
        "explore",
        "foryou",
        "fyp",
        "tiktok",
        "instagram",
        "twitter",
        "youtube",
        "facebook",
    }
)

# Google-Trends "related queries" scaffolding arrives on the search platform as
# plain tokens ("terms", "ranked", "score", "breakout") and is pure boilerplate,
# never a headline. Dropped from headline candidacy regardless of volume.
_SEARCH_BOILERPLATE: frozenset[str] = frozenset(
    {
        "search",
        "terms",
        "ranked",
        "trends",
        "trend",
        "score",
        "related",
        "queries",
        "query",
        "results",
        "rising",
        "breakout",
        "top",
    }
)

# Headline ranking weights by term_type. Distinctive cultural markers (slang,
# hashtags) outrank body-text tokens and handles, so a real term surfaces over
# a common word that merely rides volume. Mirrors PR #261's cultural-signal-
# over-generic-breadth reweight applied to candidate discovery.
_TERM_TYPE_WEIGHT: dict[str, float] = {
    "slang": 1.0,
    "hashtag": 1.0,
    "handle": 0.35,
    "token": 0.25,  # nosec B105  # term_type name, not a secret
}

# Platform-name mashups ("kenyantiktok", "nairobitiktokers", "satiktok",
# "tiktoksouthafrica") ride the same breadth bias. Match the platform base as
# a substring so prefix, suffix, and pluralised forms are all caught, without
# enumerating every city/market pair.
_PLATFORM_BASES: tuple[str, ...] = (
    "tiktok",
    "instagram",
    "twitter",
    "youtube",
    "facebook",
)


def _is_generic_seed_term(term: str) -> bool:
    """True when a term is a bare place name or breadth/platform token."""
    t = (term or "").lower()
    if t in _GENERIC_SEED_TERMS:
        return True
    return any(base in t and t != base for base in _PLATFORM_BASES)


def build_seed_path(
    market: str,
    topic_group: str,
    trend_date: date,
    *,
    client: bq.Client | None = None,
    dataset: str | None = None,
) -> dict[str, Any]:
    """Top terms for a topic with platform ordering from v_seed_first_seen."""
    client = client or get_client()
    dataset = dataset or get_dataset()
    mk = market.lower()
    tg = topic_group.strip()

    # seed_graph carries one row per (term, term_type, platform). Keep the
    # term_type axis because it drives headline ranking, and sum
    # row_count so each (term, term_type, platform) is one row. The Python pass
    # collapses the (term, platform) channel grain and ranks the headline. The
    # wide LIMIT gives lower-volume cultural terms room past the generic head.
    sql = f"""
    SELECT
      sg.term,
      sg.term_type,
      sg.platform,
      SUM(sg.row_count) AS row_count,
      MIN(fs.first_seen_event_date) AS first_seen_event_date,
      MIN(fs.first_seen_ingest_date) AS first_seen_ingest_date
    FROM `{client.project}.{dataset}.seed_graph` sg
    LEFT JOIN `{client.project}.{dataset}.v_seed_first_seen` fs
      ON sg.market = fs.market AND sg.term = fs.term AND sg.platform = fs.platform
    WHERE sg.market = @market
      AND sg.trend_date = @trend_date
      AND @topic_group IN UNNEST(sg.topic_groups)
    GROUP BY sg.term, sg.term_type, sg.platform
    ORDER BY row_count DESC
    LIMIT 200
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("market", "STRING", mk),
            bq.ScalarQueryParameter("trend_date", "DATE", trend_date),
            bq.ScalarQueryParameter("topic_group", "STRING", tg),
        ]
    )
    rows = list(client.query(sql, job_config=job_config).result())
    if not rows:
        return {
            "term": tg,
            "channels": [],
            "span_days": 0,
            "confidence": "thin",
            "coverage_note": _coverage_note(set()),
        }

    # Two parallel passes over the rows:
    #   by_term  -> channels per (term, platform), deduped across term_types.
    #   scores   -> headline rank per term (term_type weight x volume).
    # A term is barred from the headline (but still allowed as a channel) when
    # it is a generic geo/breadth name or Google-Trends search boilerplate.
    by_term: dict[str, dict[str, dict[str, Any]]] = {}
    scores: dict[str, float] = {}
    for row in rows:
        term = str(row.term)
        plat = str(row.platform)
        tt = str(row.term_type or "")
        rc = int(row.row_count or 0)
        first_seen = row.first_seen_event_date or row.first_seen_ingest_date
        fs_str = first_seen.isoformat() if first_seen else None

        plats = by_term.setdefault(term, {})
        chan = plats.get(plat)
        if chan is None:
            plats[plat] = {
                "platform": plat,
                "first_seen": fs_str,
                "row_count": rc,
                "watched_since": fs_str == trend_date.isoformat() if fs_str else False,
            }
        else:
            chan["row_count"] += rc
            if fs_str and (chan["first_seen"] is None or fs_str < chan["first_seen"]):
                chan["first_seen"] = fs_str
                chan["watched_since"] = fs_str == trend_date.isoformat()

        barred = (
            _is_generic_seed_term(term)
            or term in _SEARCH_BOILERPLATE
            or (tt == "token" and plat == "search")
        )
        if not barred:
            weight = _TERM_TYPE_WEIGHT.get(tt, _TERM_TYPE_WEIGHT["token"])
            scores[term] = scores.get(term, 0.0) + rc * weight

    # Headline: highest-scoring cultural term. Fall back to raw volume only
    # when every candidate was barred, so the card still renders something.
    if scores:
        top_term = max(scores.items(), key=lambda kv: kv[1])[0]
    else:
        top_term = max(by_term.items(), key=lambda kv: sum(c["row_count"] for c in kv[1].values()))[
            0
        ]
    channels = sorted(by_term[top_term].values(), key=lambda c: -c["row_count"])
    platforms_seen = {c["platform"] for c in channels}
    dates = [c["first_seen"] for c in channels if c.get("first_seen")]
    span = 0
    if dates:
        parsed = sorted(dates)
        if len(parsed) >= 2:
            span = (date.fromisoformat(parsed[-1]) - date.fromisoformat(parsed[0])).days
    total_rows = sum(c["row_count"] for c in channels)
    n_plat = len({c["platform"] for c in channels if not c.get("watched_since")})
    measured = (
        n_plat >= MEASURED_MIN_PLATFORMS
        and span >= MEASURED_MIN_SPAN_DAYS
        and total_rows >= MEASURED_MIN_ROWS
    )
    return {
        "term": top_term,
        "channels": channels,
        "span_days": span,
        "confidence": "measured" if measured else "thin",
        "coverage_note": _coverage_note(platforms_seen),
    }


def _coverage_note(platforms_seen: set[str]) -> str:
    missing = sorted(NOT_INGESTED - platforms_seen)
    if not missing:
        return ""
    return "Not in our data: " + ", ".join(missing)


def sanitize_path_term(term: str, *, max_len: int = 40) -> str:
    """Prompt-injection guard for discovered terms (A6a)."""
    import re

    t = re.sub(r"https?://\S+|@\S+", "", (term or "").lower())
    t = re.sub(r"[^a-z0-9#_]", "", t)
    return t[:max_len]


def format_seed_path_prompt_block(path: dict[str, Any]) -> str:
    """Compact quoted-data block for build_brief_prompt when render flag is on."""
    if not path or not path.get("channels"):
        return ""
    term = sanitize_path_term(str(path.get("term") or ""))
    if not term:
        return ""
    lines = [f'Seed path data for "{term}" (cite ordering only, do not invent platforms):']
    for ch in path.get("channels") or []:
        plat = sanitize_path_term(str(ch.get("platform") or ""), max_len=20)
        fs = str(ch.get("first_seen") or "")
        if ch.get("watched_since"):
            lines.append(f"- {plat}: watched since ingest start")
        elif plat and fs:
            lines.append(f"- {plat}: first seen {fs}")
    conf = str(path.get("confidence") or "thin")
    lines.append(f"Confidence in our data: {conf}")
    note = str(path.get("coverage_note") or "").strip()
    if note:
        lines.append(note)
    return "\n".join(lines)
