#!/usr/bin/env python
"""Dry-run the pipeline's live BigQuery queries to catch SQL that unit tests miss.

CI runs only ``pytest tests/unit/`` with no GCP credentials, so it never executes
a query. A query with a bad column or a syntax error passes every unit test (the
tests assert the SQL string contains the right fragments, not that BigQuery
accepts it) and fails for the first time at cron. Worse, several producers are
non-fatal by contract: they wrap the read in try/except and return an empty
result, so a broken query does not crash the run, it silently renders an empty
section and the regression is invisible until someone reads the output. The old
engine_evolve ROWS-alias bug shipped exactly this way. See ledger CF-0007.

This script closes that gap. It forces ``dry_run=True`` on the BigQuery client and
invokes each producer, so the real SQL (parameters and all) is validated
server-side at no cost. It records the validity at query-submit time, before a
producer can swallow the error, so a silent failure is still caught. Exit code is
1 if any query is invalid.

Run before merging a SQL change, and from morning-check as a daily pre-flight:

    python scripts/dry_run_sql.py [YYYY-MM-DD]

Defaults to yesterday (the date the last cron scored). Needs application-default
credentials for the engine project, the same as any local BigQuery read. When you
add a new live query, add its producer to REGISTRY below.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.utils.bigquery as _bq
from google.cloud import bigquery

# (sql, error) for every query that failed dry-run validation, captured at submit
# time so a producer's own try/except cannot hide it. Plain count of valid ones.
_FAILURES: list[tuple[str, str]] = []
_VALID: list[str] = []

_real_get_client = _bq.get_client


class _DryRunClient:
    """Wraps the real client and forces every query to a dry run.

    A dry-run job is validated synchronously by BigQuery and never scans data, so
    an invalid query raises at submit time. We record that and re-raise, so a
    producer that swallows the error still leaves a trace here. Everything else
    proxies straight through to the real client."""

    def __init__(self, real: bigquery.Client) -> None:
        self._real = real

    @property
    def project(self) -> str:
        return self._real.project

    def query(self, sql, job_config=None, **kwargs):
        cfg = job_config or bigquery.QueryJobConfig()
        cfg.dry_run = True
        cfg.use_query_cache = False
        try:
            job = self._real.query(sql, job_config=cfg, **kwargs)
            _VALID.append(sql)
            return job
        except Exception as exc:
            _FAILURES.append((sql, repr(exc)))
            raise

    def __getattr__(self, name):
        return getattr(self._real, name)


def _patched_get_client() -> _DryRunClient:
    return _DryRunClient(_real_get_client())


# Patch before importing the producers so they bind the dry-run client at import.
_bq.get_client = _patched_get_client

from src.analysis import event_ledger, pan_african, tone_lexicon, wave1_badges
from src.scoring import forecast


def _ledger_factual_read(trend_date):
    """Validate the event ledger's candidate BQ read for one market.

    _fetch_factual_rows takes (client, dataset, trend_date, market), not the
    fn(trend_date) contract the rest of the REGISTRY uses, so wrap it. The
    client comes from the patched _bq.get_client (a dry-run client), so the
    SELECT is validated server-side without scanning. One market is enough to
    exercise the SQL; the query is identical across markets bar a bound param.
    """
    client = _bq.get_client()
    dataset = _bq.get_dataset()
    return event_ledger._fetch_factual_rows(client, dataset, trend_date, "za")


def _render_payload_merge(trend_date):
    """Validate the MERGE that writes conversation enrichment onto stored briefs.

    persist_render_payloads takes (trend_date, briefs_by_topic) rather than the
    fn(trend_date) contract the rest of the REGISTRY uses, so wrap it with one
    representative brief; the SQL shape does not vary with brief count. The
    client is the patched dry-run client, so the MERGE is validated server-side
    and writes nothing.

    This one is here for a reason. It is DML, so no unit test executed it, and it
    raised "Unsupported subquery with table in join predicate" on every run for at
    least four consecutive days while its caller logged the error as non-fatal and
    carried on. That is precisely the silent-failure class in the module docstring.
    """
    from src.analysis.generate_briefs import persist_render_payloads

    return persist_render_payloads(
        trend_date,
        {("za", "music_amapiano"): {"display": {"state": {"badge": "Holding"}}}},
    )


# Every live read that builds SQL keyed on a date. Each takes one trend_date arg.
# Add a producer here when you ship a new query so the daily check covers it.
REGISTRY = [
    ("tone_lexicon.fetch_lexicon_scores", tone_lexicon.fetch_lexicon_scores),
    ("pan_african.read_pan_african_stories", pan_african.read_pan_african_stories),
    ("wave1_badges.fetch_continuity_lifecycle", wave1_badges.fetch_continuity_lifecycle),
    ("wave1_badges.fetch_seed_scores", wave1_badges.fetch_seed_scores),
    ("forecast.compute_forecast_outlook", forecast.compute_forecast_outlook),
    ("event_ledger._fetch_factual_rows", _ledger_factual_read),
    ("generate_briefs.persist_render_payloads", _render_payload_merge),
]


def main() -> int:
    if len(sys.argv) > 1:
        date = datetime.date.fromisoformat(sys.argv[1])
    else:
        date = datetime.date.today() - datetime.timedelta(days=1)
    print(f"Dry-running live BigQuery queries for {date}\n")

    invalid = 0
    skipped = 0
    for label, fn in REGISTRY:
        ran_before = len(_VALID)
        failed_before = len(_FAILURES)
        try:
            fn(date)
        except Exception as exc:
            # A producer that swallows BQ errors returns normally and the failure
            # is already in _FAILURES. Anything that reaches here without a
            # recorded failure is a python-level invoke problem, not bad SQL.
            if len(_FAILURES) == failed_before:
                print(f"  ERROR  {label}: could not invoke ({exc!r})")
                continue
        new_failures = _FAILURES[failed_before:]
        new_valid = len(_VALID) - ran_before
        if new_failures:
            invalid += len(new_failures)
            for _sql, err in new_failures:
                print(f"  FAIL   {label}: {err}")
        elif new_valid == 0:
            skipped += 1
            print(f"  SKIP   {label}: ran no query (flag-gated off or early return)")
        else:
            print(f"  OK     {label}: {new_valid} query(ies) valid")

    print(f"\n{len(_VALID)} queries valid, {invalid} invalid, {skipped} skipped.")
    if invalid:
        print("SQL VALIDATION FAILED. A live query will break or silently empty at cron.")
        return 1
    print("All live queries parse against BigQuery.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
