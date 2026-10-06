"""Bridge the per-row sentiment lexicon scores onto briefs for the tone split.

The Wave 2 lexicon scorer writes one ``sentiment_lexicon_score`` (a float in
[-1.0, 1.0]) on every ``enriched_content`` row, gated by
``SENTIMENT_LEXICON_ENABLED``. The PULSE mailer's tone-split section
(``src/alerts/email_render/tone_split.py``) bins those scores into a
positive / neutral / negative share, but it reads them off a per-brief
``sentiment_lexicon_scores`` LIST that nothing ever attached, so the section
always rendered empty even with ``TONE_SPLIT_ENABLED`` on.

This module closes that gap with the same compute-then-tag shape the continuity
and forecast producers use: ``fetch_lexicon_scores`` rolls the per-row scores up
to one list per (market, query_group) for a date, and
``tag_briefs_with_lexicon_scores`` stamps the list onto each brief in place. The
render self-gates and drops cleanly when the list is absent, so this stage is
purely additive: with the flag off it runs nothing and the email is unchanged.

Non-fatal by contract: any BigQuery failure logs and yields an empty lookup, so
the daily pipeline and the email are never blocked by it.
"""

from __future__ import annotations

import datetime

from google.cloud import bigquery

from src.utils.bigquery import get_client, get_dataset
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

# Guard against a runaway list: a topic with tens of thousands of rows would bloat
# the brief dict and the render. The split is a share, so a large sample is enough,
# and the SQL orders the sample by a stable score hash before this LIMIT so the
# kept slice is deterministic and source-unbiased rather than storage-ordered.
_MAX_SCORES_PER_TOPIC = 2000


def fetch_lexicon_scores(
    trend_date: datetime.date,
) -> dict[tuple[str, str], list[float]]:
    """Roll the per-row ``sentiment_lexicon_score`` up to one list per topic.

    Returns ``{(market, query_group): [score, ...]}`` for the date, unnesting
    ``topic_groups`` so a row that matched several topics contributes to each.
    Only non-null scores in [-1.0, 1.0] are kept, capped per topic at a
    deterministic, source-unbiased sample (the SQL orders by a stable score hash
    before the cap). A topic with no scored rows is omitted, so the tagger leaves
    those briefs untouched.

    Non-fatal: any failure logs and returns ``{}`` so the caller carries on and
    the tone-split section simply stays empty.
    """
    try:
        client = get_client()
        dataset = get_dataset()
        # _MAX_SCORES_PER_TOPIC is an int constant (BQ's ARRAY_AGG LIMIT needs a
        # literal, not a parameter), so inlining it is safe, not injectable.
        #
        # ORDER BY FARM_FINGERPRINT(...) inside the ARRAY_AGG makes the truncated
        # sample deterministic and unbiased. Without it, BigQuery returns an
        # arbitrary storage-ordered subset of the first 2000 scores for any topic
        # with more than that many rows. Storage order clusters by source and
        # ingest batch, so the positive/neutral/negative split was source-biased
        # and drifted across re-runs with no data change. The hash of the score
        # string is a stable pseudo-random key: the same rows survive the LIMIT on
        # every run, and the kept sample is a representative slice of the full set
        # rather than whichever batch happened to land first in storage.
        sql = f"""
        SELECT
          market,
          tg AS query_group,
          ARRAY_AGG(
            sentiment_lexicon_score IGNORE NULLS
            ORDER BY FARM_FINGERPRINT(CAST(sentiment_lexicon_score AS STRING))
            LIMIT {_MAX_SCORES_PER_TOPIC}
          ) AS scores
        FROM `{client.project}.{dataset}.enriched_content`, UNNEST(topic_groups) AS tg
        WHERE DATE(collected_at) = @trend_date
          AND sentiment_lexicon_score IS NOT NULL
          AND sentiment_lexicon_score BETWEEN -1.0 AND 1.0
        GROUP BY market, tg
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("trend_date", "DATE", trend_date),
            ]
        )
        out: dict[tuple[str, str], list[float]] = {}
        for r in client.query(sql, job_config=job_config).result():
            scores = [float(s) for s in (r.scores or [])]
            if scores:
                out[(str(r.market), str(r.query_group))] = scores
        logger.info("Lexicon scores read for %d topics on %s", len(out), trend_date)
        return out
    except Exception as exc:  # non-fatal by contract
        logger.error("Lexicon scores read failed (non-fatal): %s", exc, exc_info=True)
        return {}


def tag_briefs_with_lexicon_scores(
    briefs_by_topic: dict[tuple[str, str], dict],
    lookup: dict[tuple[str, str], list[float]],
) -> int:
    """Tag each brief in place with its ``sentiment_lexicon_scores`` list.

    Both sides key on (market, query_group). A brief whose key is absent from
    ``lookup`` (or carries an empty list) is left untouched, so a key mismatch or
    a flag-off day degrades to no-section rather than an error. Returns the count
    of briefs that received a non-empty list.
    """
    tagged = 0
    for key, brief in briefs_by_topic.items():
        scores = lookup.get(key)
        if scores:
            brief["sentiment_lexicon_scores"] = scores
            tagged += 1
    return tagged


__all__ = [
    "fetch_lexicon_scores",
    "tag_briefs_with_lexicon_scores",
]
