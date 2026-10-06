-- Client loop closure audit trail (V3 Phase F). Small decision table, no expiry.
--
-- Created by scripts/migrations/create_seed_outcomes_table.py.
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.seed_outcomes` (
  behaviour_or_candidate_id STRING NOT NULL,
  market STRING NOT NULL,
  outcome STRING NOT NULL,
  noted_by STRING,
  noted_at TIMESTAMP,
  note STRING
)
CLUSTER BY market, outcome
OPTIONS (
  description = 'Seeded behaviour / candidate outcome states for client loop closure.'
);
