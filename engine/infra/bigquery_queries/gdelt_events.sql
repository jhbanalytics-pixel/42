-- Row-level conflict, protest and civil-unrest signal from the GDELT
-- Events 2.0 public dataset.
-- Source: gdelt-bq.gdeltv2.events_partitioned
--
-- Each row is a single CAMEO-coded event with actor country codes, event
-- code, Goldstein intensity (-10 to +10), mention/source/article counts,
-- average article tone, geolocation, and one canonical source URL.
--
-- 28 May 2026: live BQ probe found two surprises against the task spec.
--
--   (1) The spec referenced bigquery-public-data.gdeltv2.events. Our service
--       account lacks access to that mirror. Same data lives at
--       gdelt-bq.gdeltv2.events_partitioned (the dataset we already use for
--       the GKG partitioned table) and we have access there.
--
--   (2) The flat events table is NOT partitioned: a SQLDATE filter forces a
--       full table scan of ~196 GB per call (~$1/market/day at $5/TB).
--       events_partitioned IS partitioned by ingestion time and the same
--       3-day filter scans 0.09 GB. ~2000x cost reduction. The events
--       table is therefore queried via _PARTITIONTIME, with SQLDATE retained
--       only as a sanity bound.
--
-- Parameters:
--   @country_code       STRING   CAMEO 3-letter country code (SAF, NGA, KEN).
--                                NOT the FIPS 10-4 code used by the GKG
--                                connector. Verified 28 May 2026 against
--                                the live dataset: SAF/NGA/KEN return live
--                                rows, SF/NI/KE return 0.
--   @date_from          INT64    Inclusive YYYYMMDD lower bound on SQLDATE.
--                                Also drives the _PARTITIONTIME prune to
--                                the same window (cost critical).
--   @date_to            INT64    Inclusive YYYYMMDD upper bound on SQLDATE.
--   @event_root_filter  STRING   Optional REGEXP on CAST(EventRootCode AS
--                                STRING). Empty string disables the filter
--                                so the connector default of
--                                '^(14|15|17|18|19)$' captures protest
--                                (14), exhibit-force (15), coerce (17),
--                                assault (18) and fight (19). The CAMEO
--                                root codes 01-13 are diplomatic / verbal
--                                cooperation events and are excluded by
--                                default; they dominate raw volume but
--                                carry no civil-unrest signal.
--   @lim                INT64    Max rows returned.

SELECT
  GLOBALEVENTID,
  SQLDATE,
  Actor1CountryCode,
  Actor2CountryCode,
  EventCode,
  EventRootCode,
  GoldsteinScale,
  NumMentions,
  NumSources,
  NumArticles,
  AvgTone,
  ActionGeo_FullName,
  ActionGeo_Lat,
  ActionGeo_Long,
  SOURCEURL,
  DATEADDED
FROM `gdelt-bq.gdeltv2.events_partitioned`
WHERE _PARTITIONTIME >= TIMESTAMP(
        PARSE_DATE('%Y%m%d', CAST(@date_from AS STRING))
      )
  AND _PARTITIONTIME < TIMESTAMP_ADD(
        TIMESTAMP(PARSE_DATE('%Y%m%d', CAST(@date_to AS STRING))),
        INTERVAL 1 DAY
      )
  AND (Actor1CountryCode = @country_code OR Actor2CountryCode = @country_code)
  AND SQLDATE >= @date_from
  AND SQLDATE <= @date_to
  AND (
    @event_root_filter = ''
    OR REGEXP_CONTAINS(CAST(EventRootCode AS STRING), @event_root_filter)
  )
ORDER BY DATEADDED DESC
LIMIT @lim;
