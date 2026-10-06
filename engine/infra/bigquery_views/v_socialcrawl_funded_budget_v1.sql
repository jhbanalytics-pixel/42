CREATE OR REPLACE VIEW `{project}.{view_dataset}.v_socialcrawl_funded_budget_v1` AS
WITH
  current_month AS (
    SELECT DATE_TRUNC(CURRENT_DATE('UTC'), MONTH) AS month_start
  ),
  latest_control AS (
    SELECT control.*
    FROM `{project}.{source_dataset}.socialcrawl_funded_control_receipts_v1` AS control
    CROSS JOIN current_month
    WHERE control.credential_lane = 'ogilvy_funded'
      AND control.month_start = current_month.month_start
      AND control.recorded_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 15 MINUTE)
      AND control.balance_observed_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 15 MINUTE)
      AND control.recorded_at <= CURRENT_TIMESTAMP()
      AND control.balance_observed_at <= control.recorded_at
    QUALIFY ROW_NUMBER() OVER (ORDER BY control.recorded_at DESC, control.control_id DESC) = 1
  ),
  candidate_controls AS (
    SELECT control.*
    FROM latest_control AS control
    WHERE control.control_contract_version = 'socialcrawl_funded_control_v1'
      AND control.funding_account = 'ogilvy_albert'
      AND control.activation_stage BETWEEN 0 AND 3
      AND control.kill_state IN ('not_tested', 'passed', 'failed', 'killed')
      AND control.attribution_state IN ('complete', 'conservative', 'gap_detected')
      AND (
        (
          control.catalog_digest = '6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6'
          AND control.metadata_digest = '7d7152b2441f367417c74169a7bd4fb1a190a92763bbb43532167d5d4612fdfc'
        )
        OR (
          control.catalog_digest = '6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6'
          AND control.metadata_digest = '501ff9eae79e1504840107432b58b66c19de70e44e2ebb16aea190e4d394c39a'
        )
      )
      AND REGEXP_CONTAINS(control.source_sha, r'^[0-9a-f]{40}$')
      AND control.control_id = LOWER(TO_HEX(SHA256(CONCAT(
        CONCAT('V', CAST(BYTE_LENGTH(control.control_contract_version) AS STRING), ':', control.control_contract_version),
        CONCAT('V', CAST(BYTE_LENGTH(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', control.recorded_at, 'UTC')) AS STRING), ':', FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', control.recorded_at, 'UTC')),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.month_start AS STRING)) AS STRING), ':', CAST(control.month_start AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(control.credential_lane) AS STRING), ':', control.credential_lane),
        CONCAT('V', CAST(BYTE_LENGTH(control.funding_account) AS STRING), ':', control.funding_account),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.activation_stage AS STRING)) AS STRING), ':', CAST(control.activation_stage AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(control.kill_state) AS STRING), ':', control.kill_state),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.current_balance AS STRING)) AS STRING), ':', CAST(control.current_balance AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', control.balance_observed_at, 'UTC')) AS STRING), ':', FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', control.balance_observed_at, 'UTC')),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.opening_balance AS STRING)) AS STRING), ':', CAST(control.opening_balance AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.month_opening_balance AS STRING)) AS STRING), ':', CAST(control.month_opening_balance AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.monthly_cap AS STRING)) AS STRING), ':', CAST(control.monthly_cap AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.reserve_floor AS STRING)) AS STRING), ':', CAST(control.reserve_floor AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.stage_cap AS STRING)) AS STRING), ':', CAST(control.stage_cap AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.monthly_ledger_debit AS STRING)) AS STRING), ':', CAST(control.monthly_ledger_debit AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.monthly_balance_delta AS STRING)) AS STRING), ':', CAST(control.monthly_balance_delta AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.monthly_effective_spend AS STRING)) AS STRING), ':', CAST(control.monthly_effective_spend AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.monthly_remaining AS STRING)) AS STRING), ':', CAST(control.monthly_remaining AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.reserve_remaining AS STRING)) AS STRING), ':', CAST(control.reserve_remaining AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.run_allowance AS STRING)) AS STRING), ':', CAST(control.run_allowance AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(control.attribution_state) AS STRING), ':', control.attribution_state),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.consecutive_complete_runs AS STRING)) AS STRING), ':', CAST(control.consecutive_complete_runs AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(CAST(control.runs_today AS STRING)) AS STRING), ':', CAST(control.runs_today AS STRING)),
        CONCAT('V', CAST(BYTE_LENGTH(control.catalog_digest) AS STRING), ':', control.catalog_digest),
        CONCAT('V', CAST(BYTE_LENGTH(control.metadata_digest) AS STRING), ':', control.metadata_digest),
        CONCAT('V', CAST(BYTE_LENGTH(control.source_sha) AS STRING), ':', control.source_sha)
      ))))
      AND control.current_balance <= control.month_opening_balance
      AND control.opening_balance = 250100
      AND control.monthly_cap = 25000
      AND control.reserve_floor = 225000
      AND control.stage_cap = CASE control.activation_stage
        WHEN 0 THEN 0 WHEN 1 THEN 100 WHEN 2 THEN 250 WHEN 3 THEN 750 END
      AND control.monthly_balance_delta = control.month_opening_balance - control.current_balance
      AND control.monthly_effective_spend = GREATEST(
        control.monthly_ledger_debit, control.monthly_balance_delta
      )
      AND control.monthly_remaining = GREATEST(
        0, control.monthly_cap - control.monthly_effective_spend
      )
      AND control.reserve_remaining = GREATEST(
        0, control.current_balance - control.reserve_floor
      )
      AND control.run_allowance = LEAST(
        control.stage_cap, control.monthly_remaining, control.reserve_remaining
      )
  ),
  month_ledger AS (
    SELECT
      SUM(IF(event_type IN ('phase_close', 'attribution_gap'), budget_debit_credits, 0))
        AS monthly_ledger_debit,
      ARRAY_AGG(
        IF(balance_observed IS NOT NULL, STRUCT(recorded_at, balance_observed), NULL)
        IGNORE NULLS ORDER BY recorded_at DESC LIMIT 1
      )[SAFE_OFFSET(0)] AS latest_balance,
      MAX(IF(event_type = 'attribution_gap', recorded_at, NULL)) AS latest_gap_at
    FROM `{project}.{source_dataset}.socialcrawl_credit_ledger_v1` AS entry
    CROSS JOIN current_month
    WHERE entry.credential_lane = 'ogilvy_funded'
      AND entry.month_start = current_month.month_start
  )
SELECT control.*, ledger.latest_balance.recorded_at AS ledger_through
FROM candidate_controls AS control
CROSS JOIN month_ledger AS ledger
WHERE control.monthly_ledger_debit = COALESCE(ledger.monthly_ledger_debit, 0)
  AND ledger.latest_balance.balance_observed = control.current_balance
  AND ledger.latest_balance.recorded_at = control.balance_observed_at
  AND NOT EXISTS (
    SELECT 1
    FROM `{project}.{source_dataset}.socialcrawl_credit_ledger_v1` AS later
    WHERE later.credential_lane = 'ogilvy_funded'
      AND later.month_start = control.month_start
      AND later.recorded_at > control.recorded_at
      AND (later.balance_observed IS NOT NULL OR later.event_type = 'attribution_gap')
  )
  AND (ledger.latest_gap_at IS NULL OR ledger.latest_gap_at <= control.recorded_at);
