"""Schema parity guard for pipeline_runs.

CI never executes a query (keyless), so a column that log_pipeline_run writes but
the committed DDL omits passes every other test and only fails at cron on a fresh
setup_bigquery, or silently regresses the health view on a create_looker_views
redeploy. This happened twice (search_velocity_score, then wikipedia_rows +
bluesky_rows). This test fails fast when the row dict and the committed schema or
view drift apart. See ledger CF-0004 and CF-0010.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_RUN = _ROOT / "scripts" / "run_rss_now.py"
_SCHEMA = _ROOT / "infra" / "bigquery_schemas" / "pipeline_runs.sql"
_VIEW = _ROOT / "infra" / "bigquery_views" / "v_pipeline_health.sql"


def _log_pipeline_run_row_columns() -> set[str]:
    """The *_rows keys written into the pipeline_runs row by log_pipeline_run."""
    src = _RUN.read_text(encoding="utf-8")
    start = src.index("def log_pipeline_run")
    nxt = src.index("\ndef ", start + 1)
    body = src[start:nxt]
    return set(re.findall(r'"([a-z0-9_]+_rows)":', body))


def test_pipeline_runs_row_columns_are_in_committed_schema():
    written = _log_pipeline_run_row_columns()
    assert "wikipedia_rows" in written
    assert "bluesky_rows" in written
    schema = _SCHEMA.read_text(encoding="utf-8")
    missing = sorted(c for c in written if not re.search(rf"\b{re.escape(c)}\b", schema))
    assert not missing, f"pipeline_runs.sql is missing columns log_pipeline_run writes: {missing}"


def _schema_columns() -> set[str]:
    schema = _SCHEMA.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*([a-z0-9_]+)\s+(?:INT64|FLOAT64|STRING|TIMESTAMP)", schema, re.M))


def test_health_view_surfaces_the_wave2_connector_rows():
    # The view surfaces every per-source connector count; the 19 Jun drift was
    # the view dropping these two while the migration added them live. After
    # #246 the same class covers youtube_scrape_rows + socialcrawl_rows.
    view = _VIEW.read_text(encoding="utf-8")
    for col in (
        "wikipedia_rows",
        "bluesky_rows",
        "youtube_scrape_rows",
        "socialcrawl_rows",
    ):
        assert re.search(rf"\b{re.escape(col)}\b", view), f"v_pipeline_health.sql dropped {col}"


def test_health_view_has_no_dangling_pipeline_runs_columns():
    # Every column the view reads via COALESCE(<col>, 0) must exist on the table,
    # so a view redeploy cannot reference a column a fresh setup never created.
    view = _VIEW.read_text(encoding="utf-8")
    referenced = set(re.findall(r"COALESCE\(([a-z0-9_]+),\s*0\)", view))
    schema_cols = _schema_columns()
    dangling = sorted(c for c in referenced if c not in schema_cols)
    assert not dangling, (
        f"v_pipeline_health.sql references columns absent from pipeline_runs.sql: {dangling}"
    )
