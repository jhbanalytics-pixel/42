-- News and entity signal per market from the GDELT GKG public dataset.
-- Source: gdelt-bq.gdeltv2.gkg_partitioned
-- Replaces the DOC 2.0 HTTP API which rate-limits hard at 1 request per 5s
-- and returns only 50-100 rows per call. The GKG is free, rate-limit-free,
-- and carries richer signal (themes, entities, tone) over a full partition.
--
-- Parameters:
--   @country_fips    STRING   FIPS 10-4 country code (SF, NI, KE)
--   @days            INT64    Partition window in days
--   @limit           INT64    Max rows returned
--   @slang_pattern   STRING   Optional REGEXP applied to V2Persons only.
--                             Empty string ('') disables the bias so the
--                             ordering falls back to recency only.
--                             29 May 2026: slang is now a SORT PRIORITY, not a
--                             WHERE exclusion. Rows whose V2Persons matches the
--                             pattern float to the top of the result window;
--                             every country row is still kept up to @limit.
--                             History: 27 May shipped slang as a WHERE filter
--                             over themes+persons+orgs and cratered volume ~99%
--                             (ZA 174 -> 1, NG 336 -> 2, KE 115 -> 0). 28 May
--                             narrowed it to V2Persons only, partial recovery
--                             (NG 402 -> 46, ~15% of healthy). The exclusion
--                             model was wrong: GDELT news classifies at ~76%
--                             downstream (best source we have), so subtracting
--                             country news removed high-quality signal. The
--                             29 May fix keeps full country volume and uses the
--                             pattern only to rank slang-matching rows first.
--
-- FIPS 10-4 country filter: GDELT encodes each location as
--   <type>#<FIPS>#<ADM1>#<ADM2>#<lat>#<long>#<featureid>
-- separated by semicolons, so a substring match on "#<FIPS>#" is accurate
-- (avoids false positives on ADM or feature ids that share two-letter codes).
--
-- Partition filter on _PARTITIONTIME is required to keep cost bounded.
--
-- The gcam_column placeholder (curly-braced token in the SELECT list
-- below) is rewritten by the connector to either a comma-prefixed
-- GCAM column alias (gcam_enabled=true) or an empty string. Lives outside
-- the bound-parameter system because BigQuery does not allow parameterising
-- column lists. The ~7KB-per-row GCAM column is kept out of the scan when
-- the parser is dark to avoid paying for bytes the pipeline does not use.
-- See src/ingestion/connectors/gdelt.py for the substitution shape.

SELECT
  DATE,
  DocumentIdentifier AS url,
  SourceCommonName AS source,
  V2Themes AS themes,
  V2Locations AS locations,
  V2Tone AS tone,
  V2Persons AS persons,
  V2Organizations AS orgs{gcam_column}
FROM `gdelt-bq.gdeltv2.gkg_partitioned`
WHERE _PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
  AND V2Locations LIKE CONCAT('%#', @country_fips, '#%')
  AND DocumentIdentifier IS NOT NULL
  AND LENGTH(V2Themes) > 0
ORDER BY
  CASE
    WHEN @slang_pattern != ''
      AND REGEXP_CONTAINS(LOWER(IFNULL(V2Persons, '')), @slang_pattern)
    THEN 0
    ELSE 1
  END,
  DATE DESC
LIMIT @limit;
