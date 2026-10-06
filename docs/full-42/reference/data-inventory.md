# Existing BigQuery data inventory

Run at 2026-09-28T20:40:05Z by core/setup/inventory.py as f42-builder (application default credentials), project ogilvy-trends-v2, location US. Read only: every query is one SELECT, dry-run first, refused above 5 GB and run with maximum_bytes_billed at 5 GB. Only counts, dates and market values are read, never row content. Columns SALVAGE.md drops on read are never picked or listed.

25 queries, 105,124,712 bytes processed in total.

## trends_v2_dev.enriched_content

Total rows: 1,782,596. Market column: `market`. Date column: `published_at` (TIMESTAMP).

| Market | Rows | Min date | Max date |
|---|---|---|---|
| ke | 444,500 | 1971-09-27 00:00:00+00 | 2026-09-28 00:45:00+00 |
| ng | 682,946 | 2008-09-11 15:00:12+00 | 2026-09-28 00:45:00+00 |
| za | 655,150 | 1940-05-29 00:00:00+00 | 2026-09-28 01:12:51+00 |

## trends_v2_dev.seed_graph

Total rows: 1,496,686. Market column: `market`. Date column: `trend_date` (DATE).

| Market | Rows | Min date | Max date |
|---|---|---|---|
| ke | 495,647 | 2026-06-18 | 2026-09-28 |
| ng | 495,314 | 2026-06-18 | 2026-09-28 |
| za | 505,725 | 2026-06-18 | 2026-09-28 |

## trends_v2_dev.trend_scores

Total rows: 4,527. Market column: `market`. Date column: `trend_date` (DATE).

| Market | Rows | Min date | Max date |
|---|---|---|---|
| ke | 1,546 | 2026-04-16 | 2026-09-28 |
| ng | 1,554 | 2026-04-16 | 2026-09-28 |
| za | 1,427 | 2026-04-16 | 2026-09-28 |

## trends_v2_staging.enriched_content

Total rows: 104,443. Market column: `market`. Date column: `published_at` (TIMESTAMP).

| Market | Rows | Min date | Max date |
|---|---|---|---|
| ke | 29,805 | 1971-09-27 00:00:00+00 | 2026-09-08 06:21:43.517182+00 |
| ng | 35,832 | 2017-06-09 00:00:00+00 | 2026-09-08 07:16:16+00 |
| za | 38,806 | 1973-10-15 00:00:00+00 | 2026-09-08 07:00:00+00 |

## trends_v2_staging.seed_graph

Total rows: 210,000. Market column: `market`. Date column: `trend_date` (DATE).

| Market | Rows | Min date | Max date |
|---|---|---|---|
| ke | 70,000 | 2026-08-21 | 2026-09-03 |
| ng | 70,000 | 2026-08-21 | 2026-09-03 |
| za | 70,000 | 2026-08-21 | 2026-09-03 |

## intelligence_42_sources_staging.collection_receipts

Total rows: 2. Market column: none fits. Date column: `recorded_at` (TIMESTAMP).
Dates run from 2026-09-27 13:33:27.897371+00 to 2026-09-27 13:42:50.820552+00.

## intelligence_42_sources_staging.enriched_content

Total rows: 1,573. Market column: `market`. Date column: `published_at` (TIMESTAMP).

| Market | Rows | Min date | Max date |
|---|---|---|---|
| ke | 470 | 2011-06-28 00:00:00+00 | 2026-09-27 13:23:10.426+00 |
| ng | 546 | 2017-06-09 00:00:00+00 | 2026-09-27 14:26:02+00 |
| za | 557 | 2011-08-09 00:00:00+00 | 2026-09-27 15:22:13+00 |

## intelligence_42_sources_staging.pipeline_runs

Total rows: 3. Market column: `market`. Date column: `started_at` (TIMESTAMP).

| Market | Rows | Min date | Max date |
|---|---|---|---|
| ke | 1 | 2026-09-27 13:31:36.812023+00 | 2026-09-27 13:31:36.812023+00 |
| ng | 1 | 2026-09-27 13:31:36.812023+00 | 2026-09-27 13:31:36.812023+00 |
| za | 1 | 2026-09-27 13:31:36.812023+00 | 2026-09-27 13:31:36.812023+00 |

## intelligence_42_sources_staging.raw_content

Total rows: 1,573. Market column: `market`. Date column: `published_at` (TIMESTAMP).

| Market | Rows | Min date | Max date |
|---|---|---|---|
| ke | 470 | 2011-06-28 00:00:00+00 | 2026-09-27 13:23:10.426+00 |
| ng | 546 | 2017-06-09 00:00:00+00 | 2026-09-27 14:26:02+00 |
| za | 557 | 2011-08-09 00:00:00+00 | 2026-09-27 15:22:13+00 |

## intelligence_42_sources_staging.system_events

