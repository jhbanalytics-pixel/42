-- RECONCILE decision ledger.
--
-- One row per (claim, run) reconcile decision. This is the audit trail of the
-- deterministic trust boundary: for each claim a brief or daily summary
-- asserted, it records what reconcile did (corroborated / stale / labelled /
-- no_match), the entity it anchored to, the receipts that backed a correction
-- or label, and the confidence tier. The module is pure code (no Gemini), so
-- ``model`` records the deterministic engine version, not an LLM name.
--
-- Written by the flag-gated shadow path (wired separately); this schema only
-- declares the table for a clean / disaster-recovery setup.
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.reconcile_actions` (
  action_id STRING NOT NULL,
  trend_date DATE NOT NULL,
  market STRING,
  run_id STRING,
  source_surface STRING,
  claim_before STRING,
  action STRING,
  claim_after STRING,
  matched_entity_key STRING,
  receipt_ids ARRAY<STRING>,
  confidence_tier STRING,
  model STRING,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
PARTITION BY trend_date
OPTIONS (
  description = 'Deterministic reconcile decisions per asserted claim. The trust boundary audit trail: action, matched entity, receipts, confidence tier. Pure code, no Gemini.'
);
