/*
  Collection window rows for the funded pilot tables raw_content,
  enriched_content and pipeline_runs. One output row per market and route over
  the union of the expected routes and the routes observed in the window. A
  route is the pipeline connector key that pipeline_runs errors start with.
  Raw rows carry feed names, domains and channel titles as their source, so a
  row's route is resolved from the declared source map first, then from the
  declared platform map, and a row neither resolves stays in the window under
  the route 'unresolved', with the source values it carried, rather than being
  dropped. A route that recorded errors in pipeline_runs gets its row even
  when it is neither expected nor observed. Errors recorded against the
  pipeline itself, or without a connector key, are counted on the market level
  route 'pipeline', which never holds rows and is never expected. Every
  declared stratum has an expected route and every market pipeline_runs
  reports rows for is a declared stratum, so pipeline rows with no readable
  raw rows always surface as reader_mismatch on a row of that market. Per
  route, pipeline_route_rows is the sum of that connector's own row column in
  pipeline_runs (the explicit mapping is route_pipeline_rows below; NULL for a
  connector with no column, and never for sub feeds riding inside a parent).
  The run fills those columns with fetched rows before the write, so a
  positive count with no raw rows is a reader mismatch on the route; a
  connector whose fetched rows are all dropped by cross connector dedup before
  the write reads the same way, loudly rather than as a silent zero. The
  known case is youtube_scrape, whose rows the youtube API rows can remove
  entirely by dedup: its reader_mismatch then names that drop, not a broken
  reader, and is checked against the dedup log before any conclusion. Rows are
  ranked by market and route and the final select keeps at most one row more
  than @query_limit, so a reader that receives more than the limit knows the
  output was cut. Every total is computed here, in SQL, over one explicit
  half open window on collected_at (pipeline_runs on started_at) and one explicit set of
  market strata. Distinct identities are counted over the whole window;
  nothing here adds per day distinct counts together. Native identities are
  platform plus native_id; a row without native_id but with a url carries an
  inferred url identity, counted apart; a row with neither is counted as a row
  without identity. The pilot writes each enriched row from its raw row, so an
  enriched row and its raw row share id. Reviewed as text; never executed by
  the unit suite. Read only.

  Parameters:
  @window_start     TIMESTAMP     inclusive start of the collection window
  @window_end       TIMESTAMP     exclusive end of the collection window
  @market_strata    ARRAY<STRING> the markets reported as separate strata
  @source_routes    ARRAY<STRUCT<source STRING, route STRING>> route of each fixed raw source value
  @platform_routes  ARRAY<STRUCT<platform STRING, route STRING>> route of a platform whose source is not listed
  @expected_sources ARRAY<STRUCT<market STRING, route STRING>> every route the window must account for
  @known_quiet      ARRAY<STRUCT<market STRING, route STRING>> routes declared quiet, a subset of the expected routes
  @query_limit      INT64         maximum rows returned (one per market and route)
*/

ASSERT @window_start < @window_end AS 'window_is_ordered';
ASSERT ARRAY_LENGTH(@market_strata) > 0 AS 'market_strata_are_declared';
ASSERT @query_limit > 0 AS 'query_limit_is_positive';

ASSERT (
  SELECT COUNT(*) = COUNT(DISTINCT source)
  FROM UNNEST(@source_routes)
) AND (
  SELECT COUNT(*) = COUNT(DISTINCT platform)
  FROM UNNEST(@platform_routes)
) AS 'route_maps_are_unambiguous';

ASSERT (
  SELECT COUNT(*)
  FROM (
    SELECT route FROM UNNEST(@source_routes)
    UNION ALL
    SELECT route FROM UNNEST(@platform_routes)
    UNION ALL
    SELECT route FROM UNNEST(@expected_sources)
    UNION ALL
    SELECT route FROM UNNEST(@known_quiet)
  )
  WHERE route IS NULL OR route IN ('', 'unresolved', 'pipeline')
) = 0 AS 'declared_routes_are_named';

ASSERT (
  SELECT COUNT(*)
  FROM (
    SELECT route FROM UNNEST(@expected_sources)
    UNION ALL
    SELECT route FROM UNNEST(@known_quiet)
  )
  WHERE NOT REGEXP_CONTAINS(route, r'^[a-z][a-z0-9_]*$')
) = 0 AS 'declared_routes_are_connector_keys';

ASSERT (
  SELECT COUNT(*)
  FROM UNNEST(@known_quiet) AS quiet
  WHERE NOT EXISTS (
    SELECT 1
    FROM UNNEST(@expected_sources) AS expected
    WHERE expected.market = quiet.market
      AND expected.route = quiet.route
  )
) = 0 AS 'known_quiet_routes_are_expected';

