-- The three item_state columns of the locality switch (C4 v3 sections 7.2 and 7.4). NOT in core.sql and NOT in
-- core/schema/apply.py SQL_FILES: only the switch release applies this file, in the same release that sets
-- LOCALITY_AUTHORITY to v2. a80's detect job writes item_state with a positional INSERT of 36 values, and BigQuery
-- refuses a positional insert whose count differs from the table's, so a table widened earlier fails every a80 run
-- and a rollback to a80 could never run detect again. Under the v1 authority the INSERT of core/detect/sql/state.sql
-- names the 36 columns of the table a80 left and writes none of these.
--
-- eligible_v1 keeps the v1 eligibility as an observation, locality_basis names the rule that wrote eligible (v1 or
-- locality_v2.1) and locality_status carries the checked v2 status to the brief (null on the v1 basis). Rows written
-- before them keep NULL.
ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.item_state`
ADD COLUMN IF NOT EXISTS eligible_v1 BOOL;
ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.item_state`
ADD COLUMN IF NOT EXISTS locality_basis STRING;
ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.item_state`
ADD COLUMN IF NOT EXISTS locality_status STRING;
