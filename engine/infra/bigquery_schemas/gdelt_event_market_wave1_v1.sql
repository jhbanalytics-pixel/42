CREATE TABLE IF NOT EXISTS `{project}.{dataset}.gdelt_event_market_wave1_v1` (
  GLOBALEVENTID INT64 NOT NULL OPTIONS(description = 'Parent GDELT event identifier.'),
  market STRING NOT NULL OPTIONS(description = 'Retained lower-case market code.'),
  evidence_role STRING NOT NULL OPTIONS(description = 'Named market evidence role.'),
  receipt_id STRING NOT NULL OPTIONS(description = 'Immutable market evidence receipt identifier.')
)
PARTITION BY _PARTITIONDATE
CLUSTER BY market, evidence_role
OPTIONS (
  description = 'Wave 1 retained market evidence at GLOBALEVENTID and market grain.'
);
