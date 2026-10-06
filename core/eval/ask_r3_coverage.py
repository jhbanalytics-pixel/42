"""Read only embedding coverage snapshot for R3."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta


MARKETS = ("ZA", "NG", "KE")
WINDOW_DAYS = 7

LATEST_RUN_SQL = """SELECT run_id, run_date, status, started_at, finished_at
FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
WHERE stage = 'understand'
  AND JSON_VALUE(counts, '$.embedded') IS NOT NULL
ORDER BY started_at DESC, run_id DESC
LIMIT 1"""

COVERAGE_SQL = """WITH market_list AS (
  SELECT 'ZA' AS market
  UNION ALL SELECT 'NG'
  UNION ALL SELECT 'KE'
), eligible_posts AS (
  SELECT
    p.post_id,
    p.platform,
    p.creator_id,
    p.geo_market,
    p.geo_source
  FROM `ogilvy-trends-v2.intelligence_42_core.posts` AS p
  WHERE TRIM(CONCAT(
      COALESCE(p.text, ''),
      IF(TRIM(COALESCE(p.transcript, '')) = '', '', CONCAT('\\n', p.transcript))
    )) != ''
    AND p.post_date BETWEEN @window_start AND @window_end
    AND EXISTS (
      SELECT 1
      FROM `ogilvy-trends-v2.intelligence_42_core.post_observations` AS o
      WHERE o.post_id = p.post_id
        AND o.observed_date BETWEEN @window_start AND @window_end
    )
), creator_home_posts AS (
  SELECT DISTINCT ep.post_id, c.home_market AS market
  FROM eligible_posts AS ep
  JOIN `ogilvy-trends-v2.intelligence_42_core.creators` AS c
    ON c.platform = ep.platform
    AND c.creator_id = ep.creator_id
  WHERE c.home_market IN ('ZA', 'NG', 'KE')
), source_market_posts AS (
  SELECT DISTINCT ep.post_id, ss.source_market AS market
  FROM eligible_posts AS ep
  JOIN `ogilvy-trends-v2.intelligence_42_core.v_post_source_markets` AS vsm
    ON vsm.post_id = ep.post_id
  CROSS JOIN UNNEST(vsm.source_sightings) AS ss
  WHERE ss.obs_date BETWEEN @window_start AND @window_end
    AND ss.source_market IN ('ZA', 'NG', 'KE')
), embedding_flags AS (
  SELECT
    e.post_id,
    COUNTIF(ARRAY_LENGTH(e.embedding) > 0) > 0 AS has_any_embedding,
    COUNTIF(ARRAY_LENGTH(e.embedding) = 768) > 0 AS has_valid_embedding
  FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment` AS e
  GROUP BY e.post_id
), eligible AS (
  SELECT
    m.market,
    ep.post_id,
    ep.geo_market = m.market AS geo_market_match,
    (ep.geo_market IS NULL OR ep.geo_source = 'home_market')
      AND c.post_id IS NOT NULL AS creator_home_market_match,
    (ep.geo_market IS NULL OR ep.geo_source = 'home_market')
      AND sm.post_id IS NOT NULL AS source_sighting_match,
    COALESCE(ef.has_any_embedding, FALSE) AS has_any_embedding,
    COALESCE(ef.has_valid_embedding, FALSE) AS has_valid_embedding
  FROM eligible_posts AS ep
  CROSS JOIN market_list AS m
  LEFT JOIN creator_home_posts AS c
    ON c.post_id = ep.post_id
    AND c.market = m.market
  LEFT JOIN source_market_posts AS sm
    ON sm.post_id = ep.post_id
    AND sm.market = m.market
  LEFT JOIN embedding_flags AS ef
    ON ef.post_id = ep.post_id
  WHERE ep.geo_market = m.market
    OR (
      (ep.geo_market IS NULL OR ep.geo_source = 'home_market')
      AND (c.post_id IS NOT NULL OR sm.post_id IS NOT NULL)
    )
), counts AS (
  SELECT
    market,
    COUNT(DISTINCT post_id) AS eligible_distinct_posts,
    COUNT(DISTINCT IF(NOT has_any_embedding, post_id, NULL)) AS needs_embedding_distinct_posts,
    COUNT(DISTINCT IF(has_valid_embedding, post_id, NULL)) AS embedded_distinct_posts,
    COUNT(DISTINCT IF(has_any_embedding AND NOT has_valid_embedding, post_id, NULL))
      AS invalid_nonempty_embedding_distinct_posts,
    COUNT(DISTINCT IF(geo_market_match, post_id, NULL)) AS geo_market_match_distinct_posts,
    COUNT(DISTINCT IF(creator_home_market_match, post_id, NULL))
      AS creator_home_market_match_distinct_posts,
    COUNT(DISTINCT IF(source_sighting_match, post_id, NULL)) AS source_sighting_match_distinct_posts
  FROM eligible
  GROUP BY market
)
SELECT
  m.market,
  COALESCE(c.eligible_distinct_posts, 0) AS eligible_distinct_posts,
  COALESCE(c.needs_embedding_distinct_posts, 0) AS needs_embedding_distinct_posts,
  COALESCE(c.embedded_distinct_posts, 0) AS embedded_distinct_posts,
  COALESCE(c.invalid_nonempty_embedding_distinct_posts, 0) AS invalid_nonempty_embedding_distinct_posts,
  COALESCE(c.geo_market_match_distinct_posts, 0) AS geo_market_match_distinct_posts,
  COALESCE(c.creator_home_market_match_distinct_posts, 0) AS creator_home_market_match_distinct_posts,
  COALESCE(c.source_sighting_match_distinct_posts, 0) AS source_sighting_match_distinct_posts
FROM market_list AS m
LEFT JOIN counts AS c USING (market)
ORDER BY m.market"""

METHOD = (
    "Eligible posts are distinct post_id values with nonblank embed.sql input, post_date within the inclusive "
    "seven day retrieval window, a post_observations sighting in that same window, and the same market predicate "
    "as tvf_search_posts. The denominator includes posts already embedded. embedded_distinct_posts requires a "
    "768 element vector; needs_embedding_distinct_posts applies embed.sql's no nonempty vector condition. "
    "Eligibility route counts can overlap. source_sighting_match_distinct_posts reports source feed affiliation "
    "separately from geo_market matches."
)


def _rows(result):
    if isinstance(result, dict):
        if "rows" not in result:
            raise ValueError("missing rows")
        rows = result["rows"]
    elif result is None:
        raise ValueError("missing result")
    else:
        rows = result
    return [dict(row) for row in rows or []]


def _error_classification(err):
    name = type(err).__name__.lower()
    if any(token in name for token in ("permission", "forbidden", "unauthorized")):
        return "permission_error"
    if any(token in name for token in ("deadline", "timeout")):
        return "timeout"
    if any(token in name for token in ("notfound", "not_found")):
        return "resource_not_found"
    if any(token in name for token in ("badrequest", "invalidargument", "syntax")):
        return "query_error"
    return "warehouse_error"


def _read(wiring, modules, query_name, sql, params):
    try:
        modules.sql.check_sql(sql)
    except Exception as err:
        return None, {"read": query_name, "classification": "query_guard_rejected",
                      "error_class": type(err).__name__}
    try:
        result = wiring.execute(sql, params, modules.sql.MAX_BYTES_BILLED)
    except Exception as err:
        return None, {"read": query_name, "classification": _error_classification(err),
                      "error_class": type(err).__name__}
    try:
        return _rows(result), None
    except Exception as err:
        return None, {"read": query_name, "classification": "result_shape_invalid",
                      "error_class": type(err).__name__}


def _iso(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _count(row, field):
    value = row[field]
    if isinstance(value, bool):
        raise ValueError("boolean count")
    count = int(value)
    if count < 0 or str(count) != str(value):
        raise ValueError("invalid count")
    return count


def _empty_market():
    return {
        "eligible_distinct_posts": None,
        "needs_embedding_distinct_posts": None,
        "embedded_distinct_posts": None,
        "invalid_nonempty_embedding_distinct_posts": None,
        "eligibility_routes": {
            "geo_market_match_distinct_posts": None,
            "creator_home_market_match_distinct_posts": None,
            "source_sighting_match_distinct_posts": None,
        },
    }


def read_embedding_coverage(wiring, modules):
    """Read recent embedding coverage without invoking a model or changing warehouse state."""
    errors = []
    markets = {market: _empty_market() for market in MARKETS}
    query_sha256 = {
        "latest_run": hashlib.sha256(LATEST_RUN_SQL.encode("utf-8")).hexdigest(),
        "coverage": hashlib.sha256(COVERAGE_SQL.encode("utf-8")).hexdigest(),
    }
    try:
        now = wiring.now()
        sast = modules.staging.SAST
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("timezone required")
        as_of_sast = now.astimezone(sast)
        as_of = as_of_sast.date()
    except Exception as err:
        errors.append({"read": "window", "classification": "clock_unavailable",
                       "error_class": type(err).__name__})
        return {
            "status": "read_error",
            "as_of_date": None,
            "as_of_sast": None,
            "window_start_date": None,
            "window_end_date": None,
            "window_days": WINDOW_DAYS,
            "window_basis": "inclusive SAST calendar dates",
            "current_day_partial": True,
            "latest_embedding_run": None,
            "markets": markets,
            "method": METHOD,
            "query_sha256": query_sha256,
            "errors": errors,
        }

    window_start = as_of - timedelta(days=WINDOW_DAYS - 1)
    window_end = as_of
    run_rows, run_error = _read(wiring, modules, "latest_run", LATEST_RUN_SQL, {})
    if run_error:
        errors.append(run_error)
        latest_run = None
    elif not run_rows:
        latest_run = None
    else:
        row = run_rows[0]
        if not row.get("run_id") or row.get("run_date") is None:
            errors.append({"read": "latest_run", "classification": "result_shape_invalid",
                           "error_class": "ValueError"})
            latest_run = None
        else:
            latest_run = {
                "run_id": row["run_id"],
                "run_date": _iso(row["run_date"]),
                "status": row.get("status"),
                "started_at": _iso(row.get("started_at")),
                "finished_at": _iso(row.get("finished_at")),
            }

    coverage_rows, coverage_error = _read(
        wiring, modules, "coverage", COVERAGE_SQL,
        {"window_start": window_start, "window_end": window_end},
    )
    if coverage_error:
        errors.append(coverage_error)
    else:
        try:
            by_market = {row["market"]: row for row in coverage_rows}
            if set(by_market) != set(MARKETS) or len(coverage_rows) != len(MARKETS):
                raise ValueError("market rows incomplete")
            for market in MARKETS:
                row = by_market[market]
                routes = {
                    "geo_market_match_distinct_posts": _count(row, "geo_market_match_distinct_posts"),
                    "creator_home_market_match_distinct_posts": _count(
                        row, "creator_home_market_match_distinct_posts"),
                    "source_sighting_match_distinct_posts": _count(row, "source_sighting_match_distinct_posts"),
                }
                markets[market] = {
                    "eligible_distinct_posts": _count(row, "eligible_distinct_posts"),
                    "needs_embedding_distinct_posts": _count(row, "needs_embedding_distinct_posts"),
                    "embedded_distinct_posts": _count(row, "embedded_distinct_posts"),
                    "invalid_nonempty_embedding_distinct_posts": _count(
                        row, "invalid_nonempty_embedding_distinct_posts"),
                    "eligibility_routes": routes,
                }
        except Exception as err:
            errors.append({"read": "coverage", "classification": "result_shape_invalid",
                           "error_class": type(err).__name__})
            markets = {market: _empty_market() for market in MARKETS}

    status = "ok" if not errors else "partial" if len(errors) < 2 else "read_error"
    return {
        "status": status,
        "as_of_date": as_of.isoformat(),
        "as_of_sast": as_of_sast.isoformat(timespec="seconds"),
        "window_start_date": window_start.isoformat(),
        "window_end_date": window_end.isoformat(),
        "window_days": WINDOW_DAYS,
        "window_basis": "inclusive SAST calendar dates",
        "current_day_partial": True,
        "latest_embedding_run": latest_run,
        "markets": markets,
        "method": METHOD,
        "query_sha256": query_sha256,
        "errors": errors,
    }
