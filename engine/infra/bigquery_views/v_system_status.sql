-- System status view.
-- Powers the morning-check SYSTEM block (the always-on observability surface).
-- Reads the append-only system_events table (created by
-- scripts/migrations/create_system_events_table.py). Cron and digest freshness
-- already live in v_pipeline_health; this view is the events-and-errors surface
-- that pipeline_runs does not carry.
--
-- system_events schema (partitioned by DATE(event_time), clustered by source,
-- severity): event_id, event_time, environment, source, severity, event_type,
-- status, market, message, error_detail, latency_ms, meta.
--
-- The view answers two questions over the last 24 hours:
--   1. Per source: how many events, how many ERRORs, when was the last event
--      and how stale is it, plus avg and max latency where recorded.
--   2. System-wide: total ERROR count and the single most recent ERROR's
--      message, error_detail, source and time.
--
-- Robust to an empty table. With no events in the window the per-source CTE
-- yields no rows and the latest-error fields read NULL with a zero error count,
-- so the view returns nothing or zeros rather than failing.

CREATE OR REPLACE VIEW `{project}.{dataset}.v_system_status` AS
WITH events_24h AS (
  SELECT
    source,
    severity,
    event_type,
    status,
    market,
    message,
    error_detail,
    latency_ms,
    event_time
  FROM `{project}.{dataset}.system_events`
  WHERE event_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
),
per_source AS (
  SELECT
    source,
    COUNT(*) AS event_count,
    COUNTIF(severity = 'ERROR') AS error_count,
    COUNTIF(severity = 'WARN') AS warn_count,
    MAX(event_time) AS last_event_time,
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(event_time), MINUTE) AS minutes_since_last_event,
    AVG(latency_ms) AS avg_latency_ms,
    MAX(latency_ms) AS max_latency_ms
  FROM events_24h
  GROUP BY source
),
latest_error AS (
  SELECT
    source AS latest_error_source,
    event_time AS latest_error_time,
    message AS latest_error_message,
    error_detail AS latest_error_detail
  FROM events_24h
  WHERE severity = 'ERROR'
  QUALIFY ROW_NUMBER() OVER (ORDER BY event_time DESC) = 1
),
totals AS (
  -- One-row aggregate so the latest-error LEFT JOIN always has a left side,
  -- even when the 24h window is empty (then total_error_events_24h is 0).
  SELECT COUNTIF(severity = 'ERROR') AS total_error_events_24h
  FROM events_24h
)
SELECT
  ps.source,
  ps.event_count,
  ps.error_count,
  ps.warn_count,
  ps.last_event_time,
  ps.minutes_since_last_event,
  ps.avg_latency_ms,
  ps.max_latency_ms,
  t.total_error_events_24h,
  le.latest_error_source,
  le.latest_error_time,
  le.latest_error_message,
  le.latest_error_detail
FROM per_source ps
CROSS JOIN totals t
LEFT JOIN latest_error le ON TRUE
ORDER BY ps.error_count DESC, ps.event_count DESC;
