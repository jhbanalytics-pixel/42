CREATE TABLE IF NOT EXISTS `{project}.{dataset}.socialcrawl_credit_ledger_v1` (
  ledger_id STRING NOT NULL OPTIONS(description = 'Stable ledger row identifier.'),
  execution_id STRING NOT NULL OPTIONS(description = 'Funded lane execution identifier.'),
  run_id STRING NOT NULL OPTIONS(description = 'Parent engine run identifier.'),
  trend_date DATE NOT NULL OPTIONS(description = 'UTC run date and partition key.'),
  recorded_at TIMESTAMP NOT NULL OPTIONS(description = 'UTC time at which the ledger event was recorded.'),
  credential_lane STRING NOT NULL OPTIONS(description = 'JHB core or Ogilvy funded credential lane.'),
  market STRING OPTIONS(description = 'Lower case market, or null for a global event.'),
  phase STRING NOT NULL OPTIONS(description = 'Contracted funded lane phase.'),
  event_type STRING NOT NULL OPTIONS(description = 'Preflight, phase close, run close, or attribution gap.'),
  calls INT64 NOT NULL OPTIONS(description = 'Number of attempted vendor calls represented by the row.'),
  budget_debit_credits NUMERIC NOT NULL OPTIONS(description = 'Conservative breaker debit, including quoted transport failure debit.'),
  vendor_reported_credits NUMERIC NOT NULL OPTIONS(description = 'Exact credits reported by successful vendor envelopes.'),
  balance_observed NUMERIC OPTIONS(description = 'Observed vendor balance when available.'),
  opening_balance NUMERIC NOT NULL OPTIONS(description = 'Approved activation opening balance.'),
  month_opening_balance NUMERIC NOT NULL OPTIONS(description = 'First proven balance in the current UTC calendar month.'),
  month_start DATE NOT NULL OPTIONS(description = 'First day of the UTC calendar month.'),
  monthly_ledger_debit_before NUMERIC NOT NULL OPTIONS(description = 'Ledger debit before this event.'),
  monthly_balance_delta_before NUMERIC NOT NULL OPTIONS(description = 'Opening balance less current balance before this event.'),
  monthly_effective_spend_before NUMERIC NOT NULL OPTIONS(description = 'Larger of ledger debit and balance delta before this event.'),
  monthly_cap NUMERIC NOT NULL OPTIONS(description = 'Approved monthly optional credit cap.'),
  reserve_floor NUMERIC NOT NULL OPTIONS(description = 'Approved funded lane reserve floor.'),
  run_allowance NUMERIC NOT NULL OPTIONS(description = 'Full run allowance calculated at preflight.'),
  catalog_digest STRING NOT NULL OPTIONS(description = 'Observed SocialCrawl route catalog digest.'),
  metadata_digest STRING NOT NULL OPTIONS(description = 'Observed normalized SocialCrawl endpoint metadata digest.'),
  attribution_state STRING NOT NULL OPTIONS(description = 'Complete, conservative, or gap detected.'),
  created_at TIMESTAMP NOT NULL OPTIONS(description = 'UTC immutable row creation time.')
)
PARTITION BY trend_date
CLUSTER BY credential_lane, event_type, execution_id, phase
OPTIONS (
  description = 'Staging only immutable SocialCrawl credit ledger. Natural key: (execution_id, credential_lane, market or global, phase, event_type).'
);
