-- Event-state ledger.
--
-- One row per (trend_date, market, entity_key) tracking the CURRENT
-- resolved state of a real-world event the engine is watching: a match,
-- a release, an election, a launch. The PULSE Intelligence Core reads
-- across today's factual enriched_content rows (GDELT GKG entities, RSS
-- headlines, search-velocity spikes), clusters them by entity, then
-- resolves each event's state from those rows alone via a single
-- per-market Gemini pass. The deterministic headline-pattern guess is
-- the fallback that ships when Gemini is skipped or fails.
--
-- Anti-hallucination is enforced in Python, not the model: a returned
-- state must cite a supplied evidence row index or it is dropped, and a
-- 'resolved' label with no citing row falls back to the deterministic
-- guess. resolved_by records which path produced the row.
--
-- Persisted (not just in-memory) so the dashboard can render an event
-- timeline and the operator can audit which events the engine called
-- resolved versus scheduled versus unknown on any given day.
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.event_ledger` (
  ledger_id STRING,
  trend_date DATE,
  market STRING,
  entity_key STRING,
  entity_aliases ARRAY<STRING>,
  event_kind STRING,
  state_label STRING,
  state_text STRING,
  as_of TIMESTAMP,
  evidence_quote STRING,
  corroborating_sources ARRAY<STRING>,
  source_count INT64,
  confidence FLOAT64,
  resolved_by STRING,
  gemini_model STRING,
  generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
PARTITION BY trend_date
OPTIONS (
  description = 'Event-state ledger: current resolved state per watched entity per market per day, from today factual rows alone.'
);
