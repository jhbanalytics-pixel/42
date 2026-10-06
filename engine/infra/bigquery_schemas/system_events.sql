-- System observability events.
--
-- One append-only row per notable system event across the whole stack: the
-- engine cron, the digest send, the watchdog, and the Listening Post chat/api.
-- Mirrors the pipeline_runs philosophy (append log, never updated) but captures
-- the cause of a failure, not just the row counts, and it spans services that
-- pipeline_runs does not see (the LP chat turns + errors). One queryable
-- history powers the v_system_status view and the morning-check SYSTEM block,
-- and the [TEV2_FATAL] Cloud Logging marker (emitted by the writer for fatal
-- events) drives the real-time email alert (scripts/deploy_alert_policy.sh).
--
-- Created by scripts/migrations/create_system_events_table.py.
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.system_events` (
  event_id STRING NOT NULL,
  event_time TIMESTAMP NOT NULL,
  environment STRING NOT NULL,
  -- Originating service: engine_cron | lp_chat | lp_api | watchdog | email.
  source STRING NOT NULL,
  -- INFO | WARN | ERROR.
  severity STRING NOT NULL,
  -- cron_run | chat_turn | digest_failed | connector_fail | error | ...
  event_type STRING,
  -- ok | failed | skipped | ...
  status STRING,
  market STRING,
  -- Short human summary.
  message STRING,
  -- Cause / traceback for ERROR events.
  error_detail STRING,
  latency_ms INT64,
  -- Freeform JSON context.
  meta STRING
)
PARTITION BY DATE(event_time)
CLUSTER BY source, severity
OPTIONS (
  description = 'Append-only system observability events across the engine cron, digest, watchdog, and Listening Post.'
);
