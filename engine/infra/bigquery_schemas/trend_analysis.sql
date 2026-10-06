-- Gemini AI analysis of top trends.
--
-- Schema evolves additively: every new field added by a Phase 2 brief
-- iteration ships through scripts/migrations/add_*_to_trend_analysis.py
-- (idempotent ADD COLUMN IF NOT EXISTS) and lands here on the same commit
-- so this file stays the source of truth for fresh-environment creates.
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.trend_analysis` (
  analysis_id STRING NOT NULL,
  trend_date DATE NOT NULL,
  market STRING NOT NULL,
  query_group STRING NOT NULL,
  cycle_id STRING,
  trend_score FLOAT64,
  trend_synthesis STRING,
  cultural_context STRING,
  campaign_angles ARRAY<STRING>,
  risk_flags ARRAY<STRING>,
  gemini_model STRING,
  prompt_tokens INT64,
  completion_tokens INT64,
  analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
  -- Brief v1 columns added 2026-05-04 (commit d2df0b8 + migration that day).
  platforms ARRAY<STRING>,
  sentiment_summary STRING,
  status_tag STRING,
  -- Brief v2 columns added 2026-05-05 (Workstream E, brief alignment with the
  -- Trends Engine Marketing Brief mandate for Zero-Edit prompts + creator
  -- + social-reference outputs).
  nano_banana_prompt STRING,
  lyria_prompt STRING,
  top_creators ARRAY<STRING>,
  social_refs ARRAY<STRING>,
  -- Brief v2.1 column added 2026-05-05 (Thapelo's request, per-platform
  -- content counts surfaced in the email card next to the platform list).
  platform_counts ARRAY<STRING>,
  -- Brief v2.2 column added 2026-05-05 (AI Layer 2, multimodal NB
  -- grounding). One-sentence visual fingerprint extracted by Gemini
  -- from sample post text in the same call as the rest of the brief.
  -- Grounds nano_banana_prompt in concrete visual elements.
  visual_anchor STRING,
  -- Workstream E (2026-05-05): per-project Brand24 sentiment trajectory
  -- label derived from the brand24_mention_sentiment Wave-2 aggregate
  -- rows. One value per market, attached to every brief in that market.
  b24_sentiment_trajectory STRING,
  -- Brief headline added 2026-06 (migration add_headline_column_to_trend_analysis.py).
  headline STRING,
  -- Render bundle added 2026-06-10 (migration add_render_payload_column.py).
  -- Single JSON string carrying the full {display, comment_*, driving_hashtags,
  -- forecast} payload so a recovery resend matches the live mailer exactly.
  render_payload STRING,
  -- Reconcile columns added by add_reconcile_columns.py (Intelligence Core
  -- trust boundary). The reconciled read of this brief's claims: corrected
  -- text (or the original when unchanged), the engine's confidence, the
  -- corroboration sources that backed any correction, and a date-stamp note
  -- trail. Written by the flag-gated reconcile shadow path.
  reconciled_read STRING,
  reconcile_confidence FLOAT64,
  corroboration_sources STRING,
  reconcile_notes STRING
)
PARTITION BY trend_date
CLUSTER BY market
OPTIONS (
  description = 'Vertex AI Gemini trend analysis outputs including synthesis, activation idea, paste-ready Nano Banana / Lyria prompts, top creators, and social references'
);
