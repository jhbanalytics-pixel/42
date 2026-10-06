-- Discovery loop candidate proposals (V3 Track C). One row per candidate_id;
-- deterministic hash idempotency. Partition by proposed_date; no expiry.
--
-- Created by scripts/migrations/create_seed_candidates_table.py.
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.seed_candidates` (
  candidate_id STRING NOT NULL,
  proposed_date DATE NOT NULL,
  market STRING NOT NULL,
  candidate_type STRING NOT NULL,
  candidate_value STRING NOT NULL,
  source STRING NOT NULL,
  lane STRING NOT NULL,
  score FLOAT64,
  seed_fit STRUCT<
    genz FLOAT64,
    slang FLOAT64,
    visual_audio FLOAT64,
    co_occur FLOAT64
  >,
  safety_flags ARRAY<STRING>,
  evidence_topics ARRAY<STRING>,
  sample_row_ids ARRAY<STRING>,
  status STRING NOT NULL,
  status_by STRING,
  status_at TIMESTAMP,
  rationale STRING
)
PARTITION BY proposed_date
CLUSTER BY market, status
OPTIONS (
  description = 'Discovery loop ranked taxonomy candidates pending human review.'
);
