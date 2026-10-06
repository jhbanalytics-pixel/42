"""Tier-upgrade detection + enrichment sampling for the daily email digest.

Runs immediately after scripts/run_rss_now.py writes the day's trend_scores
via MERGE. Queries BigQuery for each (market, query_group)'s most recent
prior score, picks out tier upgrades, samples enriched content and GKG
entities, and fires a single email digest covering every market.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from google.cloud import bigquery as bq

from src.alerts.email_digest import (
    HealedIssue,
    IssueFlag,
    TrendAlert,
    TrendMover,
    _mailer_v2_enabled,
    build_alert,
    score_to_tier,
    send_email_digest,
    tier_upgraded,
)
from src.utils.bigquery import get_client, get_dataset
from src.utils.config_loader import load_scoring
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

# Identifier whitelists. Bound query parameters cannot stand in for
# table or column names in BigQuery SQL, so any identifier interpolated
# into a query must come from one of these allowlists.
_ALLOWED_MARKETS: frozenset[str] = frozenset({"za", "ng", "ke"})
_ALLOWED_ENTITY_COLUMNS: frozenset[str] = frozenset({"v2persons", "v2orgs"})


def _validate_market(market: str) -> str:
    if market not in _ALLOWED_MARKETS:
        raise ValueError(f"market {market!r} not in {_ALLOWED_MARKETS}")
    return market


def _validate_entity_column(column: str) -> str:
    if column not in _ALLOWED_ENTITY_COLUMNS:
        raise ValueError(f"entity column {column!r} not in {_ALLOWED_ENTITY_COLUMNS}")
    return column


def _query_prior_scores(
    score_rows: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Look up each (market, query_group)'s most recent prior trend_score row.

    Scans the last 30 days strictly before today to keep the query cheap.
    Missing pairs are treated as first-time scoring (no prior row).
    """
    if not score_rows:
        return {}

    pairs = sorted({(str(r["market"]), str(r["query_group"])) for r in score_rows})
    if not pairs:
        return {}

    client = get_client()
    dataset = get_dataset()
    target = f"`{client.project}.{dataset}.trend_scores`"

    # Bind (market, query_group) pairs as ARRAY parameters and join via
    # UNNEST. Avoids SQL string interpolation of caller-supplied values.
    markets = [_validate_market(m) for m, _ in pairs]
    query_groups = [str(q) for _, q in pairs]
    sql = f"""
    WITH pair_keys AS (
      SELECT m AS market, g AS query_group
      FROM UNNEST(@markets) AS m WITH OFFSET pos
      JOIN UNNEST(@query_groups) AS g WITH OFFSET pos2 ON pos = pos2
    ),
    prior AS (
      SELECT t.market, t.query_group, t.trend_score, t.trend_date,
             ROW_NUMBER() OVER (
               PARTITION BY t.market, t.query_group ORDER BY t.trend_date DESC
             ) AS rn
      FROM {target} t
      JOIN pair_keys p USING (market, query_group)
      WHERE t.trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
        AND t.trend_date < CURRENT_DATE()
    )
    SELECT market, query_group, trend_score FROM prior WHERE rn = 1
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ArrayQueryParameter("markets", "STRING", markets),
            bq.ArrayQueryParameter("query_groups", "STRING", query_groups),
        ]
    )
    out: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        for r in client.query(sql, job_config=job_config).result():
            out[(r.market, r.query_group)] = {
                "market": r.market,
                "query_group": r.query_group,
                "trend_score": float(r.trend_score or 0.0),
            }
    except Exception as exc:
        logger.warning("prior-score lookup failed; treating all pairs as first-time: %s", exc)
    return out


_ENTITY_SPLIT = re.compile(r";")


def _top_entities_for(
    market: str, query_group: str, persons_orgs: str = "persons", limit: int = 5
) -> list[str]:
    """Count most-frequent GKG Persons or Organizations across today's enriched
    rows in the given (market, query_group). Entities come in as
    'Name,char_offset;Name,char_offset;...'. Offsets dropped; names trimmed.
    """
    client = get_client()
    dataset = get_dataset()
    # Whitelist the column identifier so it can safely be interpolated;
    # market and query_group go through bound parameters.
    column = _validate_entity_column("v2persons" if persons_orgs == "persons" else "v2orgs")
    market = _validate_market(market)
    sql = f"""
    SELECT {column} AS raw
    FROM `{client.project}.{dataset}.enriched_content`
    WHERE DATE(collected_at) = CURRENT_DATE()
      AND market = @market
      AND query_group = @query_group
      AND {column} IS NOT NULL
      AND LENGTH({column}) > 0
    LIMIT 500
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("market", "STRING", market),
            bq.ScalarQueryParameter("query_group", "STRING", query_group),
        ]
    )
    counts: Counter[str] = Counter()
    try:
        for row in client.query(sql, job_config=job_config).result():
            raw = row.raw or ""
            for item in _ENTITY_SPLIT.split(raw):
                name = item.split(",", 1)[0].strip()
                if len(name) >= 3:
                    counts[name] += 1
    except Exception as exc:
        logger.warning("entity lookup failed for %s/%s: %s", market, query_group, exc)
        return []
    return [name for name, _ in counts.most_common(limit)]


