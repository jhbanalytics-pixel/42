"""BigQuery dry runs of every locality_v2 statement, against staging (project ogilvy-trends-v2).

The DuckDB harness accepts forms BigQuery may refuse, and nothing in the locality work has run on BigQuery (C4 v3
section 27, review F11). These are the dry runs to make before the release: the locality script and its two
inserts, the two views, the step's reads, the cross-tabulation, the state script under both authorities, the
label_shared_members measurement and the brief's candidate, not_local_audit and unreadable_audit statements.

The dry runs are skipped unless F42_BQ=1. Every job is a dry run: BigQuery parses, resolves names and plans the query
without running it, so nothing is created, written or billed. They resolve names, so the locality tables of
core/schema/core.sql and the views of locality_views.sql must exist on the dataset first; run them after the DDL.
The parameter check at the bottom needs no network and always runs."""

import os
import re
from datetime import date, datetime, timezone

import pytest

from core.brief import job as brief_job
from core.detect import job, locality, sqlrun
from core.detect.tests.duck import split_create

PROJECT = "ogilvy-trends-v2"
D = date(2026, 10, 7)
KEY = {"d": D, "detect_run_id": "detect-20261007-000000000000", "metric_version": "locality_v2.1"}
SCRIPT = {**KEY, "cutoff": datetime(2026, 10, 7, 3, tzinfo=timezone.utc)}
STATE = {"d": D, "run_id": KEY["detect_run_id"], "rule_version": "warmup-1"}
BRIEF = {"d": D, "market": "ZA"}
SHADOW = {"d": D, "detect_run_id": KEY["detect_run_id"]}
SHARED = {"start": date(2026, 10, 1), "end": D}


def body_with_params(stmt):
    """A CREATE statement's query body; a table function's bare parameter d becomes @d."""
    _, _, names, body = split_create(stmt)
    for name in names:
        body = re.sub(rf"(?<![\w.@]){name}(?!\w)", f"@{name}", body)
    return body


def cases():
    out = [
        ("script", locality._script(sqlrun.CORE, sqlrun.AGENT), SCRIPT),
        ("members", sqlrun.render((locality.SQL / "locality_members.sql").read_text(encoding="utf-8")), SCRIPT),
        ("summary", sqlrun.render((locality.SQL / "locality_summary.sql").read_text(encoding="utf-8")), SCRIPT),
        ("step_existing", "SELECT COUNT(*) n FROM {core}.item_locality WHERE " + locality._KEY, KEY),
        ("step_members", locality.MEMBERS_SQL, KEY), ("step_summary", locality.SUMMARY_SQL, KEY),
        ("step_verified", locality.VERIFIED_SQL, KEY), ("step_series", locality.SERIES_WITHOUT_ROW_SQL, KEY),
        ("crosstab", locality.CROSSTAB_SQL, SHADOW), ("disagree", locality.DISAGREE_SQL, SHADOW),
        ("state_v1", job.state_script("v1"), {**STATE, "authority": "v1"}),
        ("state_v2", job.state_script("v2"), {**STATE, "authority": "v2"}),
        ("label_shared_members", sqlrun.render((locality.SQL / "label_shared_members.sql").read_text(encoding="utf-8")),
         SHARED),
        ("candidates", brief_job.QUERIES["candidates"], BRIEF),
        ("candidates_without_locality", brief_job.QUERIES["candidates_without_locality"], BRIEF),
        ("not_local_audit", brief_job.QUERIES["not_local_audit"], BRIEF),
        ("unreadable_audit", brief_job.QUERIES["unreadable_audit"], {**BRIEF, "run_id": KEY["detect_run_id"]}),
    ]
    out += [(f"view_{sqlrun.object_name(s).split('.')[-1]}", body_with_params(s), {"d": D} if split_create(s)[2] else {})
            for s in sqlrun.locality_view_statements()]
    return [(name, sqlrun.render(sql), params) for name, sql, params in out]


CASES = cases()


def test_every_parameter_a_locality_statement_uses_is_supplied_to_its_dry_run():
    for name, sql, params in CASES:
        used = set(re.findall(r"(?<![\w@])@(\w+)", sql))
        assert used <= set(params), (name, sorted(used - set(params)))


def test_the_statements_are_the_ones_the_job_runs():
    names = [name for name, _, _ in CASES]
    assert names.count("state_v1") == names.count("state_v2") == 1 and "script" in names
    assert any(name.startswith("view_") for name in names) and len(names) == len(set(names))


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
@pytest.mark.parametrize(("sql", "params"), [(sql, params) for _, sql, params in CASES], ids=[n for n, _, _ in CASES])
def test_dry_run(sql, params):
    from google.cloud import bigquery

    client = bigquery.Client(project=PROJECT)
    config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False,
                                     query_parameters=[sqlrun._param(k, v) for k, v in params.items()])
    assert client.query(sql, job_config=config).dry_run
