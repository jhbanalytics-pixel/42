"""Read-only check of why clusters, coord_signals and breakout_signals stay empty on 42 staging.

    py -3.13 ops/runners/check_empty_stages.py

Run it from the root of a checkout. Needs google-cloud-bigquery and gcloud ADC on this PC. SELECT only, each query
capped at 1 GB billed; nothing is written.

What it prints:
  RUNS     one line per runs row for understand, coaction and breakout since 2026-09-27: date, stage, status, start
           time, error, then understand's cluster counts per market (skipped reason, posts, today_posts, error) or
           the other stage's counts.
  TABLES   rows and last day since 2026-08-01 in clusters, coord_signals and breakout_signals.
"""

P = "ogilvy-trends-v2"
CAP = 10**9

RUNS_SQL = f"""
SELECT run_date, stage, status, started_at,
  LEFT(IFNULL(error, ''), 300) AS error,
  CASE WHEN stage = 'understand' THEN TO_JSON_STRING(JSON_QUERY(counts, '$.cluster'))
       ELSE LEFT(TO_JSON_STRING(counts), 400) END AS detail
FROM `{P}.intelligence_42_agent.runs`
WHERE stage IN ('understand', 'coaction', 'breakout') AND run_date >= '2026-09-27'
ORDER BY run_date, stage, started_at
"""

TABLES_SQL = f"""
SELECT 'clusters' AS t, COUNT(*) AS n, MAX(cluster_date) AS last_day
FROM `{P}.intelligence_42_core.clusters` WHERE cluster_date >= '2026-08-01'
UNION ALL
SELECT 'coord_signals', COUNT(*), MAX(metric_date)
FROM `{P}.intelligence_42_core.coord_signals` WHERE metric_date >= '2026-08-01'
UNION ALL
SELECT 'breakout_signals', COUNT(*), MAX(metric_date)
FROM `{P}.intelligence_42_core.breakout_signals` WHERE metric_date >= '2026-08-01'
"""


def run_line(r):
    started = f"{r.started_at:%H:%MZ}" if r.started_at else "--:--"
    return (f"{r.run_date} {(r.stage or '-'):<10} {(r.status or '-'):<8} {started}  "
            f"err={r.error or '-'} | {r.detail or '-'}")


def main(client=None):
    from google.cloud import bigquery

    client = client or bigquery.Client(project=P)

    def config():
        return bigquery.QueryJobConfig(maximum_bytes_billed=CAP)

    print("RUNS (understand shows its cluster counts per market; others show their counts)")
    n = 0
    for r in client.query(RUNS_SQL, job_config=config()).result():
        n += 1
        print(run_line(r))
    print(f"runs rows: {n}")
    print("TABLES since 2026-08-01")
    for r in client.query(TABLES_SQL, job_config=config()).result():
        print(f"{r.t:<17} rows={r.n} last={r.last_day}")


if __name__ == "__main__":
    main()
