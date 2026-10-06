"""Standalone idempotency-guard utility for the daily pipeline.

Exit code 0  -> today's pipeline has NOT yet succeeded; a run should proceed.
Exit code 1  -> today's pipeline already has at least one success row in
                ``pipeline_runs``; a run should skip to avoid INSERT
                double-writes into ``raw_content`` / ``enriched_content``.

Required env:
    GCP_PROJECT       GCP project hosting BigQuery, e.g. ``ogilvy-trends-v2``.
    BIGQUERY_DATASET  Dataset BASE name, e.g. ``trends_v2``. The runtime suffix
                      (``_{TRENDS_ENV}``) is appended by ``get_dataset()`` so
                      ``trends_v2`` + ``dev`` -> ``trends_v2_dev``. This script
                      MUST go through ``get_dataset()`` rather than reading the
                      env var raw, otherwise it points at a non-existent
                      dataset and the BigQuery API returns 404. Bug shipped
                      8 May 2026 in ``db14420``, fixed 9 May 2026 after the
                      guard caused the workflow to skip the day's run.
    TRENDS_ENV        Environment name, e.g. ``dev`` or ``prod``. Defaults
                      to ``dev`` via ``get_dataset``.
    GOOGLE_APPLICATION_CREDENTIALS  Path to service-account JSON. On Cloud Run
                                    ADC supplies creds, so this is unset there.

Standalone manual check. The live cron does NOT call this: the Cloud Run job
entrypoint (scripts/run_rss_now.py) carries its own per-market guard that skips
markets already marked success today, so the 00:30 primary and 02:30 fallback
cannot double-ingest. This script stays as a quick "did today run" probe.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Make the repo root importable so we can reuse get_dataset() from
# src/utils/bigquery.py. Same pattern as scripts/run_rss_now.py:42.
sys.path.insert(0, str(Path(__file__).parent.parent))

from google.cloud import bigquery
from src.utils.bigquery import get_dataset


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: required env var {name} is empty.", file=sys.stderr)
        sys.exit(2)
    return value


def main() -> int:
    project = _required_env("GCP_PROJECT")
    # Touch BIGQUERY_DATASET to keep the env-var-required contract; the actual
    # dataset name comes from get_dataset() which appends the env suffix.
    _required_env("BIGQUERY_DATASET")
    dataset = get_dataset()
    today = datetime.now(UTC).date().isoformat()

    client = bigquery.Client(project=project)
    table = f"`{project}.{dataset}.pipeline_runs`"
    sql = f"SELECT COUNT(*) AS n FROM {table} WHERE DATE(started_at) = @d AND status = 'success'"
    cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", today)]
    )
    rows = list(client.query(sql, job_config=cfg).result())
    n = rows[0].n if rows else 0

    print(f"pipeline_runs success count for {today} in {dataset}: {n}")

    if n > 0:
        print(
            f"Today's pipeline already succeeded ({n} success rows). "
            "Exiting 1 so the workflow skips the run."
        )
        return 1

    print("Today's pipeline has not yet succeeded. Workflow will proceed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