Total rows: 7. Market column: `market`. Date column: `event_time` (TIMESTAMP).

| Market | Rows | Min date | Max date |
|---|---|---|---|
| ke | 3 | 2026-09-27 13:33:07.150071+00 | 2026-09-27 13:42:47.907691+00 |
| ng | 2 | 2026-09-27 13:32:21.613252+00 | 2026-09-27 13:42:45.062873+00 |
| za | 2 | 2026-09-27 13:31:59.956559+00 | 2026-09-27 13:42:41.757873+00 |

## Queries

1. ok: estimate 10,485,760 bytes, 10,485,760 bytes processed, 10,485,760 bytes billed, @names = enriched_content, trend_scores, seed_graph

```sql
SELECT table_name, table_type FROM `ogilvy-trends-v2.trends_v2_dev.INFORMATION_SCHEMA.TABLES` WHERE table_name IN UNNEST(@names) ORDER BY table_name
```

2. ok: estimate 10,485,760 bytes, 10,485,760 bytes processed, 10,485,760 bytes billed, @names = enriched_content, seed_graph, trend_scores

```sql
SELECT table_name, column_name, data_type FROM `ogilvy-trends-v2.trends_v2_dev.INFORMATION_SCHEMA.COLUMNS` WHERE table_name IN UNNEST(@names) ORDER BY table_name, ordinal_position
```

3. ok: estimate 0 bytes, 0 bytes processed, 0 bytes billed

```sql
SELECT COUNT(*) AS total_rows FROM `ogilvy-trends-v2.trends_v2_dev.enriched_content`
```

4. ok: estimate 20,518,416 bytes, 20,518,416 bytes processed, 20,971,520 bytes billed

```sql
SELECT CAST(`market` AS STRING) AS market, COUNT(*) AS row_count, CAST(MIN(`published_at`) AS STRING) AS min_date, CAST(MAX(`published_at`) AS STRING) AS max_date FROM `ogilvy-trends-v2.trends_v2_dev.enriched_content` GROUP BY `market` ORDER BY row_count DESC LIMIT 101
```

5. ok: estimate 0 bytes, 0 bytes processed, 0 bytes billed

```sql
SELECT COUNT(*) AS total_rows FROM `ogilvy-trends-v2.trends_v2_dev.seed_graph`
```

6. ok: estimate 17,960,232 bytes, 17,960,232 bytes processed, 18,874,368 bytes billed

```sql
SELECT CAST(`market` AS STRING) AS market, COUNT(*) AS row_count, CAST(MIN(`trend_date`) AS STRING) AS min_date, CAST(MAX(`trend_date`) AS STRING) AS max_date FROM `ogilvy-trends-v2.trends_v2_dev.seed_graph` GROUP BY `market` ORDER BY row_count DESC LIMIT 101
```

7. ok: estimate 0 bytes, 0 bytes processed, 0 bytes billed

```sql
SELECT COUNT(*) AS total_rows FROM `ogilvy-trends-v2.trends_v2_dev.trend_scores`
```

8. ok: estimate 54,324 bytes, 54,324 bytes processed, 10,485,760 bytes billed

```sql
SELECT CAST(`market` AS STRING) AS market, COUNT(*) AS row_count, CAST(MIN(`trend_date`) AS STRING) AS min_date, CAST(MAX(`trend_date`) AS STRING) AS max_date FROM `ogilvy-trends-v2.trends_v2_dev.trend_scores` GROUP BY `market` ORDER BY row_count DESC LIMIT 101
```

9. ok: estimate 10,485,760 bytes, 10,485,760 bytes processed, 10,485,760 bytes billed, @names = enriched_content, seed_graph

```sql
SELECT table_name, table_type FROM `ogilvy-trends-v2.trends_v2_staging.INFORMATION_SCHEMA.TABLES` WHERE table_name IN UNNEST(@names) ORDER BY table_name
```

10. ok: estimate 10,485,760 bytes, 10,485,760 bytes processed, 10,485,760 bytes billed, @names = enriched_content, seed_graph

```sql
SELECT table_name, column_name, data_type FROM `ogilvy-trends-v2.trends_v2_staging.INFORMATION_SCHEMA.COLUMNS` WHERE table_name IN UNNEST(@names) ORDER BY table_name, ordinal_position
```

11. ok: estimate 0 bytes, 0 bytes processed, 0 bytes billed

```sql
SELECT COUNT(*) AS total_rows FROM `ogilvy-trends-v2.trends_v2_staging.enriched_content`
```

12. ok: estimate 1,128,860 bytes, 1,128,860 bytes processed, 10,485,760 bytes billed

```sql
SELECT CAST(`market` AS STRING) AS market, COUNT(*) AS row_count, CAST(MIN(`published_at`) AS STRING) AS min_date, CAST(MAX(`published_at`) AS STRING) AS max_date FROM `ogilvy-trends-v2.trends_v2_staging.enriched_content` GROUP BY `market` ORDER BY row_count DESC LIMIT 101
```

