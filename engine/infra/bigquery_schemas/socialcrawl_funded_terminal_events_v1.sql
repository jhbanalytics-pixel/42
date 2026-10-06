CREATE TABLE IF NOT EXISTS `{project}.{dataset}.socialcrawl_funded_terminal_events_v1` (
  terminal_contract_version STRING NOT NULL OPTIONS(description = 'Funded terminal event contract version.'),
  terminal_id STRING NOT NULL OPTIONS(description = 'Content address over every event field except terminal_id and created_at.'),
  execution_id STRING NOT NULL OPTIONS(description = 'Funded execution identifier.'),
  run_id STRING NOT NULL OPTIONS(description = 'Pipeline run identifier.'),
  credential_lane STRING NOT NULL OPTIONS(description = 'Approved funded credential lane.'),
  funding_account STRING NOT NULL OPTIONS(description = 'Nonsecret funded account label.'),
  recorded_at TIMESTAMP NOT NULL OPTIONS(description = 'UTC terminal decision time and partition key.'),
  balance_read_status STRING NOT NULL OPTIONS(description = 'Measured or unavailable post-run balance read state.'),
  last_measured_balance NUMERIC NOT NULL OPTIONS(description = 'Latest balance obtained from a successful vendor response.'),
  last_balance_observed_at TIMESTAMP NOT NULL OPTIONS(description = 'UTC time of the latest successful balance response.'),
  authorized_quoted_debit NUMERIC NOT NULL OPTIONS(description = 'Quoted debit reserved before funded HTTP attempts.'),
  vendor_reported_debit NUMERIC NOT NULL OPTIONS(description = 'Known debit reported by valid vendor responses.'),
  overage_debit NUMERIC NOT NULL OPTIONS(description = 'Nonnegative vendor debit above quoted authority.'),
  ledger_debit NUMERIC NOT NULL OPTIONS(description = 'Conservative known debit retained in the funded ledger.'),
  attribution_state STRING NOT NULL OPTIONS(description = 'Complete, conservative, or gap detected attribution state.'),
  kill_state STRING NOT NULL OPTIONS(description = 'Terminal kill state.'),
  reason_codes ARRAY<STRING> OPTIONS(description = 'Ordered nonempty terminal reason codes.'),
  source_sha STRING NOT NULL OPTIONS(description = 'Full source commit SHA admitted for the runtime.'),
  manifest_sha256 STRING NOT NULL OPTIONS(description = 'Approved execution manifest digest.'),
  created_at TIMESTAMP NOT NULL OPTIONS(description = 'UTC immutable row creation time.')
)
PARTITION BY DATE(recorded_at)
CLUSTER BY credential_lane, kill_state, execution_id
OPTIONS (
  description = 'Staging-only immutable terminal events for funded SocialCrawl executions.'
);
