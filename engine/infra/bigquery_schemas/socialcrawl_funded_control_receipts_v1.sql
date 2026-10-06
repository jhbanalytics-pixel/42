CREATE TABLE IF NOT EXISTS `{project}.{dataset}.socialcrawl_funded_control_receipts_v1` (
  control_contract_version STRING NOT NULL OPTIONS(description = 'Funded control receipt contract version.'),
  control_id STRING NOT NULL OPTIONS(description = 'SHA256 over the length-prefixed canonical fields excluding control_id and created_at.'),
  recorded_at TIMESTAMP NOT NULL OPTIONS(description = 'UTC time represented by the control decision.'),
  month_start DATE NOT NULL OPTIONS(description = 'First day of the UTC calendar month and partition key.'),
  credential_lane STRING NOT NULL OPTIONS(description = 'Approved funded credential lane.'),
  funding_account STRING NOT NULL OPTIONS(description = 'Nonsecret funded account label.'),
  activation_stage INT64 NOT NULL OPTIONS(description = 'Funded activation stage from zero through three.'),
  kill_state STRING NOT NULL OPTIONS(description = 'Not tested, passed, failed, or killed.'),
  current_balance NUMERIC NOT NULL OPTIONS(description = 'Current vendor credit balance observed at preflight.'),
  balance_observed_at TIMESTAMP NOT NULL OPTIONS(description = 'UTC time of the current balance observation.'),
  opening_balance NUMERIC NOT NULL OPTIONS(description = 'Approved funded-lane opening balance.'),
  month_opening_balance NUMERIC NOT NULL OPTIONS(description = 'First proven balance in the current UTC month.'),
  monthly_cap NUMERIC NOT NULL OPTIONS(description = 'Approved monthly optional credit cap.'),
  reserve_floor NUMERIC NOT NULL OPTIONS(description = 'Approved balance reserve floor.'),
  stage_cap NUMERIC NOT NULL OPTIONS(description = 'Maximum credit allowance for the activation stage.'),
  monthly_ledger_debit NUMERIC NOT NULL OPTIONS(description = 'Current-month funded phase and attribution-gap debits.'),
  monthly_balance_delta NUMERIC NOT NULL OPTIONS(description = 'Month opening balance less current balance.'),
  monthly_effective_spend NUMERIC NOT NULL OPTIONS(description = 'Larger of ledger debit and balance delta.'),
  monthly_remaining NUMERIC NOT NULL OPTIONS(description = 'Nonnegative monthly cap remaining.'),
  reserve_remaining NUMERIC NOT NULL OPTIONS(description = 'Nonnegative balance above the reserve floor.'),
  run_allowance NUMERIC NOT NULL OPTIONS(description = 'Minimum of stage, monthly, and reserve remaining.'),
  attribution_state STRING NOT NULL OPTIONS(description = 'Complete, conservative, or gap detected.'),
  consecutive_complete_runs INT64 NOT NULL OPTIONS(description = 'Consecutive fully reconciled funded runs.'),
  runs_today INT64 NOT NULL OPTIONS(description = 'Funded runs closed in the current UTC day.'),
  catalog_digest STRING NOT NULL OPTIONS(description = 'Approved SocialCrawl catalog digest.'),
  metadata_digest STRING NOT NULL OPTIONS(description = 'Approved normalized endpoint metadata digest.'),
  source_sha STRING NOT NULL OPTIONS(description = 'Full source commit SHA admitted for the runtime.'),
  created_at TIMESTAMP NOT NULL OPTIONS(description = 'UTC immutable row creation time.')
)
PARTITION BY month_start
CLUSTER BY credential_lane, activation_stage, kill_state, attribution_state
OPTIONS (
  description = 'Staging-only immutable funded-lane control receipts. Natural key: control_id.'
);