ASSERT (
  SELECT COUNT(*)
  FROM UNNEST(@expected_sources) AS expected
  WHERE expected.market IS NULL
    OR expected.market NOT IN UNNEST(@market_strata)
) = 0 AS 'expected_markets_are_declared_strata';

ASSERT (
  SELECT COUNT(*)
  FROM UNNEST(@market_strata) AS stratum
  WHERE NOT EXISTS (
    SELECT 1
    FROM UNNEST(@expected_sources) AS expected
    WHERE expected.market = stratum
  )
) = 0 AS 'every_stratum_has_an_expected_route';

ASSERT (
  SELECT COUNT(*)
  FROM `ogilvy-trends-v2.trends_v2_staging.raw_content`
  WHERE collected_at >= @window_start
    AND collected_at < @window_end
    AND (market IS NULL OR market NOT IN UNNEST(@market_strata))
) = 0 AS 'every_market_is_a_declared_stratum';

ASSERT (
  SELECT COUNT(*)
  FROM `ogilvy-trends-v2.trends_v2_staging.pipeline_runs`
  WHERE started_at >= @window_start
    AND started_at < @window_end
    AND IFNULL(total_rows, 0) > 0
    AND (market IS NULL OR market NOT IN UNNEST(@market_strata))
) = 0 AS 'every_pipeline_market_is_a_declared_stratum';

ASSERT (
  SELECT COUNT(*)
  FROM (
    SELECT market, route
    FROM UNNEST(@expected_sources)
    UNION DISTINCT
    SELECT
      raw.market,
      COALESCE(
        (SELECT source_route.route FROM UNNEST(@source_routes) AS source_route
          WHERE source_route.source = raw.source),
        (SELECT platform_route.route FROM UNNEST(@platform_routes) AS platform_route
          WHERE platform_route.platform = raw.platform),
        'unresolved'
      ) AS route
    FROM `ogilvy-trends-v2.trends_v2_staging.raw_content` AS raw
    WHERE raw.collected_at >= @window_start
      AND raw.collected_at < @window_end
    UNION DISTINCT
    SELECT
      run.market,
      IFNULL(REGEXP_EXTRACT(run_error, r'^([a-z][a-z0-9_]*):'), 'pipeline') AS route
    FROM `ogilvy-trends-v2.trends_v2_staging.pipeline_runs` AS run,
      UNNEST(run.errors) AS run_error
    WHERE run.started_at >= @window_start
      AND run.started_at < @window_end
      AND run.market IN UNNEST(@market_strata)
    UNION DISTINCT
    SELECT run.market, route_count.route
    FROM `ogilvy-trends-v2.trends_v2_staging.pipeline_runs` AS run,
      UNNEST([
          STRUCT('rss' AS route, run.rss_rows AS route_rows),
          STRUCT('youtube' AS route, run.youtube_rows AS route_rows),
          STRUCT('gdelt' AS route, run.gdelt_rows AS route_rows),
          STRUCT('ensemble' AS route, run.ensemble_rows AS route_rows),
          STRUCT('bigquery_trends' AS route, run.bigquery_trends_rows AS route_rows),
          STRUCT('reddit' AS route, run.reddit_rows AS route_rows),
          STRUCT('apple_music' AS route, run.apple_music_rows AS route_rows),
          STRUCT('wikipedia' AS route, run.wikipedia_rows AS route_rows),
          STRUCT('bluesky' AS route, run.bluesky_rows AS route_rows),
          STRUCT('google_trends_rss' AS route, run.google_trends_rss_rows AS route_rows),
          STRUCT('app_charts' AS route, run.app_charts_rows AS route_rows),
          STRUCT('audiomack' AS route, run.audiomack_rows AS route_rows),
          STRUCT('cloudflare_radar' AS route, run.cloudflare_radar_rows AS route_rows),
          STRUCT('youtube_scrape' AS route, run.youtube_scrape_rows AS route_rows),
          STRUCT('socialcrawl' AS route, run.socialcrawl_rows AS route_rows)
        ]) AS route_count
    WHERE run.started_at >= @window_start
      AND run.started_at < @window_end
      AND run.market IN UNNEST(@market_strata)
    GROUP BY run.market, route_count.route
    HAVING SUM(route_count.route_rows) > 0
  )
) <= @query_limit AS 'output_fits_the_query_limit';

