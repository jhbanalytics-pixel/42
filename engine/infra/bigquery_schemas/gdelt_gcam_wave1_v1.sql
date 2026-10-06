CREATE TABLE IF NOT EXISTS `{project}.{dataset}.gdelt_gcam_wave1_v1` (
  document_url STRING NOT NULL OPTIONS(description = 'Canonical GKG document URL.'),
  published_at TIMESTAMP NOT NULL OPTIONS(description = 'GKG document publication timestamp.'),
  v10_1 FLOAT64 OPTIONS(description = 'Nullable GCAM v10.1 value.'),
  v10_2 FLOAT64 OPTIONS(description = 'Nullable GCAM v10.2 value.'),
  v19_1 FLOAT64 OPTIONS(description = 'Nullable GCAM v19.1 value.'),
  v19_9 FLOAT64 OPTIONS(description = 'Nullable GCAM v19.9 value.'),
  v20_1 FLOAT64 OPTIONS(description = 'Nullable GCAM v20.1 value.')
)
PARTITION BY DATE(published_at)
CLUSTER BY document_url
OPTIONS (
  description = 'Wave 1 GDELT GCAM dimensions at document URL and published timestamp grain.'
);
