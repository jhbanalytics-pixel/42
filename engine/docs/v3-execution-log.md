# V3 execution log

One row per phase gate. Paste evidence verbatim (bq-snapshot verdict lines, dry-run bytes, spot-check notes). Each flip PR links the row that justified it. LP1 merge gate requires a live row here.

Template columns: date | phase_gate | evidence_summary | flip_slot | PR

| date | phase_gate | evidence_summary | flip_slot | PR |
|---|---|---|---|---|
| 2026-07-02 | STAGING v3-full | feat/v3-staging: seed_graph+path+candidates dark merge; pytest unit green; flags default false | — | feat/v3-staging |

# History

First staging row before prod flip queue consumption.
| 2026-07-05 | FLIP app_charts | three-market live probe clean (za/ng/ke top-free apps.json, real feed envelope); consumer wired in run_rss_now (CONNECTORS + app_charts_rows + apps family); pipeline_runs.app_charts_rows live in BQ; unit suite 13/13 | app_charts | feat/flip-app-charts |
| 2026-07-05 | FLIP SEED_GRAPH_ENABLED | gate of 4 Jul executed 5 Jul: morning-check CLEAN (pipeline sent, 24/24 briefs, no connector-zero); seed_graph + seed_candidates tables live on trends_v2_dev; dev backfill present (240k rows); dry_run_sql 8/8 valid; wiring on master run_rss_now:2245 non-fatal | seed_graph | #242 |
| 2026-07-06 | FLIP NEAR_MISS_CAPTURE (pending) | Merge after seed_graph rows on trend_date 2026-07-06 (~5k/market); morning-check CLEAN; test_seed_graph 29 passed; parity wiring in enrichment.py | near_miss | feat/flip-near-miss-capture-2026-07-06 |