WITH raw_window AS (
  SELECT
    raw.market,
    raw.source,
    COALESCE(
      (SELECT source_route.route FROM UNNEST(@source_routes) AS source_route
        WHERE source_route.source = raw.source),
      (SELECT platform_route.route FROM UNNEST(@platform_routes) AS platform_route
        WHERE platform_route.platform = raw.platform),
      'unresolved'
    ) AS route,
    raw.platform,
    raw.native_id,
    raw.url,
    raw.geo_method_id,
    raw.published_at,
    raw.collected_at,
    CASE
      WHEN IFNULL(raw.native_id, '') != '' THEN CONCAT('native:', raw.platform, ':', raw.native_id)
      WHEN IFNULL(raw.url, '') != '' THEN CONCAT('url:', raw.url)
    END AS identity
  FROM `ogilvy-trends-v2.trends_v2_staging.raw_content` AS raw
  WHERE raw.collected_at >= @window_start
    AND raw.collected_at < @window_end
    AND raw.market IN UNNEST(@market_strata)
),
raw_routes AS (
  SELECT
    market,
    route,
    COUNT(*) AS physical_rows,
    COUNT(DISTINCT IF(IFNULL(native_id, '') != '',
      CONCAT(platform, ':', native_id), NULL)) AS distinct_native_observations,
    COUNT(DISTINCT IF(IFNULL(native_id, '') = '' AND IFNULL(url, '') != '',
      url, NULL)) AS inferred_identity_observations,
    COUNTIF(identity IS NULL) AS rows_without_identity,
    COUNTIF(IFNULL(geo_method_id, '') != '') AS rows_with_geo_method,
    MAX(published_at) AS newest_published_at,
    MAX(collected_at) AS newest_collected_at,
    COUNTIF(published_at IS NULL) AS rows_without_published_at
  FROM raw_window
  GROUP BY market, route
),
/* The source values no map resolved, so the unresolved route shows what it holds. */
unresolved_values AS (
  SELECT
    market,
    ARRAY_AGG(DISTINCT IFNULL(source, '') ORDER BY IFNULL(source, '')) AS unresolved_source_values
  FROM raw_window
  WHERE route = 'unresolved'
  GROUP BY market
),
/* Every raw row of the window by id, whatever its market, so a reassigned
   enriched row still finds the market its raw row was collected under. */
raw_markets AS (
  SELECT id, market
  FROM `ogilvy-trends-v2.trends_v2_staging.raw_content`
  WHERE collected_at >= @window_start
    AND collected_at < @window_end
),
enriched_window AS (
  SELECT
    enriched.id,
    enriched.market,
    COALESCE(
      (SELECT source_route.route FROM UNNEST(@source_routes) AS source_route
        WHERE source_route.source = enriched.source),
      (SELECT platform_route.route FROM UNNEST(@platform_routes) AS platform_route
        WHERE platform_route.platform = enriched.platform),
      'unresolved'
    ) AS route,
    enriched.topic_groups,
    CASE
      WHEN IFNULL(enriched.native_id, '') != ''
        THEN CONCAT('native:', enriched.platform, ':', enriched.native_id)
      WHEN IFNULL(enriched.url, '') != '' THEN CONCAT('url:', enriched.url)
    END AS identity
  FROM `ogilvy-trends-v2.trends_v2_staging.enriched_content` AS enriched
  WHERE enriched.collected_at >= @window_start
    AND enriched.collected_at < @window_end
    AND enriched.market IN UNNEST(@market_strata)
),
qualified AS (
  SELECT
    market,
    route,
    identity,
    LOGICAL_OR(EXISTS(
      SELECT 1
      FROM UNNEST(topic_groups) AS topic_group
      WHERE LOWER(TRIM(topic_group)) NOT IN ('', 'other')
    )) AS classified
  FROM enriched_window
  WHERE identity IS NOT NULL
  GROUP BY market, route, identity
),
identity_routes AS (
  SELECT market, identity, COUNT(DISTINCT route) AS providing_routes
  FROM qualified
  GROUP BY market, identity
),
qualified_routes AS (
  SELECT
    qualified.market,
    qualified.route,
    COUNT(DISTINCT qualified.identity) AS qualified_observations,
    COUNT(DISTINCT IF(qualified.classified, qualified.identity, NULL)) AS classified_observations,
    COUNT(DISTINCT IF(identity_routes.providing_routes = 1,
      qualified.identity, NULL)) AS unique_qualified_observations
  FROM qualified
  JOIN identity_routes
    ON identity_routes.market = qualified.market
    AND identity_routes.identity = qualified.identity
  GROUP BY qualified.market, qualified.route
),
reassigned_routes AS (
  SELECT
    enriched_window.market,
    enriched_window.route,
    COUNTIF(raw_markets.market IS DISTINCT FROM enriched_window.market) AS reassigned_market_rows
  FROM enriched_window
  JOIN raw_markets
    ON raw_markets.id = enriched_window.id
  GROUP BY enriched_window.market, enriched_window.route
),
run_window AS (
  SELECT
    market,
    total_rows,
    errors,
    rss_rows,
    youtube_rows,
    gdelt_rows,
    ensemble_rows,
    bigquery_trends_rows,
    reddit_rows,
    apple_music_rows,
    wikipedia_rows,
    bluesky_rows,
    google_trends_rss_rows,
    app_charts_rows,
    audiomack_rows,
    cloudflare_radar_rows,
    youtube_scrape_rows,
    socialcrawl_rows
  FROM `ogilvy-trends-v2.trends_v2_staging.pipeline_runs`
  WHERE started_at >= @window_start
    AND started_at < @window_end
    AND market IN UNNEST(@market_strata)
),
market_pipeline AS (
  SELECT market, IFNULL(SUM(total_rows), 0) AS pipeline_reported_rows
  FROM run_window
  GROUP BY market
),
/* One row per market and connector key from each connector's own row column.
   Connector keys without a column (spotify, semrush, pulsar) have no entry
   and read NULL. Sub feed columns riding inside a parent connector are not
   routes and are not mapped. */
