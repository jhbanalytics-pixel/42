"""Dry-run the detect views of this checkout on staging BigQuery before a jobs deploy, so a statement BigQuery
rejects is found before the 02:00 detect run applies the file (core/detect/job.py, first and unguarded step).

    py -3.13 ops/runners/dryrun_views.py

Run from the repo root on Albert's PC with his own Google login (ADC). Every statement goes to BigQuery with
dry_run=True: BigQuery checks the statement and estimates bytes, and creates, changes and bills nothing. It never
calls the bq CLI and writes no file.

A view that reads another view changed in the same file is checked against the live version of that view, so a
FAIL naming a column the new upstream view adds can be a false alarm; the printed error says which column.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PROJECT = "ogilvy-trends-v2"


def check(client, stmts, name_of, config):
    """[(object name, None or error text)] for each statement, in file order."""
    out = []
    for stmt in stmts:
        try:
            client.query(stmt, job_config=config)
            out.append((name_of(stmt), None))
        except Exception as exc:  # each statement is reported, none stops the rest
            out.append((name_of(stmt), f"{type(exc).__name__}: {str(exc)[:400]}"))
    return out


def main():
    from google.cloud import bigquery

    from core.detect import sqlrun

    client = bigquery.Client(project=PROJECT)
    config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    results = check(client, sqlrun.statements(), sqlrun.object_name, config)
    for name, error in results:
        print(f"{'OK  ' if error is None else 'FAIL'} {name}" + ("" if error is None else f"\n     {error}"))
    failed = sum(1 for _, e in results if e)
    print(f"\n{len(results) - failed} of {len(results)} statements OK, {failed} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
