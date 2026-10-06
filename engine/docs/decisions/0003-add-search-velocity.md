# ADR 0003: Add search velocity as the 9th scoring signal via BigQuery Trends

**Date:** 2026-04-14
**Author:** Albert Meintjes
**Status:** Active

## Decision

Replace the abandoned pytrends library with the BigQuery public dataset
`bigquery-public-data.google_trends.international_top_rising_terms` as the source of
search intent data. Add `search_velocity_score` as the 9th scoring signal at 0.07 weight.

## Context

Thapelo proposed using pytrends for search intent data in a 13 April email. The instinct
is right: search data adds a predictive dimension that social signals alone cannot. A topic
trending on TikTok with no search activity is noise. A topic trending on TikTok while its
Google search volume is spiking is a real emerging trend.

pytrends is the wrong implementation. The library was archived on 17 April 2025 by its
primary maintainer, who explicitly advised users to stop relying on it. It scrapes
unofficial endpoints and has no API stability guarantees.

## Alternatives considered

**pytrends.** Dead. Archived April 2025. Maintainer's own recommendation: stop using it.

**SerpAPI Google Trends.** Works and is production-grade. At R1,400/month for 5,000
searches on the Developer tier it is the right paid option if we need the `related_queries`
data that the public dataset lacks. Retained as a Phase 2 enhancement.

**Google Trends API alpha.** Announced July 2025. Requires application for access and is
not reliable for a May 1 launch date.

**BigQuery public dataset.** Free. Zero additional infrastructure. Requires only that the
BigQuery dataset is in US multi-region (already the case, required for the scoring
scheduled query). The `international_top_rising_terms` table covers ZA and NG.
The `percent_gain` field (note: not `percent_gained`) measures how fast a search term's
volume increased, which maps directly to velocity as a scoring input.

## Coverage evidence

Script `scripts/fetch_search_velocity.py` queries the public dataset per market and writes
results to `docs/reference/`. Run it to get current coverage counts.

As of 14 April 2026 (see `docs/reference/search_velocity_sample_2026-04-14.json`):

| Market | Rows | Top term | Top pct_gain |
|---|---|---|---|
| ZA (South Africa) | 25 | chelsea vs man city | 14,650% |
| NG (Nigeria) | 25 | al akhdoud vs al-nassr | 13,100% |
| KE (Kenya) | 0 | No data | N/A |

Kenya returns zero rows even over a 90-day lookback window. Kenya is simply not in the
Google Trends public dataset for `international_top_rising_terms`. For KE, the
`search_velocity_score` column defaults to 0 on all rows, meaning search velocity does
not contribute to KE trend scoring. SerpAPI (Phase 2) would cover KE if needed.

The data itself is sports-heavy for this week (football fixtures), which is expected.
The signal is most useful for detecting non-sport trending topics where social and search
signals diverge.

## Scoring impact

Adding a 9th signal at 0.07 weight requires rebalancing the existing eight. Velocity drops
from 0.25 to 0.20 (still the highest-weighted single signal). Watchlist drops from 0.10 to
0.08. All other signals unchanged. Total remains 1.00.

## Consequences

`search_velocity_score` is set to `LEAST(percent_gain / 1000.0, 1.0)` to normalise the
raw percentage into a 0-1 range. The `BigQueryTrendsConnector` populates this column. All
other connectors default it to 0. KE rows always have search_velocity_score = 0.

The `search_velocity_score` column is present in `trend_scores` and `enriched_content`
schemas. `configs/scoring.yaml` documents the weight.

Source documents: `docs/reference/` contains the 13 April Trend Scope Alignment email and
BigQuery Trends sample JSON from the coverage validation run.
