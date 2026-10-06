"""Hashtags driving the conversation, per market per topic.

Descriptive, not prescriptive. This surfaces the hashtags people are
ACTUALLY using on each trending topic right now (ranked by post count and
engagement), so the team can see what is driving the conversation. It is
distinct from the prescriptive hashtag recommender (Gemini-curated,
brand-safe tags to USE in a campaign); this one just reports observed
reality, so it is accurate by construction and carries no prediction risk.

The connectors leave the dedicated `hashtags` column empty, but the tags
live in the post `text` (TikTok / Instagram captions especially), so the
ranking regex-extracts `#\\w+` from text. A stoplist drops platform-generic
noise (#fyp, #foryou, #viral, ...) so the topic-specific tags surface.

Read-only. No BQ writes, no Vertex calls. Mirrors engine_pulse.py.

CLI:
    python scripts/hashtags_driving.py [--date YYYY-MM-DD] [--top N] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any

# Platform-generic tags that appear on every topic and carry no
# topic signal. Lower-cased, without the leading '#'. Kept deliberately
# conservative: only clearly-generic discovery/platform tags, never a word
# that could be a real topic marker (e.g. "amapiano", "mpesa" stay in).
GENERIC_TAG_STOPLIST = frozenset(
    {
        "fyp",
        "fypシ",
        "fypage",
        "foryou",
        "foryoupage",
        "foryourpage",
        "viral",
        "viralvideo",
        "viraltiktok",
        "trending",
        "trend",
        "explore",
        "explorepage",
        "reels",
        "reel",
        "reelsinstagram",
        "instagram",
        "insta",
        "instagood",
        "instadaily",
        "tiktok",
        "tiktokviral",
        "capcut",
        "duet",
        "stitch",
        "follow",
        "followme",
        "like",
        "likes",
        "share",
        "comment",
        "subscribe",
        "shorts",
        "short",
        "youtube",
        "youtuber",
        "video",
        "viralpost",
        "pov",
        "trendingnow",
        "exploremore",
    }
)

DEFAULT_TOP = 5


@dataclass
class TopicHashtags:
    market: str
    topic: str
    hashtags: list[dict[str, Any]]  # [{tag, posts, engagement}]


def is_generic(tag: str, stoplist: frozenset[str] = GENERIC_TAG_STOPLIST) -> bool:
    """True when a hashtag is platform-generic noise (drop it from the ranking).

    Strips a single leading '#', lower-cases, then checks the stoplist. Pure.
    """
    cleaned = tag.lstrip("#").lower()
    # All-digit tags (#8217, #039, #254) are HTML-entity / encoding artifacts
    # the regex picks up from "&#8217;"-style apostrophes in text, never real
    # hashtags, so drop them too.
    return cleaned in stoplist or cleaned.isdigit()


def rank_topic_hashtags(rows: list[dict[str, Any]], top: int = DEFAULT_TOP) -> list[TopicHashtags]:
    """Group flat (market, topic, hashtag, posts, engagement) rows into a
    per-topic top-N ranking, dropping generic tags.

    Pure: no IO. `rows` is already aggregated per (market, topic, hashtag)
    by the SQL; this applies the stoplist and the top-N cut so the cut is
    unit-testable without BigQuery and identical to a downstream caller's.
    """
    by_topic: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        tag = str(r["hashtag"])
        if is_generic(tag):
            continue
        key = (str(r["market"]), str(r["topic"]))
        by_topic.setdefault(key, []).append(
            {
                "tag": tag,
                "posts": int(r["posts"]),
                "engagement": int(float(r["engagement"] or 0)),
            }
        )

    out: list[TopicHashtags] = []
    for (market, topic), tags in sorted(by_topic.items()):
        ranked = sorted(tags, key=lambda t: (t["posts"], t["engagement"]), reverse=True)[:top]
        out.append(TopicHashtags(market=market, topic=topic, hashtags=ranked))
    return out


def _fetch(trend_date: str) -> list[dict[str, Any]]:
    from google.cloud import bigquery

    client = bigquery.Client(project="ogilvy-trends-v2")
    # Extract #tags from text per (market, topic), aggregate by post count +
    # engagement. Pulls a generous candidate set (top 25 per topic by raw
    # frequency) so the Python-side stoplist + top-N cut has room to work.
    sql = """
        WITH tags AS (
          SELECT market, tg AS topic, LOWER(h) AS hashtag, IFNULL(engagement_total, 0) AS eng
          FROM `ogilvy-trends-v2.trends_v2_dev.enriched_content`,
               UNNEST(topic_groups) AS tg,
               UNNEST(REGEXP_EXTRACT_ALL(IFNULL(text, ''), r'#\\w+')) AS h
          WHERE DATE(collected_at) = @d AND market IN ('za', 'ng', 'ke')
        )
        SELECT market, topic, hashtag, COUNT(*) AS posts, SUM(eng) AS engagement
        FROM tags
        GROUP BY market, topic, hashtag
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY market, topic ORDER BY COUNT(*) DESC, SUM(eng) DESC) <= 25
        ORDER BY market, topic, posts DESC
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", trend_date)]
    )
    return [dict(r) for r in client.query(sql, job_config=cfg).result()]


def build(trend_date: str, top: int = DEFAULT_TOP) -> list[TopicHashtags]:
    return rank_topic_hashtags(_fetch(trend_date), top=top)


def render_text(trend_date: str, topics: list[TopicHashtags]) -> str:
    out = [f"HASHTAGS DRIVING THE CONVERSATION {trend_date}", ""]
    if not topics:
        out.append("  no hashtag signal for this date")
        return "\n".join(out)
    current_market = ""
    for t in topics:
        if t.market != current_market:
            current_market = t.market
            out.append(f"[{t.market.upper()}]")
        tags = "  ".join(f"{h['tag']}({h['posts']})" for h in t.hashtags) or "(none)"
        out.append(f"  {t.topic:24s} {tags}")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="", help="trend_date YYYY-MM-DD; default today UTC")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="top N tags per topic")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()
    trend_date = args.date or datetime.now(UTC).date().isoformat()
    try:
        date.fromisoformat(trend_date)
    except ValueError:
        print(f"invalid date: {trend_date}", file=sys.stderr)
        return 2
    try:
        topics = build(trend_date, top=args.top)
    except Exception as exc:  # pragma: no cover
        print(f"hashtags_driving failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps({"trend_date": trend_date, "topics": [asdict(t) for t in topics]}, indent=2)
        )
    else:
        print(render_text(trend_date, topics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
