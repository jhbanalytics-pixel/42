"""Driving-hashtag producer for the PULSE "What's driving the conversation" slot.

After Phase-2 briefs generate and the comment-sentiment pass runs, this fetches
each briefed topic's hashtags from BigQuery (regex-extracted from the post text
because the dedicated ``hashtags`` column is empty across connectors), ranks
them by posts+engagement with a generic-tag stoplist, runs one Gemini call per
top-N tag to compute the mood per tag from the posts and comments using it,
hardens the output, and tags the brief dict so the hashtag slot
(``src/alerts/email_render/card.py:_hashtags``) renders LIVE for the most
hashtag-rich topics.

Mirrors ``src/analysis/comment_sentiment.py`` by contract: isolated, non-fatal,
gated at the call site behind ``DRIVING_HASHTAGS_ENABLED``. Any failure logs
and leaves the briefs untouched, so the email ships exactly as it would
without it.

Per-tag mood reuses the comment-sentiment producer pattern (one Gemini call,
schema-validated, all-or-nothing). The corpus for a tag is the posts and
comments USING THAT tag (caption text across TikTok, Instagram, Threads and
reddit, not reddit-only), not the topic-wide corpus, so a tag earns its OWN
mood rather than inheriting the topic's mood. Hardening identical to the room
producer: schema-valid, no smuggled double-quotes, scrub em/en/double-hyphen,
no person names, short clause cap.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
from typing import Any

# The gemini_usage ledger has ONE row shape shared by every Vertex Gemini
# caller, so this stage imports the writer rather than mirroring it: two
# hand-copied writers would drift and the watchdog reads both through the same
# columns.
from src.utils.gemini_usage import (
    UsageSink,
    persist_gemini_usage,
    record_usage,
    usage_rows,
)

logger = logging.getLogger(__name__)

USAGE_CONSUMER = "driving_hashtags"

# All-digit tags (#8217, #039, #254) are HTML-entity numbers the regex picks
# up from "&#8217;"-style apostrophes in text, never real hashtags. Drop them
# alongside the platform-generic stoplist.
_PREFIX_RE = re.compile(r"^\s*\[r/[^\]]+\]\s*")
# Smuggled-verbatim-quote guard: a double-quoted span in the output means the
# model pasted real text. Same as comment_sentiment.
_DOUBLE_QUOTES = ('"', "“", "”")

# Generic platform/discovery tags that appear on every topic and carry no
# topic signal. Lower-cased, without the leading '#'. Kept conservative: only
# clearly-generic tags, never a word that could be a real topic marker
# (e.g. "amapiano", "mpesa" stay in).
DEFAULT_STOPLIST = frozenset(
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

TOP_N_DEFAULT = 3
MIN_POSTS_PER_TAG_DEFAULT = 3
MOOD_CHAR_CAP = 52
TAG_CHAR_CAP = 40

# Topics whose taxonomy term is a global homonym so heavily contaminated that the
# foreign slice dominates the hashtag ranking, and neither the geo_blocklist
# (keyword markers catch under 10% of the noise) nor the language guard (the
# worst polluters are short hashtag-only posts below the 100-char langdetect
# floor) cleans it reliably. diaspora_japa: "japa" is Nigerian emigration slang
# AND Brazilian-Portuguese for Japanese food AND a Sanskrit mantra term, so
# #sushi / #comidajaponesa would render as "driving" the Nigerian diaspora
# conversation. Skip these so the card cages instead of surfacing foreign tags;
# the brief still renders. Remove a topic once a per-topic Nigerian-context
# allowlist cleans it (the proper homonym fix).
# tech_gemini_ai joined 11 Jun 2026: "gemini" is also the zodiac sign, so
# astrology tags (#geminiseason, #zodiac) would render as "driving" the
# Google Gemini conversation. Cage the card until a Gemini-context
# allowlist cleans the tag pool.
_GEO_HOMONYM_DENY: frozenset[str] = frozenset({"diaspora_japa", "tech_gemini_ai"})

MOOD_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"mood": {"type": "string"}},
    "required": ["mood"],
}

MOOD_SYSTEM_INSTRUCTION = (
    "You read posts and comments USING a single hashtag in a Sub-Saharan African "
    "social discussion. Return ONE short clause (max five words) naming the mood "
    "specifically tied to that hashtag (for example: practical and supportive, "
    "frustrated about prices, ambitious side-hustle pride). Never copy verbatim "
    "text, never wrap text in quotes, never name a person, never use a dash. "
    "Summarise only what THIS tag's discussion actually says."
)


# ----------------------------------------------------------------------
# Pure helpers (unit-testable without BigQuery)


def is_generic(tag: str, stoplist: frozenset[str] = DEFAULT_STOPLIST) -> bool:
    """True when a hashtag is platform-generic noise or an encoding artifact.

    Strips a single leading '#', lower-cases, checks the stoplist, drops
    all-digit tags (HTML entity artifacts). Pure.
    """
    cleaned = tag.lstrip("#").lower()
    if not cleaned:
        return True
    if cleaned.isdigit():
        return True
    return cleaned in stoplist


def _scrub_dashes(text: str) -> str:
    """Strip em/en dashes and double-hyphens before output reaches render.

    Matches comment_sentiment._scrub_dashes. The digest auditor hard-fails on
    those three; the producer scrubs them at the boundary so the brief can
    never carry one.
    """
    return text.replace("--", " ").replace(chr(0x2014), " ").replace(chr(0x2013), " ")


def _clamp_words(text: str, cap: int) -> str:
    """Clamp to <= cap chars at a WORD boundary so a mood never cuts mid-word.

    The raw ``[:cap]`` slice cut moods like 'humorous resilience through fi' off a
    real word; this trims to the last whole word that fits instead.
    """
    s = text.strip()
    if len(s) <= cap:
        return s
    return s[:cap].rsplit(" ", 1)[0].rstrip(",.;:") or s[:cap]


def rank_tags(
    rows: list[dict[str, Any]],
    top_n: int = TOP_N_DEFAULT,
    min_posts: int = MIN_POSTS_PER_TAG_DEFAULT,
    stoplist: frozenset[str] = DEFAULT_STOPLIST,
) -> list[dict[str, Any]]:
    """Apply the stoplist + min-posts floor + top-N cut to pre-aggregated rows.

    Input rows: ``[{tag, posts, engagement}]`` already grouped per topic by SQL.
    Returns the top N tags with a share-of-total-tagged-posts field added so
    the renderer can show "24% of tagged posts" without a second pass.

    The share denominator is the total posts across EVERY extracted tag
    (before the stoplist and min-posts cut), not just the surviving
    candidates. card.py renders the value as "X% of tagged posts", so dividing
    by the survivors alone overstated the share whenever generic or low-volume
    tags were pruned. Counting them keeps the displayed percentage honest.
    """
    total = sum(int(r.get("posts") or 0) for r in rows) or 1
    candidates: list[dict[str, Any]] = []
    for r in rows:
        tag = str(r["tag"])
        if is_generic(tag, stoplist):
            continue
        posts = int(r.get("posts") or 0)
        if posts < min_posts:
            continue
        candidates.append(
            {
                "tag": tag,
                "posts": posts,
                "engagement": int(float(r.get("engagement") or 0)),
            }
        )
    if not candidates:
        return []
    candidates.sort(key=lambda t: (t["posts"], t["engagement"]), reverse=True)
    top = candidates[:top_n]
    for t in top:
        t["share_pct"] = round(t["posts"] * 100.0 / total, 1)
    return top


def _harden_mood(mood: Any) -> str | None:
    """Validate and clamp the per-tag mood string; return None on any fail.

    All-or-nothing: if the model returns junk, no mood string is set so the
    renderer falls back to "tag without mood" (caged), never half-rendered.
    """
    if not isinstance(mood, str):
        return None
    clean = _clamp_words(_scrub_dashes(mood.strip()), MOOD_CHAR_CAP)
    if not clean:
        return None
    if any(q in clean for q in _DOUBLE_QUOTES):
        return None
    return clean


# ----------------------------------------------------------------------
# Gemini call


def mood_for_tag(
    tag: str,
    comments: list[str],
    market: str,
    topic: str,
    gemini_client: Any,
    usage_tally: dict | None = None,
) -> str | None:
    """One Gemini call for one tag in one topic -> short mood clause or None.

    Best-effort, no retry (matches comment_sentiment.summarise_topic). Errors
    log non-fatal and the tag stays caged for the day; the slot still renders
    the other tags that succeeded.

    ``usage_tally``, when passed, collects this call's token counts for the
    gemini_usage ledger. A raising call adds nothing, so a Vertex 5xx never
    bills the ledger for tokens it did not spend. This stage fires one call PER
    TAG per topic, so it is the higher-volume of the two token-blind consumers.
    """
    if not comments:
        return None
    corpus = "\n".join(f"- {c}" for c in comments)
    prompt = (
        f"Market: {market}. Topic: {topic}. Hashtag: {tag}.\n"
        f"Posts and comments using {tag}:\n{corpus}\n\n"
        f"Return the mood specifically tied to {tag}."
    )
    try:
        resp = gemini_client.generate_brief(
            prompt,
            MOOD_RESPONSE_SCHEMA,
            system_instruction=MOOD_SYSTEM_INSTRUCTION,
            temperature=0.4,
        )
    except Exception as exc:
        logger.warning(
            "Driving-hashtag mood failed for %s/%s/%s (non-fatal): %s",
            market,
            topic,
            tag,
            exc,
        )
        return None
    if usage_tally is not None:
        record_usage(usage_tally, market, resp)
    parsed = getattr(resp, "parsed", None) or {}
    return _harden_mood(parsed.get("mood"))


# ----------------------------------------------------------------------
# BQ fetches (non-fatal, mirror comment_sentiment patterns)


def _fetch_topic_tag_counts(
    bq_client: Any,
    project: str,
    dataset: str,
    market: str,
    topic: str,
    trend_date: datetime.date,
) -> list[dict[str, Any]]:
    """Pre-rank tag counts per topic: regex-extract #tags from text, aggregate.

    Pulls top 25 raw candidates so the Python-side stoplist + top-N cut has
    room to work after generic-tag pruning. Non-fatal: one bad topic returns
    [] so the rest of the pass continues.
    """
    from google.cloud import bigquery

    # Dedupe tags per post before aggregating. A caption repeating a tag N
    # times would otherwise add N to COUNT(*) and N copies of the post's
    # engagement to SUM(eng), inflating rank, the min_posts floor, and the
    # exec-facing share. SELECT DISTINCT id, tag collapses each post to one row
    # per distinct tag so posts counts distinct posts and engagement is summed
    # once per post.
    sql = f"""
        WITH tags AS (
          SELECT DISTINCT id, LOWER(h) AS tag, IFNULL(engagement_total, 0) AS eng
          FROM `{project}.{dataset}.enriched_content`,
               UNNEST(topic_groups) AS tg,
               UNNEST(REGEXP_EXTRACT_ALL(IFNULL(text, ''), r'#\\w+')) AS h
          WHERE DATE(collected_at) = @trend_date
            AND market = @market
            AND tg = @topic_group
        )
        SELECT tag, COUNT(DISTINCT id) AS posts, SUM(eng) AS engagement
        FROM tags
        GROUP BY tag
        QUALIFY ROW_NUMBER() OVER (ORDER BY COUNT(DISTINCT id) DESC, SUM(eng) DESC) <= 25
        ORDER BY posts DESC
    """
    try:
        job = bq_client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("trend_date", "DATE", trend_date),
                    bigquery.ScalarQueryParameter("market", "STRING", market),
                    bigquery.ScalarQueryParameter("topic_group", "STRING", topic),
                ]
            ),
        )
        return [dict(r) for r in job.result()]
    except Exception as exc:
        logger.warning("Tag-count fetch failed for %s/%s (non-fatal): %s", market, topic, exc)
        return []


def _fetch_content_mentioning_tag(
    bq_client: Any,
    project: str,
    dataset: str,
    market: str,
    topic: str,
    tag: str,
    trend_date: datetime.date,
    cap: int = 20,
    body_char_cap: int = 600,
) -> list[str]:
    """Posts and comments using the tag, for the per-tag mood Gemini call.

    Hashtags live in TikTok/Instagram/Threads caption text, not reddit, so the
    corpus is ANY enriched_content row for this (date, market, topic) whose text
    mentions the tag, ranked by engagement. Strips the ``[r/<sub>]`` prefix and
    drops reddit tombstones (harmless on non-reddit text), truncates each body to
    ``body_char_cap``, caps the corpus at ``cap`` rows. Non-fatal: a failure
    returns [] so that one tag stays caged for the day.
    """
    from google.cloud import bigquery

    tag_pattern = tag if tag.startswith("#") else f"#{tag}"
    # A LIKE '%#tag%' match prefix-collides (#japa matches #japan) and treats
    # the LIKE wildcards '_' / '%' inside a tag as wildcards, so the per-tag
    # corpus can pull unrelated posts. Anchor on a re-escaped lowercase tag
    # plus a trailing non-word boundary so #japa stops at a word edge and never
    # matches #japan.
    tag_regex = re.escape(tag_pattern.lower()) + r"(\W|$)"
    sql = f"""
        SELECT text
        FROM `{project}.{dataset}.enriched_content`
        WHERE DATE(collected_at) = @trend_date
          AND market = @market
          AND @topic_group IN UNNEST(topic_groups)
          AND REGEXP_CONTAINS(LOWER(IFNULL(text, '')), @tag_regex)
          AND LENGTH(IFNULL(text, '')) > 0
        ORDER BY engagement_total DESC
        LIMIT @cap
    """
    try:
        job = bq_client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("trend_date", "DATE", trend_date),
                    bigquery.ScalarQueryParameter("market", "STRING", market),
                    bigquery.ScalarQueryParameter("topic_group", "STRING", topic),
                    bigquery.ScalarQueryParameter("tag_regex", "STRING", tag_regex),
                    bigquery.ScalarQueryParameter("cap", "INT64", int(cap)),
                ]
            ),
        )
        rows = [dict(r) for r in job.result()]
    except Exception as exc:
        logger.warning(
            "Tag-content fetch failed for %s/%s/%s (non-fatal): %s",
            market,
            topic,
            tag,
            exc,
        )
        return []
    out: list[str] = []
    for r in rows:
        body = _PREFIX_RE.sub("", str(r.get("text") or "")).strip()
        if body and body.lower() not in ("[deleted]", "[removed]"):
            out.append(body[:body_char_cap])
    return out


# ----------------------------------------------------------------------
# Top-level producer


def compute_driving_hashtags(
    trend_date: datetime.date,
    briefs_by_topic: dict[tuple[str, str], dict],
    *,
    gemini_client: Any = None,
    bq_client: Any = None,
    dataset: str | None = None,
    top_n: int = TOP_N_DEFAULT,
    min_posts: int = MIN_POSTS_PER_TAG_DEFAULT,
    stoplist: frozenset[str] = DEFAULT_STOPLIST,
    usage_sink: UsageSink | None = None,
) -> dict[tuple[str, str], dict]:
    """Fetch, rank, Gemini per tag, harden. Returns ``{(market, topic): {...}}``.

    None-defaults match comment_sentiment so tests inject mocks. Non-fatal by
    contract at every level: a fetch failure for one topic returns []; a mood
    failure for one tag leaves that tag's mood None; only tags with BOTH a
    valid mood AND >= min_posts survive into the final payload.

    Every Gemini call's tokens land in the gemini_usage ledger via ``usage_sink``
    (default ``persist_gemini_usage``), flushed in a ``finally`` so a mid-pass
    failure still records the spend that already happened.
    """
    usage_tally: dict = {}
    sink = usage_sink or persist_gemini_usage
    try:
        from src.analysis.gemini_client import GeminiClient
        from src.utils.bigquery import get_client, get_dataset

        gemini_client = gemini_client or GeminiClient()
        bq_client = bq_client or get_client()
        dataset = dataset or get_dataset()
        project = getattr(bq_client, "project", None) or os.environ.get("GCP_PROJECT")

        out: dict[tuple[str, str], dict] = {}
        for market, topic in briefs_by_topic:
            if topic in _GEO_HOMONYM_DENY:
                continue
            tag_rows = _fetch_topic_tag_counts(
                bq_client, project, dataset, market, topic, trend_date
            )
            if not tag_rows:
                continue
            top_tags = rank_tags(tag_rows, top_n=top_n, min_posts=min_posts, stoplist=stoplist)
            if not top_tags:
                continue
            tags_with_mood: list[dict[str, Any]] = []
            for t in top_tags:
                comments = _fetch_content_mentioning_tag(
                    bq_client, project, dataset, market, topic, t["tag"], trend_date
                )
                mood = mood_for_tag(t["tag"], comments, market, topic, gemini_client, usage_tally)
                if mood:
                    t_out = dict(t)
                    # Defensive: clamp the tag length, scrub dashes from the tag itself
                    # so a regex artifact like #word--word never reaches the render.
                    t_out["tag"] = _scrub_dashes(str(t["tag"]))[:TAG_CHAR_CAP]
                    t_out["mood"] = mood
                    tags_with_mood.append(t_out)
            if tags_with_mood:
                out[(market, topic)] = {"driving_hashtags": tags_with_mood}
        logger.info("Driving hashtags computed for %d/%d topics", len(out), len(briefs_by_topic))
        return out
    except Exception as exc:
        logger.error("Driving hashtags failed (non-fatal): %s", exc, exc_info=True)
        return {}
    finally:
        sink(usage_rows(trend_date, USAGE_CONSUMER, usage_tally))


def tag_briefs_with_driving_hashtags(
    briefs_by_topic: dict[tuple[str, str], dict],
    payload: dict[tuple[str, str], dict],
) -> int:
    """Attach the ``driving_hashtags`` field to each brief in place; count tagged.

    Mirrors ``tag_briefs_with_comment_sentiment``. All-or-nothing per topic so
    the card is never half-LIVE: a topic missing from ``payload`` is left
    untouched.
    """
    tagged = 0
    for key, brief in briefs_by_topic.items():
        entry = payload.get(key)
        if entry and entry.get("driving_hashtags"):
            brief["driving_hashtags"] = [dict(t) for t in entry["driving_hashtags"]]
            tagged += 1
    return tagged
