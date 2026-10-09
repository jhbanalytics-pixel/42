"""The three item_state columns of the locality switch (C4 v3 section 7.4) are not in the schema the shadow release applies.

a80's detect job writes item_state with a positional INSERT that supplies 36 values. BigQuery refuses a positional insert
whose value count differs from the table's, so a table widened by any release would fail every a80 detect run and make a
rollback to a80 impossible. The columns therefore live in their own file, which only the switch release applies, and the
INSERT detect runs under the v1 authority names exactly the 36 columns a80's table has."""

import subprocess
from pathlib import Path

import pytest

from core.detect import job
from core.schema import apply
from core.detect.tests.test_state_columns import insert_shape

SCHEMA_DIR = Path(__file__).resolve().parents[1]
SWITCH = SCHEMA_DIR / "locality_switch.sql"
SWITCH_COLUMNS = ["eligible_v1", "locality_basis", "locality_status"]

# item_state at a80be1d, in table order (core/schema/core.sql there), 36 columns.
A80_ITEM_STATE = [
    "metric_date", "market", "item_id", "kind", "state_raw", "state", "untested", "main_series_id", "main_y", "main_mu",
    "main_ratio", "q_min", "sig_days3", "creators3", "posts3", "top_creator_share3", "authenticity", "share_flags",
    "sponsored_share", "geo_status", "local_share", "geo_known_posts7", "spread_platforms", "found_platforms",
    "markets_hot", "lead_market", "diffusion", "novelty", "last_wave", "moment", "eligible", "worth_raw", "worth_pct",
    "run_id", "rule_version", "base_state"]


def item_state_create(statements):
    [create] = [s for s in statements if "intelligence_42_core.item_state`" in s and s.upper().startswith("CREATE TABLE")]
    return create


def table_columns(create):
    body = create[create.index("(") + 1:create.rindex(")\nPARTITION")]
    depth, pieces, current = 0, [], ""
    for ch in body:
        depth += ch in "(<"
        depth -= ch in ")>"
        if ch == "," and depth == 0:
            pieces.append(current)
            current = ""
        else:
            current += ch
    pieces.append(current)
    return [p.split()[0] for p in pieces if p.strip()]


def test_the_pinned_names_are_the_36_columns_of_a80s_table():
    assert len(A80_ITEM_STATE) == 36 and len(set(A80_ITEM_STATE)) == 36
    shown = subprocess.run(["git", "show", "a80be1d:core/schema/core.sql"], capture_output=True, cwd=SCHEMA_DIR)
    if shown.returncode != 0:
        pytest.skip("a80be1d is not in this repository's history")
    statements = apply.split_statements(shown.stdout.decode("utf-8"))
    assert table_columns(item_state_create(statements)) == A80_ITEM_STATE


def test_the_default_schema_apply_leaves_item_state_at_the_a80_columns():
    statements = apply.load_statements()
    assert table_columns(item_state_create(statements)) == A80_ITEM_STATE
    for statement in statements:
        assert not ("ALTER" in statement.upper().split("`")[0] and ".item_state`" in statement), statement[:90]


def test_the_switch_columns_are_in_their_own_file_that_the_default_apply_does_not_read():
    assert SWITCH not in apply.SQL_FILES
    statements = apply.split_statements(SWITCH.read_text(encoding="utf-8"))
    assert [apply.kind(s) for s in statements] == ["alter"] * 3
    assert [s.split()[-2] for s in statements] == SWITCH_COLUMNS
    assert all(".item_state`" in s for s in statements)


def sql_for(authority):
    return job.state_script(authority, "core", "agent")


def test_under_v1_the_insert_names_the_36_a80_columns_and_writes_none_of_the_three():
    values, named = insert_shape(sql_for("v1"))
    assert named == A80_ITEM_STATE and values == 36


def test_under_v2_the_insert_names_39_columns_ending_with_the_three():
    values, named = insert_shape(sql_for("v2"))
    assert named == A80_ITEM_STATE + SWITCH_COLUMNS and values == 39