13. ok: estimate 0 bytes, 0 bytes processed, 0 bytes billed

```sql
SELECT COUNT(*) AS total_rows FROM `ogilvy-trends-v2.trends_v2_staging.seed_graph`
```

14. ok: estimate 2,520,000 bytes, 2,520,000 bytes processed, 10,485,760 bytes billed

```sql
SELECT CAST(`market` AS STRING) AS market, COUNT(*) AS row_count, CAST(MIN(`trend_date`) AS STRING) AS min_date, CAST(MAX(`trend_date`) AS STRING) AS max_date FROM `ogilvy-trends-v2.trends_v2_staging.seed_graph` GROUP BY `market` ORDER BY row_count DESC LIMIT 101
```

15. ok: estimate 10,485,760 bytes, 10,485,760 bytes processed, 10,485,760 bytes billed

```sql
SELECT table_name, table_type FROM `ogilvy-trends-v2.intelligence_42_sources_staging.INFORMATION_SCHEMA.TABLES` ORDER BY table_name
```

16. ok: estimate 10,485,760 bytes, 10,485,760 bytes processed, 10,485,760 bytes billed, @names = collection_receipts, enriched_content, pipeline_runs, raw_content, system_events

```sql
SELECT table_name, column_name, data_type FROM `ogilvy-trends-v2.intelligence_42_sources_staging.INFORMATION_SCHEMA.COLUMNS` WHERE table_name IN UNNEST(@names) ORDER BY table_name, ordinal_position
```

17. ok: estimate 16 bytes, 16 bytes processed, 10,485,760 bytes billed

```sql
SELECT COUNT(*) AS total_rows, CAST(MIN(`recorded_at`) AS STRING) AS min_date, CAST(MAX(`recorded_at`) AS STRING) AS max_date FROM `ogilvy-trends-v2.intelligence_42_sources_staging.collection_receipts`
```

18. ok: estimate 0 bytes, 0 bytes processed, 0 bytes billed

```sql
SELECT COUNT(*) AS total_rows FROM `ogilvy-trends-v2.intelligence_42_sources_staging.enriched_content`
```

19. ok: estimate 14,092 bytes, 14,092 bytes processed, 10,485,760 bytes billed

```sql
SELECT CAST(`market` AS STRING) AS market, COUNT(*) AS row_count, CAST(MIN(`published_at`) AS STRING) AS min_date, CAST(MAX(`published_at`) AS STRING) AS max_date FROM `ogilvy-trends-v2.intelligence_42_sources_staging.enriched_content` GROUP BY `market` ORDER BY row_count DESC LIMIT 101
```

20. ok: estimate 0 bytes, 0 bytes processed, 0 bytes billed

```sql
SELECT COUNT(*) AS total_rows FROM `ogilvy-trends-v2.intelligence_42_sources_staging.pipeline_runs`
```

21. ok: estimate 36 bytes, 36 bytes processed, 10,485,760 bytes billed

```sql
SELECT CAST(`market` AS STRING) AS market, COUNT(*) AS row_count, CAST(MIN(`started_at`) AS STRING) AS min_date, CAST(MAX(`started_at`) AS STRING) AS max_date FROM `ogilvy-trends-v2.intelligence_42_sources_staging.pipeline_runs` GROUP BY `market` ORDER BY row_count DESC LIMIT 101
```

22. ok: estimate 0 bytes, 0 bytes processed, 0 bytes billed

```sql
SELECT COUNT(*) AS total_rows FROM `ogilvy-trends-v2.intelligence_42_sources_staging.raw_content`
```

23. ok: estimate 14,092 bytes, 14,092 bytes processed, 10,485,760 bytes billed

```sql
SELECT CAST(`market` AS STRING) AS market, COUNT(*) AS row_count, CAST(MIN(`published_at`) AS STRING) AS min_date, CAST(MAX(`published_at`) AS STRING) AS max_date FROM `ogilvy-trends-v2.intelligence_42_sources_staging.raw_content` GROUP BY `market` ORDER BY row_count DESC LIMIT 101
```

24. ok: estimate 0 bytes, 0 bytes processed, 0 bytes billed

```sql
SELECT COUNT(*) AS total_rows FROM `ogilvy-trends-v2.intelligence_42_sources_staging.system_events`
```

25. ok: estimate 84 bytes, 84 bytes processed, 10,485,760 bytes billed

```sql
SELECT CAST(`market` AS STRING) AS market, COUNT(*) AS row_count, CAST(MIN(`event_time`) AS STRING) AS min_date, CAST(MAX(`event_time`) AS STRING) AS max_date FROM `ogilvy-trends-v2.intelligence_42_sources_staging.system_events` GROUP BY `market` ORDER BY row_count DESC LIMIT 101
```