route_pipeline_rows AS (
  SELECT market, route, SUM(route_rows) AS pipeline_route_rows
  FROM (
    SELECT run_window.market, route_count.route, route_count.route_rows
    FROM run_window, UNNEST([
        STRUCT('rss' AS route, rss_rows AS route_rows),
        STRUCT('youtube' AS route, youtube_rows AS route_rows),
        STRUCT('gdelt' AS route, gdelt_rows AS route_rows),
        STRUCT('ensemble' AS route, ensemble_rows AS route_rows),
        STRUCT('bigquery_trends' AS route, bigquery_trends_rows AS route_rows),
        STRUCT('reddit' AS route, reddit_rows AS route_rows),
        STRUCT('apple_music' AS route, apple_music_rows AS route_rows),
        STRUCT('wikipedia' AS route, wikipedia_rows AS route_rows),
        STRUCT('bluesky' AS route, bluesky_rows AS route_rows),
        STRUCT('google_trends_rss' AS route, google_trends_rss_rows AS route_rows),
        STRUCT('app_charts' AS route, app_charts_rows AS route_rows),
        STRUCT('audiomack' AS route, audiomack_rows AS route_rows),
        STRUCT('cloudflare_radar' AS route, cloudflare_radar_rows AS route_rows),
        STRUCT('youtube_scrape' AS route, youtube_scrape_rows AS route_rows),
        STRUCT('socialcrawl' AS route, socialcrawl_rows AS route_rows)
      ]) AS route_count
  )
  GROUP BY market, route
),
run_errors AS (
  SELECT run_window.market, run_error
  FROM run_window, UNNEST(run_window.errors) AS run_error
),
/* The route each recorded error names: its connector key prefix, or the
   market level route when the prefix is 'pipeline' or no connector key. */
