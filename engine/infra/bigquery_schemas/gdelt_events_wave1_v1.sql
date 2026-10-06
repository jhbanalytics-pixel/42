CREATE TABLE IF NOT EXISTS `{project}.{dataset}.gdelt_events_wave1_v1` (
  GLOBALEVENTID INT64 NOT NULL OPTIONS(description = 'Canonical GDELT event identifier.'),
  event_date DATE NOT NULL OPTIONS(description = 'GDELT event date.'),
  actor1_country_code STRING OPTIONS(description = 'Nullable first actor country code.'),
  actor2_country_code STRING OPTIONS(description = 'Nullable second actor country code.'),
  event_code STRING OPTIONS(description = 'Nullable CAMEO event code.'),
  event_root_code STRING OPTIONS(description = 'Nullable CAMEO root event code.'),
  goldstein_scale FLOAT64 OPTIONS(description = 'Nullable Goldstein scale.'),
  mention_count INT64 NOT NULL OPTIONS(description = 'Observed mention count.'),
  source_count INT64 NOT NULL OPTIONS(description = 'Observed source count.'),
  article_count INT64 NOT NULL OPTIONS(description = 'Observed article count.'),
  average_tone FLOAT64 OPTIONS(description = 'Nullable GDELT average tone.'),
  action_geo_name STRING OPTIONS(description = 'Nullable action geography label.'),
  action_geo_latitude FLOAT64 OPTIONS(description = 'Nullable action geography latitude.'),
  action_geo_longitude FLOAT64 OPTIONS(description = 'Nullable action geography longitude.'),
  source_url STRING OPTIONS(description = 'Nullable source article URL.'),
  date_added TIMESTAMP NOT NULL OPTIONS(description = 'GDELT ingestion timestamp.')
)
PARTITION BY event_date
CLUSTER BY event_root_code
OPTIONS (
  description = 'Wave 1 GDELT event facts at one row per GLOBALEVENTID.'
);
