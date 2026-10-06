# V3.8 staging full checklist (2 Jul 2026)

Branch: `feat/v3-staging` (TEV2), `feat/lp-r-staging` (LP). All flags default false unless noted.

| Ticket | Status | Notes |
|---|---|---|
| A0 SCHEMA_ORDER | DONE | seed_insights, system_events, seed_graph, seed_candidates, seed_outcomes |
| A1 seed_graph table | DONE | migration + sql |
| A2 seed_graph builder | DONE | src/analysis/seed_graph.py |
| A3 cron wiring | DONE | SEED_GRAPH_ENABLED=false |
| A4 backfill | DONE | scripts/backfill_seed_graph.py |
| A5 seed_path | DONE | src/analysis/seed_path.py |
| A6a brief prompt + persist | DONE | SEED_PATH_RENDER_ENABLED gates writer |
| A6b email card | DONE | _seed_path in card.py |
| A7 near-miss | DONE | NEAR_MISS_CAPTURE_ENABLED=false |
| A8 structured ID capture | DONE | ensemble v2gcam side-channel + seed_graph parse |
| B0 vendor pre-call | DONE | fetch_units_history at cron start |
| B0b FORCE_REINGEST_MARKETS | DONE | per-execution only, cleanup DML, tests |
| B1 n_factual/n_social | DONE | CORROB_COUNTS_ENABLED=false, migration |
| C1 tables | DONE | seed_candidates + seed_outcomes sql/migrations |
| C2 seed_candidates ranker | DONE | src/analysis/seed_candidates.py + cron stage |
| C3 review CLI | DONE | scripts/review_seed_candidates.py |
| C4 taxonomy stub | DONE | scripts/propose_taxonomy_candidates.py dark |
| LP-R1 client metrics | DONE | prior commit ab90ac2 |
| LP-R2/R2b posts+comments | DONE | prior commit |
| LP-R3 consolidated doc | DONE | prior commit 4988603 |
| LP-R4 topic picker | DONE | TopicPicker + /api/market-topics + topics deep link |
| LP-R5 evidence pack | DONE | prior commit |
| LP1 fetch_seed_graph_adjacency/path | DONE | bq.py |
| LP2 /api/seed-path | DONE | main.py + chat tool |
| LP3 seedpath.jsx | DONE | App.jsx route |
| Track D Twitter | BLOCKED | WPP/Ogilvy legal sign-off |
| C4 live Gemini | BLOCKED | RECONCILE 7-day watch + propose stub |
| Prod flips | BLOCKED | staging visual QA + one flip per cron day |

Staging score: 28/30 code tickets DONE, 2 blocked on legal/reconcile gates.
