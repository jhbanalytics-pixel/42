"""Under the v1 authority nothing reads or writes the three switch columns of item_state (C4 v3 section 7.4).

The shadow release meets the table a80 left: 36 columns, no eligible_v1, locality_basis or locality_status. Every
statement that names them must then run on that table, so the harness table here is built without them. Under v2 the
statements are the files as written."""

import re
from datetime import date
from pathlib import Path

import pytest

from core.brief import job as brief_job
from core.detect import job, scorecard, seeds, sqlrun, watches
from core.detect.tests import duck
from core.detect.tests.duck import run_duck, temp_macro
from core.detect.tests.duck import strip_leading_comments as _strip
from core.detect.tests.test_state_locality import SQL, world
from core.schema.tests.test_switch_columns import SWITCH_COLUMNS

D = date(2026, 10, 7)
READ = re.compile(r"\b[sx]\.locality_(?:basis|status)\b")
THREE = ",\n  eligible_v1 BOOLEAN, locality_basis VARCHAR, locality_status VARCHAR);"


@pytest.fixture
def a80_table(monkeypatch):
    assert THREE in duck.TABLES
    monkeypatch.setattr(duck, "TABLES", duck.TABLES.replace(THREE, ");"))
    con = duck.connect()
    assert [r[0] for r in con.execute("DESCRIBE core.item_state").fetchall()][-1] == "base_state"
    for stmt in sqlrun.split(sqlrun.render((SQL / "waves.sql").read_text(encoding="utf-8"), "core", "agent")):
        con.execute(duck.create_statement(_strip(stmt)))
    return con


def statements_reading_the_columns():
    out = {f"watches.{k}": v for k, v in watches.statements().items()}
    out.update({f"seeds.{k}": v for k, v in seeds.statements().items()})
    out.update({f"scorecard.{k}": v for k, v in scorecard.queries(scorecard.load_reference()).items()})
    out.update({f"brief.{k}": brief_job.query_sql(k) for k in brief_job.QUERIES})
    return {k: v for k, v in out.items() if READ.search(v)}


def test_the_statements_that_read_the_columns_are_the_ones_expected(monkeypatch):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v2")
    assert sorted(statements_reading_the_columns()) == sorted([
        "watches.items", "seeds.candidates", "brief.candidates", "brief.candidates_without_locality",
        "scorecard.time_to_detect", "scorecard.lead_time", "scorecard.recall", "scorecard.breadth_platforms",
        "scorecard.locality_regime"])


def test_under_v1_no_statement_reads_a_switch_column(monkeypatch):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v1")
    assert statements_reading_the_columns() == {}


def test_under_v2_the_statements_are_the_files_as_written(monkeypatch):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v2")
    assert len(statements_reading_the_columns()) == 9


PARAMS = {"d": D, "run_id": "detect-1", "market": "ZA", "week_start": date(2026, 10, 5), "week_end": date(2026, 10, 11)}


def test_under_v1_the_watch_and_scorecard_statements_run_on_the_a80_table(a80_table, monkeypatch):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v1")
    texts = {f"watches.{k}": v for k, v in watches.statements().items() if k == "items"}
    texts.update({f"scorecard.{k}": v for k, v in scorecard.queries(scorecard.load_reference()).items()
                  if k in ("time_to_detect", "lead_time", "recall", "breadth_platforms", "locality_regime")})
    assert len(texts) == 6
    for name, sql in texts.items():
        used = set(re.findall(r"@(\w+)", sql))
        duck.query(a80_table, sql, {k: v for k, v in PARAMS.items() if k in used})        # binds, or raises


def test_under_v1_the_state_step_inserts_into_the_a80_table_and_writes_none_of_the_three(a80_table, monkeypatch):
    from core.conftest import set_locality_authority
    from core.detect.tests.test_detect_states import RULE, RUN
    from core.detect.tests.fixtures import D as DAY

    set_locality_authority(monkeypatch, "v1")
    world(a80_table)
    temp, insert = [_strip(s) for s in sqlrun.split(job.state_script("v1", "core", "agent"))]
    a80_table.execute(temp_macro(temp))
    run_duck(a80_table, insert, {"d": DAY, "run_id": RUN, "rule_version": RULE, "authority": "v1"})
    columns = [r[0] for r in a80_table.execute("DESCRIBE core.item_state").fetchall()]
    assert not set(SWITCH_COLUMNS) & set(columns)
    assert a80_table.execute("SELECT COUNT(*) FROM core.item_state").fetchone()[0] > 0