def _sample_enriched(market: str, query_group: str, n: int = 3) -> list[dict[str, str]]:
    """Pull a small representative sample of today's enriched rows.

    Prefers rows with a non-empty title and nonzero engagement; falls back to
    any row with a URL so the digest always has clickable links.
    """
    client = get_client()
    dataset = get_dataset()
    market = _validate_market(market)
    sql = f"""
    SELECT title, text, url, source
    FROM `{client.project}.{dataset}.enriched_content`
    WHERE DATE(collected_at) = CURRENT_DATE()
      AND market = @market
      AND query_group = @query_group
      AND url IS NOT NULL AND LENGTH(url) > 0
    ORDER BY (
      CASE WHEN title IS NOT NULL AND LENGTH(title) > 0 THEN 2 ELSE 0 END
      + CASE WHEN engagement_total > 0 THEN 1 ELSE 0 END
    ) DESC, RAND()
    LIMIT @lim
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("market", "STRING", market),
            bq.ScalarQueryParameter("query_group", "STRING", query_group),
            bq.ScalarQueryParameter("lim", "INT64", int(n)),
        ]
    )
    out: list[dict[str, str]] = []
    try:
        for r in client.query(sql, job_config=job_config).result():
            out.append(
                {
                    "title": r.title or "",
                    "text": r.text or "",
                    "url": r.url or "",
                    "source": r.source or "",
                }
            )
    except Exception as exc:
        logger.warning("sample lookup failed for %s/%s: %s", market, query_group, exc)
    return out


MOVER_DELTA_MIN = 0.01
MOVERS_PER_MARKET = 3


def _should_skip_quiet_day(
    total_upgrades: int,
    total_movers: int,
    issues: list,
    healed: list,
    briefs_by_topic: dict | None,
) -> bool:
    """True when there is nothing worth sending.

    The v1 ops digest skips a fully quiet day (no upgrades, movers, issues, or
    heals). The v2 PULSE leads on the cross-market verdict and the per-topic
    briefs, so it has substance even with zero tier upgrades; skip it only when
    there are no briefs to carry either. Without this carve-out a flat day (a
    quiet Sunday) would suppress the whole PULSE despite a full set of briefs.
    """
    if total_upgrades or total_movers or issues or healed:
        return False
    # The v2 PULSE still has the verdict + briefs to send on a quiet day; only
    # skip when there is no v2 content (v1, or no briefs).
    return not (_mailer_v2_enabled() and briefs_by_topic)


def send_daily_digest(
    score_rows: list[dict[str, Any]],
    stats: dict[str, Any] | None = None,
    *,
    issues: list[IssueFlag] | None = None,
    healed: list[HealedIssue] | None = None,
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]] | None = None,
    daily_summary: dict[str, Any] | None = None,
) -> tuple[int, int, int, str]:
    """Build and send the daily email digest covering all markets.

    The email always carries substance when sent. It surfaces in order: tier
    upgrades vs the most recent prior score, top movers (biggest abs score
    delta per market excluding upgrades), any pipeline issues the caller
    flagged, and a stats footer. Only skips delivery when there are zero
    upgrades, zero meaningful movers, and zero issues (i.e. day one with no
    prior data and a clean run), which is a degenerate case.

    Returns (markets_with_upgrades, total_upgrades, total_movers, status)
    where status is 'sent', 'dry_run', 'send_failed', or
    'skipped_nothing_notable'. 'dry_run' (EMAIL_ALERTS_ENABLED != true) is a
    deliberate no-send, not a failure, so the marker row reads healthy.
    """
    scoring = load_scoring()
    weights = scoring.get("weights", {})
    # Fallback only when scoring.yaml lacks thresholds; keep it in lockstep with
    # the recalibrated live floors (email_digest.score_to_tier) so a config gap
    # never tiers the alert path differently from the digest.
    thresholds = scoring.get("thresholds", {"trending": 0.45, "emerging": 0.30, "monitoring": 0.18})
    prior = _query_prior_scores(score_rows)

    upgrades_by_market: dict[str, list[TrendAlert]] = {}
    upgrade_keys: set[tuple[str, str]] = set()
    mover_candidates: dict[str, list[tuple[float, dict[str, Any], dict[str, Any]]]] = {}

    for row in score_rows:
        market = str(row.get("market", ""))
        qg = str(row.get("query_group", ""))
        trend_score = float(row.get("trend_score") or 0.0)
        tier = score_to_tier(trend_score, thresholds)
        prior_row = prior.get((market, qg))
        prior_tier = (
            score_to_tier(float(prior_row["trend_score"]), thresholds) if prior_row else None
        )

        if tier_upgraded(tier, prior_tier):
            samples = _sample_enriched(market, qg, n=3)
            persons = _top_entities_for(market, qg, "persons", limit=5)
            orgs = _top_entities_for(market, qg, "orgs", limit=5)
            alert = build_alert(row, prior_row, weights, thresholds, samples, persons, orgs)
            upgrades_by_market.setdefault(market, []).append(alert)
            upgrade_keys.add((market, qg))
            logger.info(
                "Tier upgrade queued: %s/%s %.3f %s -> %s",
                market,
                qg,
                trend_score,
                prior_tier or "NEW",
                tier,
            )
            continue

        if prior_row is None:
            continue
        delta = trend_score - float(prior_row["trend_score"])
        if abs(delta) < MOVER_DELTA_MIN:
            continue
        mover_candidates.setdefault(market, []).append((delta, row, prior_row))

    # Pick top N movers per market by absolute delta (excluding upgrade rows),
    # then enrich each with headline + entities + tone.
    movers_by_market: dict[str, list[TrendMover]] = {}
    for market, candidates in mover_candidates.items():
        candidates.sort(key=lambda t: abs(t[0]), reverse=True)
        for delta, row, prior_row in candidates[:MOVERS_PER_MARKET]:
            qg = str(row.get("query_group", ""))
            if (market, qg) in upgrade_keys:
                continue
            trend_score = float(row.get("trend_score") or 0.0)
            prior_score = float(prior_row.get("trend_score") or 0.0)
            tier = score_to_tier(trend_score, thresholds)
            prior_tier = score_to_tier(prior_score, thresholds)
            samples = _sample_enriched(market, qg, n=3)
            persons = _top_entities_for(market, qg, "persons", limit=4)
            orgs = _top_entities_for(market, qg, "orgs", limit=4)
            tone_avg = (
                float(row.get("tone_score") or 0.0) if (row.get("tone_rows") or 0) > 0 else None
            )
            mover = TrendMover(
                market=market,
                query_group=qg,
                current_score=trend_score,
                prior_score=prior_score,
                delta=delta,
                tier=tier,
                prior_tier=prior_tier,
                item_count=int(row.get("item_count") or 0),
                source_diversity=int(row.get("source_diversity") or 0),
                samples=samples,
                top_persons=persons,
                top_orgs=orgs,
                tone_avg=tone_avg,
            )
            movers_by_market.setdefault(market, []).append(mover)
            logger.info(
                "Mover queued: %s/%s %.3f -> %.3f (delta %.3f)",
                market,
                qg,
                prior_score,
                trend_score,
                delta,
            )

    total_upgrades = sum(len(v) for v in upgrades_by_market.values())
    total_movers = sum(len(v) for v in movers_by_market.values())
    issues = issues or []
    healed = healed or []

    if _should_skip_quiet_day(total_upgrades, total_movers, issues, healed, briefs_by_topic):
        logger.info(
            "Nothing notable and no v2 PULSE content. Skipping email "
            "(no upgrades, movers, issues, heals, or briefs)."
        )
        return (0, 0, 0, "skipped_nothing_notable")

    send_status = send_email_digest(
        upgrades_by_market,
        # UTC date so the digest date matches the UTC trend_date in BQ, not the
        # host's local date.today().
        run_date=datetime.now(UTC).date(),
        movers_by_market=movers_by_market,
        issues=issues,
        stats=stats,
        healed=healed,
        briefs_by_topic=briefs_by_topic,
        daily_summary=daily_summary,
    )
    # send_email_digest names its exact outcome. Pass a deliberate dry run
    # through as "dry_run" (a non-failure) so it is not recorded as a send
    # failure; collapse the real no-email outcomes (missing creds, missing
    # recipients, SMTP/auth blip) to "send_failed". A legacy bool False (older
    # caller or a test stub) also maps to "send_failed" via the else branch.
    if send_status == "sent":
        status = "sent"
    elif send_status == "dry_run":
        status = "dry_run"
    else:
        status = "send_failed"
    logger.info(
        "Daily digest result: upgrades=%d movers=%d issues=%d status=%s",
        total_upgrades,
        total_movers,
        len(issues),
        status,
    )
    return (len(upgrades_by_market), total_upgrades, total_movers, status)


# Backwards-compatible alias. Older callers (and the scheduled task) import
# `alerts_for_upgrades`; keep it pointing at the new function so upgrading
# the call site is optional. Signature changed: now 4-tuple not 3-tuple.
alerts_for_upgrades = send_daily_digest


__all__ = ["alerts_for_upgrades", "send_daily_digest"]
