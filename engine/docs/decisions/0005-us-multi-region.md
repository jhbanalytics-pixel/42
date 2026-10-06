# ADR 0005: BigQuery dataset region ,  US multi-region

**Date:** 2026-04-14
**Author:** Albert Meintjes
**Status:** Active

## Decision

The `trends_v2_dev` (and future `trends_v2` prod) dataset is in `US` multi-region. It must
not be moved to any other region.

## Context

The 9th scoring signal (`search_velocity_score`) comes from querying
`bigquery-public-data.google_trends.international_top_rising_terms`. This public dataset
lives in the `US` multi-region. BigQuery cross-region joins fail with a hard error: you
cannot join a table in `US` with a table in `us-central1`, `EU`, or any other region.

The default BigQuery region in `gcloud` config and in the setup script was `us-central1`
before being corrected to `US` during the Phase 0.0 GCP bootstrap.

## Consequences

All BigQuery operations for this project run in US multi-region. This is acceptable under
data residency requirements: the data involved is public social content and aggregated
engagement counts, not PII. POPIA, DPA, and NDPC compliance review confirmed this is
acceptable (see legal gate calendar in the project plan).

If the project is ever required to move to EU or another region, the scoring architecture
would need to change: either duplicate the public dataset via a scheduled query into a
local table, or replace BigQuery Trends with a different data source such as SerpAPI.
