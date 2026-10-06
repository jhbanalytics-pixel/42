"""Behaviour scan: market-wide signal read before brief synthesis.

T's ask: do not synthesise before the behaviour is seen. This module breaks the
day's clustered signal into plain-English behaviours per market, each with a
count and 2 to 3 real post examples, so a human can approve before any brief is
written. Pure BigQuery reads, no Gemini, no ingestion.
"""

from __future__ import annotations

import logging
import os
import re

from . import bq

logger = logging.getLogger("listening_post.behaviours")

DEFAULT_PER_MARKET = 10
EXAMPLES_PER_BEHAVIOUR = 3
# Pull a wider candidate set than we show, then select the ones that carry real
# voice-post proof. Many top-score topics are news or search driven with no
# social posts; widening lets proof-backed behaviours fill the list.
_CANDIDATE_CAP = 25


def _first_sentence(text: str) -> str:
    blob = re.sub(r"\s+", " ", str(text or "")).strip()
    if not blob:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", blob)
    return parts[0].strip() if parts else blob


def _behaviour_line(row: dict) -> str:
    """One plain-English read of the behaviour, humanised, no snake_case."""
    for key in ("cultural_context", "headline", "trend_synthesis"):
        line = _first_sentence(row.get(key))
        if line:
            return bq.humanize_topic_refs(bq.truncate_words(line, 32))
    return bq.topic_label(str(row.get("query_group") or ""))


def _client_metrics_default() -> bool:
    return os.environ.get("RESEARCH_CLIENT_METRICS_DEFAULT", "true").strip().lower() == "true"


def _example_from_voice_row(row: dict) -> dict:
    url = str(row.get("url") or "")
    if not url.lower().startswith(("http://", "https://")):
        url = ""
    voice = str(row.get("voice_kind") or bq._voice_kind(row.get("content_type")))
    return {
        "text": bq.truncate_words(str(row.get("text") or row.get("title") or ""), 40),
        "platform": str(row.get("platform") or ""),
        "handle": str(row.get("handle") or row.get("author_handle") or ""),
        "url": url,
        "voice_kind": voice,
        "content_type": str(row.get("content_type") or ""),
        "engagement": int(row.get("engagement") or row.get("engagement_total") or 0),
        "published_at": row.get("published_at"),
    }


def _pick_examples(voice_rows: list, *, max_total: int = EXAMPLES_PER_BEHAVIOUR) -> list:
    """Prefer a mix of posts and comments when both exist."""
    posts = [r for r in voice_rows if (r.get("voice_kind") or "post") != "comment"]
    comments = [r for r in voice_rows if (r.get("voice_kind") or "") == "comment"]
    picked: list = []
    if comments:
        picked.append(comments[0])
    for row in posts:
        if len(picked) >= max_total:
            break
        picked.append(row)
    for row in comments[1:]:
        if len(picked) >= max_total:
            break
        if row not in picked:
            picked.append(row)
    return [_example_from_voice_row(r) for r in picked[:max_total]]


def _bucket_posts(posts: list, groups: set[str], market: str) -> dict[str, list]:
    by_group: dict[str, list] = {}
    for p in posts:
        if str(p.get("market") or "").lower() != market:
            continue
        # fetch_posts_for_topics tags each row with the topic it was matched on.
        tagged = str(p.get("topic_group") or "")
        keys = [tagged] if tagged in groups else [str(tg) for tg in (p.get("topic_groups") or []) if str(tg) in groups]
        for key in keys:
            by_group.setdefault(key, []).append(p)
    return by_group


def scan_market_behaviours(
    markets: list[str],
    per_market: int = DEFAULT_PER_MARKET,
    *,
    trend_date=None,
) -> dict:
    """Return per-market behaviour rows with proof. No synthesis."""
    mks = [m.strip().lower() for m in (markets or []) if m]
    if not mks:
        return {"markets": [], "behaviours": {}, "trend_date": None}

    td = trend_date or bq.latest_trend_date()
    candidate_per = min(_CANDIDATE_CAP, max(per_market * 2, per_market + 12))
    topics = bq.fetch_market_topics(mks, candidate_per, trend_date=td)

    groups_by_market: dict[str, set[str]] = {}
    for row in topics:
        mk = str(row.get("market") or "").lower()
        qg = str(row.get("query_group") or "")
        if mk and qg:
            groups_by_market.setdefault(mk, set()).add(qg)

    all_groups = sorted({g for gs in groups_by_market.values() for g in gs})
    voice_rows = (
        bq.fetch_posts_and_comments_for_topics(
            mks,
            all_groups,
            per_topic_posts=EXAMPLES_PER_BEHAVIOUR,
            per_topic_comments=5,
        )
        if all_groups
        else []
    )
    voice_metrics = bq.fetch_voice_metrics_for_topics(mks, all_groups) if all_groups else {}

    out: dict[str, list] = {}
    for mk in mks:
        by_group = _bucket_posts(voice_rows, groups_by_market.get(mk, set()), mk)
        rows = []
        for row in topics:
            if str(row.get("market") or "").lower() != mk:
                continue
            qg = str(row.get("query_group") or "")
            examples = _pick_examples(by_group.get(qg, []))
            vm = voice_metrics.get((mk, qg), {})
            platforms = sorted({e["platform"] for e in examples if e["platform"]})
            rows.append(
                {
                    "id": f"{mk}:{qg}",
                    "market": mk,
                    "query_group": qg,
                    "signal_topic": bq.topic_label(qg),
                    "behaviour": _behaviour_line(row),
                    "metric": {
                        "post_count": int(vm.get("post_count") or 0),
                        "comment_count": int(vm.get("comment_count") or 0),
                        "engagement_total": int(vm.get("engagement_total") or 0),
                        "platform_count": int(vm.get("platform_count") or len(platforms)),
                        "mention_count": None,
                        "trend_score": round(float(row.get("trend_score") or 0), 3),
                    },
                    "platforms": platforms,
                    "examples": examples,
                }
            )
        # Proof leads: behaviours with real example posts rank above bare topics,
        # then by trend strength. A behaviour with no voice posts is weak signal.
        # From the wider candidate set, keep the top per_market with proof first.
        rows.sort(key=lambda r: (len(r["examples"]) > 0, r["metric"]["trend_score"]), reverse=True)
        out[mk] = rows[:per_market]

    trend_date_str = td.isoformat() if td is not None and hasattr(td, "isoformat") else (str(td) if td else None)
    logger.info(
        "behaviour scan markets=%s per=%d topics=%d voice_rows=%d",
        mks,
        per_market,
        len(topics),
        len(voice_rows),
    )
    return {
        "markets": mks,
        "behaviours": out,
        "trend_date": trend_date_str,
        "client_metrics_default": _client_metrics_default(),
    }
