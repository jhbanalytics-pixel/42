CREATE OR REPLACE VIEW `{project}.{view_dataset}.v_socialcrawl_funded_budget_v2` AS
WITH
  latest_control AS (
    SELECT control.*
    FROM `{project}.{source_dataset}.socialcrawl_funded_control_receipts_v1` AS control
    WHERE control.credential_lane = 'ogilvy_funded'
      AND control.funding_account = 'ogilvy_albert'
    QUALIFY ROW_NUMBER() OVER (ORDER BY control.recorded_at DESC, control.control_id DESC) = 1
  ),
  latest_terminal AS (
    SELECT terminal.*
    FROM `{project}.{source_dataset}.socialcrawl_funded_terminal_events_v1` AS terminal
    WHERE terminal.credential_lane = 'ogilvy_funded'
      AND terminal.funding_account = 'ogilvy_albert'
    QUALIFY ROW_NUMBER() OVER (ORDER BY terminal.recorded_at DESC, terminal.terminal_id DESC) = 1
  ),
  compatible_budget AS (
    SELECT * FROM `{project}.{view_dataset}.v_socialcrawl_funded_budget_v1`
  )
SELECT
  IF(terminal.recorded_at > control.recorded_at, 'blocked',
    IF(budget.kill_state = 'passed' AND budget.attribution_state = 'complete'
      AND budget.run_allowance = budget.stage_cap, 'ready',
      IF(budget.monthly_remaining = 0 OR budget.reserve_remaining = 0, 'exhausted', 'blocked'))
  ) AS state,
  control.month_start,
  control.credential_lane,
  control.funding_account,
  control.activation_stage,
  control.opening_balance,
  IF(terminal.recorded_at > control.recorded_at, terminal.last_measured_balance, budget.current_balance)
    AS current_balance,
  control.month_opening_balance,
  control.monthly_cap,
  IF(terminal.recorded_at > control.recorded_at,
    GREATEST(control.monthly_ledger_debit + terminal.ledger_debit, control.monthly_ledger_debit),
    budget.monthly_ledger_debit) AS monthly_ledger_debit,
  IF(terminal.recorded_at > control.recorded_at,
    control.month_opening_balance - terminal.last_measured_balance,
    budget.monthly_balance_delta) AS monthly_balance_delta,
  IF(terminal.recorded_at > control.recorded_at,
    GREATEST(control.monthly_ledger_debit + terminal.ledger_debit,
      control.month_opening_balance - terminal.last_measured_balance),
    budget.monthly_effective_spend) AS monthly_effective_spend,
  IF(terminal.recorded_at > control.recorded_at,
    GREATEST(0, control.monthly_cap - GREATEST(control.monthly_ledger_debit + terminal.ledger_debit,
      control.month_opening_balance - terminal.last_measured_balance)),
    budget.monthly_remaining) AS monthly_remaining,
  control.reserve_floor,
  IF(terminal.recorded_at > control.recorded_at,
    GREATEST(0, terminal.last_measured_balance - control.reserve_floor),
    budget.reserve_remaining) AS reserve_remaining,
  control.stage_cap,
  IF(terminal.recorded_at > control.recorded_at, 0, budget.run_allowance) AS run_allowance,
  IF(terminal.recorded_at > control.recorded_at, terminal.attribution_state, budget.attribution_state)
    AS attribution_state,
  IF(terminal.recorded_at > control.recorded_at, terminal.kill_state, budget.kill_state)
    AS kill_state,
  terminal.terminal_id,
  terminal.recorded_at AS terminal_recorded_at,
  terminal.balance_read_status,
  terminal.last_measured_balance,
  terminal.last_balance_observed_at,
  terminal.authorized_quoted_debit,
  terminal.vendor_reported_debit,
  terminal.overage_debit,
  terminal.reason_codes
FROM latest_control AS control
LEFT JOIN compatible_budget AS budget ON TRUE
LEFT JOIN latest_terminal AS terminal ON TRUE;
