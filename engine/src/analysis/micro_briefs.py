"""Micro-brief early-signals slot (Wave 3, dark).

Small-N but real cultural moments (14 to 17 rows in the observed FN class)
dissolve into broad topic buckets today and never render anywhere. This module
gives them one rendered line each, below the main briefs, at zero Gemini cost.

``select_micro_briefs`` is a pure function: it takes the seed_graph row-grain
rows already built in-process for the day (see ``src.analysis.seed_graph``)
and the same config-term / stoplist / safety loaders the discovery loop uses
(``src.analysis.seed_candidates``), and returns a small ranked list of terms
that are real but too small to earn a full brief. No BigQuery, no network, no
Gemini call anywhere in this path.

Wired by ``scripts/run_rss_now.py`` only when ``MICRO_BRIEFS_ENABLED`` is
true; the render side (``src.alerts.email_render``) drops the whole section
when the flag is off or no items are supplied, so a flag-off day is
byte-identical to today.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from src.analysis.seed_candidates import aggregate_terms, check_safety

# term_type values eligible for a micro-brief: hashtag/slang plus the
# "multi-word structured" types (music, channel) per the plan spec.
_ELIGIBLE_TERM_TYPES = frozenset({"hashtag", "slang", "music", "channel"})

_MIN_FREQUENCY = 5
_MAX_FREQUENCY = 50
_MAX_AGE_DAYS = 3
_DEFAULT_LIMIT = 3
_MAX_PLATFORMS = 2
_MAX_TOPICS = 2
_MAX_SAMPLE_ROW_IDS = 3


def _term_types_for(rows: list[dict[str, Any]], market: str, term: str) -> set[str]:
    """The distinct term_type values a term appears under in the raw rows.

    ``aggregate_terms`` collapses candidate_type down to a binary keyword/slang
    split, which loses the music/channel distinction the plan asks for, so this
    reads term_type straight off the row grain instead.
    """
    mk = market.lower()
    types: set[str] = set()
    for raw in rows:
        if str(raw.get("market") or "").lower() != mk:
            continue
        if str(raw.get("term") or "").lower() != term:
            continue
        types.add(str(raw.get("term_type") or "token").lower())
    return types


def select_micro_briefs(
    seed_graph_rows: list[dict[str, Any]],
    market: str,
    trend_date: date,
    *,
    config_terms: set[str],
    stoplist: frozenset[str] | set[str],
    limit: int = _DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Select up to ``limit`` early-signal terms for one market.

    Aggregates ``seed_graph_rows`` per term (reusing
    ``seed_candidates.aggregate_terms``, the same aggregation the discovery
    loop uses), then keeps a term when all of:

    - term_type is hashtag, slang, music, or channel (a structured or slang
      term, not a bare token)
    - frequency (distinct row_ids) is between 5 and 50 inclusive
    - first_seen is within 3 days of ``trend_date``
    - the term is not in ``config_terms`` or ``stoplist``
    - ``check_safety`` returns no flags
    - the term carries at least one topic_group or near_topic

    Survivors are ranked by frequency descending, ties broken by more recent
    first_seen first (newer wins), then by term for a stable order, and
    capped at ``limit``.

    Returns a list of dicts: term, frequency, platforms (top 2 by row count),
    topics (up to 2, topic_groups preferred over near_topics), sample_row_ids
    (up to 3).
    """
    mk = market.lower()
    agg = aggregate_terms(seed_graph_rows, mk)

    candidates: list[dict[str, Any]] = []
    for term, bucket in agg.items():
        if term in config_terms or term in stoplist:
            continue

        term_types = _term_types_for(seed_graph_rows, mk, term)
        if not term_types & _ELIGIBLE_TERM_TYPES:
            continue

        freq = int(bucket.get("frequency") or 0)
        if freq < _MIN_FREQUENCY or freq > _MAX_FREQUENCY:
            continue

        first_seen = bucket.get("first_seen_event_date")
        if first_seen is None:
            continue
        if (trend_date - first_seen).days > _MAX_AGE_DAYS:
            continue

        topics = sorted(bucket.get("topic_groups") or [])
        near_topics = sorted(bucket.get("near_topics") or [])
        if not topics and not near_topics:
            continue

        if check_safety(bucket):
            continue

        platform_counts = bucket.get("platform_counts") or {}
        platforms = [
            p
            for p, _ in sorted(platform_counts.items(), key=lambda kv: (-kv[1], kv[0]))[
                :_MAX_PLATFORMS
            ]
        ]

        combined_topics = (topics or near_topics)[:_MAX_TOPICS]
        sample_row_ids = list(bucket.get("sample_row_ids") or [])[:_MAX_SAMPLE_ROW_IDS]

        candidates.append(
            {
                "term": term,
                "frequency": freq,
                "platforms": platforms,
                "topics": combined_topics,
                "sample_row_ids": sample_row_ids,
                "_first_seen": first_seen,
            }
        )

    candidates.sort(key=lambda c: (-c["frequency"], -_recency_key(c["_first_seen"]), c["term"]))
    for c in candidates:
        del c["_first_seen"]

    return candidates[:limit]


def _recency_key(first_seen: date) -> int:
    """Ordinal day number so a later (more recent) date sorts higher."""
    return (first_seen - date(1970, 1, 1)).days if first_seen else 0


__all__ = ["select_micro_briefs"]