error_routes AS (
  SELECT DISTINCT
    market,
    IFNULL(REGEXP_EXTRACT(run_error, r'^([a-z][a-z0-9_]*):'), 'pipeline') AS route
  FROM run_errors
),
routes AS (
  SELECT market, route
  FROM UNNEST(@expected_sources)
  UNION DISTINCT
  SELECT market, route
  FROM raw_routes
  UNION DISTINCT
  SELECT market, route
  FROM error_routes
  UNION DISTINCT
  SELECT market, route
  FROM route_pipeline_rows
  WHERE pipeline_route_rows > 0
),
failures AS (
  SELECT routes.market, routes.route, COUNT(*) AS recorded_failures
  FROM routes
  JOIN run_errors
    ON run_errors.market = routes.market
    AND (
      STARTS_WITH(run_errors.run_error, CONCAT(routes.route, ':'))
      OR (
        routes.route = 'pipeline'
        AND NOT REGEXP_CONTAINS(run_errors.run_error, r'^[a-z][a-z0-9_]*:')
      )
    )
  GROUP BY routes.market, routes.route
),
joined AS (
  SELECT
    routes.market,
    routes.route,
    EXISTS(
      SELECT 1
      FROM UNNEST(@expected_sources) AS expected
      WHERE expected.market = routes.market
        AND expected.route = routes.route
    ) AS expected,
    EXISTS(
      SELECT 1
      FROM UNNEST(@known_quiet) AS quiet
      WHERE quiet.market = routes.market
        AND quiet.route = routes.route
    ) AS declared_quiet,
    IFNULL(raw_routes.physical_rows, 0) AS physical_rows,
    IFNULL(raw_routes.distinct_native_observations, 0) AS distinct_native_observations,
    IFNULL(raw_routes.inferred_identity_observations, 0) AS inferred_identity_observations,
    IFNULL(raw_routes.rows_without_identity, 0) AS rows_without_identity,
    IFNULL(qualified_routes.qualified_observations, 0) AS qualified_observations,
    IFNULL(qualified_routes.classified_observations, 0) AS classified_observations,
    IFNULL(qualified_routes.unique_qualified_observations, 0) AS unique_qualified_observations,
    IFNULL(reassigned_routes.reassigned_market_rows, 0) AS reassigned_market_rows,
    IFNULL(raw_routes.rows_with_geo_method, 0) AS rows_with_geo_method,
    raw_routes.newest_published_at,
    raw_routes.newest_collected_at,
    IFNULL(raw_routes.rows_without_published_at, 0) AS rows_without_published_at,
    IFNULL(failures.recorded_failures, 0) AS recorded_failures,
    IFNULL(market_pipeline.pipeline_reported_rows, 0) AS pipeline_reported_rows,
    route_pipeline_rows.pipeline_route_rows,
    IFNULL(unresolved_values.unresolved_source_values, []) AS unresolved_source_values
  FROM routes
  LEFT JOIN raw_routes
    ON raw_routes.market = routes.market AND raw_routes.route = routes.route
  LEFT JOIN qualified_routes
    ON qualified_routes.market = routes.market AND qualified_routes.route = routes.route
  LEFT JOIN reassigned_routes
    ON reassigned_routes.market = routes.market AND reassigned_routes.route = routes.route
  LEFT JOIN failures
    ON failures.market = routes.market AND failures.route = routes.route
  LEFT JOIN market_pipeline
    ON market_pipeline.market = routes.market
  LEFT JOIN route_pipeline_rows
    ON route_pipeline_rows.market = routes.market
    AND route_pipeline_rows.route = routes.route
  LEFT JOIN unresolved_values
    ON unresolved_values.market = routes.market AND routes.route = 'unresolved'
),
market_totals AS (
  SELECT
    joined.*,
    SUM(physical_rows) OVER (PARTITION BY market) AS market_physical_rows
  FROM joined
),
decided AS (
  SELECT
    market_totals.*,
    CASE
      WHEN pipeline_reported_rows > 0 AND market_physical_rows = 0 THEN 'reader_mismatch'
      WHEN route = 'unresolved' THEN 'unresolved_route'
      WHEN pipeline_route_rows > 0 AND physical_rows = 0 THEN 'reader_mismatch'
      WHEN physical_rows > 0 THEN 'emitting'
      WHEN recorded_failures > 0 THEN 'failed'
      WHEN declared_quiet THEN 'known_quiet'
      ELSE 'unexplained_zero'
    END AS control_state,
    CASE
      WHEN pipeline_reported_rows > 0 AND market_physical_rows = 0
        THEN 'pipeline_rows_not_readable'
      WHEN route = 'unresolved' THEN 'route_not_resolved'
      WHEN pipeline_route_rows > 0 AND physical_rows = 0
        THEN 'pipeline_route_rows_not_readable'
      WHEN physical_rows > 0 THEN NULL
      WHEN recorded_failures > 0 THEN 'recorded_failure'
      WHEN declared_quiet THEN 'declared_quiet'
      ELSE 'zero_without_decision_record'
    END AS control_reason
  FROM market_totals
),
ranked AS (
  SELECT
    decided.*,
    ROW_NUMBER() OVER (ORDER BY market, route) AS row_rank
  FROM decided
)
SELECT
  market,
  route,
  expected,
  physical_rows,
  distinct_native_observations,
  inferred_identity_observations,
  rows_without_identity,
  qualified_observations,
  classified_observations,
  unique_qualified_observations,
  reassigned_market_rows,
  rows_with_geo_method,
  newest_published_at,
  newest_collected_at,
  rows_without_published_at,
  recorded_failures,
  pipeline_reported_rows,
  pipeline_route_rows,
  control_state,
  control_reason,
  unresolved_source_values
FROM ranked
WHERE row_rank <= @query_limit + 1
ORDER BY market, route;
