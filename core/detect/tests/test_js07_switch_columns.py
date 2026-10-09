"""Release B source commit precondition JS-07 (W8-REL-B v2.1 section 5): under locality authority v1 every statement B's jobs run
that names eligible_v1, locality_basis or locality_status binds on the a80 item_state (36 columns); the state INSERT names exactly
the 36 a80 columns; the item_state CREATE text equals a80's; no file in SQL_FILES carries an item_state ALTER.

test_switch_columns_absent.py is the starting point and pins the statements it enumerates. These tests close what it leaves to
a hand written list: any other file that names a switch column is found by a scan of the tree, every SQL file is rewritten for
v1 and read, the three statements it only checks as text are bound on the harness, and the schema side is judged against
a80's own text."""
import re
import subprocess
from datetime import date
from pathlib import Path

import pytest

from core.brief import job as brief_job
from core.detect import job, seeds, sqlrun
from core.detect.tests import duck
from core.detect.tests.test_switch_columns_absent import PARAMS, READ, a80_table, statements_reading_the_columns  # noqa: F401
from core.detect.tests.test_state_columns import insert_shape
from core.schema.tests.test_switch_columns import A80_ITEM_STATE
from core.setup import durable_effects_check as de
from core.setup.release import jobs_source as js

ROOT = Path(__file__).resolve().parents[3]
A80 = "a80be1dee7f4ee2aa9775d0f353bab930809f057"
COLUMN = re.compile(r"\b(?:eligible_v1|locality_basis|locality_status)\b")
# Every non-test file under core/ whose code names a switch column, as found at this checkout. A new one fails the first test.
NAMING_FILES = {
    "core/brief/job.py", "core/brief/sql/brief.sql", "core/detect/job.py", "core/detect/scorecard.py", "core/detect/sql/scorecard.sql",
    "core/detect/sql/seeds.sql", "core/detect/sql/state.sql", "core/detect/sql/watches.sql", "core/detect/sqlrun.py",
    "core/eval/quality_score.py", "core/schema/locality_switch.sql", "core/trust/gate.py", "core/trust/locality.py",
}


def a80_tree():
    if subprocess.run(["git", "cat-file", "-e", A80], cwd=ROOT, capture_output=True).returncode != 0:
        pytest.fail(f"commit {A80} is not available in this checkout")
    return de.git_tree_files(A80, "core", ROOT)


def strip_comments(text):
    return re.sub(r"--[^\n]*", "", re.sub(r"(?m)^\s*#.*$", "", text))


def test_js07_only_the_known_files_name_a_switch_column():
    found = {p for p, t in js.tree_from_disk(ROOT)
             if not de.is_test_path(p) and p.endswith((".py", ".sql")) and COLUMN.search(strip_comments(t))}
    assert found == NAMING_FILES, sorted(found ^ NAMING_FILES)


def test_js07_the_state_insert_under_v1_names_exactly_the_36_a80_columns_and_no_switch_column():
    values, named = insert_shape(job.state_script("v1", "core", "agent"))
    assert named == A80_ITEM_STATE and values == 36
    assert not COLUMN.search(strip_comments(job.state_script("v1", "core", "agent")).split("INSERT INTO", 1)[1].split("SELECT", 1)[0])


def test_js07_the_brief_statements_bind_on_the_36_column_table_and_seeds_fails_only_on_a_harness_gap(a80_table, monkeypatch):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v1")
    names = ("candidates", "candidates_without_locality", "candidates_without_news_column", "candidates_without_locality_or_news_column")
    for name in names:
        sql = brief_job.query_sql(name)
        used = set(re.findall(r"@(\w+)", sql))
        # candidates and candidates_without_locality read market_news_posts7, which the head view has; the other two never do
        duck.query(a80_table, sql, {k: v for k, v in dict(PARAMS, market="ZA").items() if k in used})      # binds, or raises
    # seeds.candidates reads clusters.keywords, a column the DuckDB harness table lacks. It is the only thing that stops it
    # binding here: the error names that column and no switch column, which v1 has already replaced with a typed NULL.
    sql = seeds.statements()["candidates"]
    used = set(re.findall(r"@(\w+)", sql))
    with pytest.raises(Exception) as error:
        duck.query(a80_table, sql, {k: v for k, v in dict(PARAMS, market="ZA").items() if k in used})
    assert "keywords" in str(error.value) and not COLUMN.search(str(error.value).split("LINE 1")[0])


def test_js07_the_item_state_create_text_equals_a80s_by_whole_statement_hash():
    head = js.item_state_create(js.tree_from_disk(ROOT))
    old = js.item_state_create(a80_tree(), ("agent.sql", "core.sql"))
    assert len(head) == len(old) == 1
    assert de.sha_text(head[0]) == de.sha_text(old[0])


def test_js07_no_file_in_sql_files_carries_an_item_state_alter_and_the_switch_file_is_outside_them():
    files = js.tree_from_disk(ROOT)
    names = js.sql_file_names(files)
    assert "locality_switch.sql" not in names
    for name in names:
        for statement in de.split_statements(dict(files)[f"core/schema/{name}"]):
            assert not re.match(r"ALTER\s+TABLE\s+`[^`]*item_state`", statement, re.I), (name, statement[:80])
    assert any("item_state" in s for s in de.split_statements(dict(files)["core/schema/locality_switch.sql"]))


def test_js07_a_new_statement_that_reads_a_switch_column_under_v1_would_be_found():
    sql = "SELECT s.eligible_v1 FROM core.item_state s"
    assert COLUMN.search(sql) and not COLUMN.search(sqlrun.for_authority(sql, "v1"))
    unrewritten = "SELECT s.locality_status FROM core.item_state s"
    assert READ.search(unrewritten)
