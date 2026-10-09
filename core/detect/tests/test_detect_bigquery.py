"""BigQuery dry runs of every detect SQL statement against staging (project ogilvy-trends-v2).

Skipped unless F42_BQ=1. Every job here is a dry run: BigQuery parses, resolves names and plans the
query without running it, so nothing is created, written or billed. The views and table functions
already exist on staging, so the bodies below resolve their dependencies.
"""

import os
import re
from datetime import date
from pathlib import Path

import pytest
from google.cloud import bigquery

from .. import aggregate, job, legacy, sqlrun, stats
from .duck import split_create, strip_leading_comments

pytestmark = pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")

PROJECT = "ogilvy-trends-v2"
SQL = Path(__file__).resolve().parents[1] / "sql"
D = date(2026, 9, 20)
PARAMS = {"d": D, "run_id": "detect-20260920-000000000000", "rule_version": "r1"}


@pytest.fixture(scope="module")
def client():
    return bigquery.Client(project=PROJECT)


def dry_run(client, sql, params=(), arrays=()):
    """Dry-run one query or script; scalar params use sqlrun's type inference. Returns the job."""
    config = bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False,
        query_parameters=[sqlrun._param(k, v) for k, v in dict(params).items()] + list(arrays))
    result = client.query(sql, job_config=config)
    assert result.dry_run
    return result


def body_with_params(stmt):
    """A CREATE statement's query body; a table function's bare parameter d becomes @d."""
    _, _, names, body = split_create(stmt)
    for name in names:
        body = re.sub(rf"(?<![\w.@]){name}(?!\w)", f"@{name}", body)
    return body


def file_statements(name):
    text = sqlrun.render((SQL / name).read_text(encoding="utf-8"))
    return [strip_leading_comments(s) for s in sqlrun.split(text)]


CREATES = sqlrun.statements() + file_statements("waves.sql") + sqlrun.spread_statements()


@pytest.mark.parametrize("stmt", CREATES, ids=[sqlrun.object_name(s) for s in CREATES])
def test_view_and_table_function_bodies(client, stmt):
    dry_run(client, body_with_params(stmt), {"d": D})


def test_aggregate_insert(client):
    dry_run(client, sqlrun.render(aggregate.aggregate_sql()), PARAMS)


def test_aggregate_reads_and_writes(client):
    post_items = [{"post_id": "p1", "item_id": "i1", "via": "hashtag"}]
    items = [{"item_id": "i1", "kind": "hashtag", "canonical_key": "amapiano", "label": "#Amapiano",
              "first_seen": D, "first_seen_market": "ZA", "first_seen_platform": "tiktok", "last_seen": D,
              "status": "generic"}]
    dry_run(client, sqlrun.render(aggregate.FIRST_SIGHTED_SQL), {"d": D})
    dry_run(client, sqlrun.render(aggregate.POST_ITEMS_INSERT_SQL),
            arrays=[aggregate._struct_array("rows", post_items, aggregate.POST_ITEM_FIELDS)])
    dry_run(client, sqlrun.render(aggregate.cultural_map_merge_sql()),
            arrays=[aggregate._struct_array("items", items, aggregate.ITEM_FIELDS)])


def test_legacy_merge(client):
    items = [{"item_id": "i1", "kind": "hashtag", "canonical_key": "fyp", "label": "#FYP",
              "first_seen": date(2026, 6, 21), "first_seen_market": "ZA", "first_seen_platform": "tiktok",
              "last_seen": date(2026, 6, 23), "status": "generic"},
             {"item_id": "i2", "kind": "creator", "canonical_key": "tiktok:kabza_de_small", "label": "kabza_de_small",
              "first_seen": date(2026, 5, 10), "first_seen_market": "ZA", "first_seen_platform": "tiktok",
              "last_seen": date(2026, 7, 21), "status": "active"}]
    dry_run(client, sqlrun.render(legacy.statements()["merge"]),
            arrays=[aggregate._struct_array("items", items, aggregate.ITEM_FIELDS)])


def test_state_script(client):
    # BigQuery accepts a dry run of the whole script, temp function included
    dry_run(client, sqlrun.render((SQL / "state.sql").read_text(encoding="utf-8")), {**PARAMS, "authority": "v1"})


def test_sqlrun_query_bodies(client):
    dry_run(client, sqlrun.render(stats.SIGNAL_SQL), {"d": D})
    dry_run(client, sqlrun.render(job.ITEM_STATE_COUNT_SQL), {"d": D, "run_id": PARAMS["run_id"]})
    dry_run(client, sqlrun.render("SELECT * FROM {core}.tvf_item_window(@d)"), {"d": D})
    dry_run(client, sqlrun.render("SELECT * FROM {core}.tvf_placebo_base(@d)"), {"d": D})
