"""state.sql with the locality edit of C4 v3 section 11.3, run while the detect run is still running (tests L-25, L-27).

state.sql reads this run's rows from v_item_locality_checked, not from the current view, because the detect run is
'running' until chain.finish and the current view joins only ok runs. With @authority = 'v2' the checked status
decides eligible; with 'v1' (the branch default until the switch commit) eligible is the v1 expression, byte for byte.
The INSERT names all 39 columns (review N4)."""

from pathlib import Path

import pytest

from core.detect import sqlrun
from core.detect.tests import duck
from core.detect.tests.duck import run_duck, temp_macro
from core.detect.tests.duck import strip_leading_comments as _strip
from core.detect.tests.fixtures import D, run
from core.detect.tests.test_detect_states import ANY, RULE, RUN, World, fresh
from core.detect.tests.test_state_columns import insert_shape

SQL = Path(__file__).resolve().parents[1] / "sql"
V2 = "locality_v2.1"


@pytest.fixture
def con():
    c = duck.connect()
    for stmt in sqlrun.split(sqlrun.render((SQL / "waves.sql").read_text(encoding="utf-8"), "core", "agent")):
        c.execute(duck.create_statement(_strip(stmt)))
    return c


def world(con):
    w = World()
    for item, known, local in (("d1", 12, 3), ("d3", 10, 8), ("none", 0, 0)):
        fresh(w, item)
        w.daily(item, 0, 5, **ANY, geo_known_posts=known, local_posts=local)      # what v1 detect reads
    w.build(con)
    # the detect run is still running: no ok detect row exists, as during state.sql in job.py
    duck.load(con, "agent.runs", [run("detect", D, run_id=RUN, status="running")])
    return w


def state(con, authority):
    temp, insert = [_strip(s) for s in sqlrun.split(sqlrun.render((SQL / "state.sql").read_text(encoding="utf-8"), "core", "agent"))]
    con.execute(temp_macro(temp))
    run_duck(con, insert, {"d": D, "run_id": RUN, "rule_version": RULE, "authority": authority})
    rows = duck.query(con, "SELECT * FROM {core}.item_state s WHERE s.run_id = @r", {"r": RUN})
    return {r["item_id"]: r for r in rows}


def put(con, item, known, local, status):
    from datetime import datetime, timezone
    t = datetime(2026, 9, 20, 3, tzinfo=timezone.utc)
    foreign = known - local
    duck.load(con, "core.item_locality", [{
        "run_date": D, "market": "ZA", "item_id": item, "detect_run_id": RUN, "population_cutoff": t,
        "metric_version": V2, "schema_version": 1, "computed_at": t, "population_posts": known,
        "known_posts": known, "local_posts": local, "foreign_posts": foreign, "unknown_posts": 0,
        "feed_only_posts": 0, "vetoed_feed_posts": 0, "local_creators": local, "known_creators": known,
        "feed_only_creators": 0, "breadth_creators": local, "status": status,
        "local_share": local / known if known else None, "population_digest": "d" * 64}])
    duck.load(con, "core.item_locality_verified", [{"run_date": D, "market": "ZA", "item_id": item, "detect_run_id": RUN,
        "metric_version": V2, "verified_at": t, "member_rows": known, "population_digest": "d" * 64}])


def test_state_reads_this_runs_locality_row_while_the_detect_run_is_running(con):
    world(con)
    put(con, "d1", 3, 3, "market_unconfirmed")        # v1 detect said not_local (12 known, 3 local); v2 says unconfirmed
    put(con, "d3", 8, 0, "not_local")                 # v1 detect said local (10 known, 8 local); v2 says not_local
    put(con, "d1", 9, 0, "not_local")                 # a row of the same item from another detect run: ignored
    con.execute("UPDATE core.item_locality SET detect_run_id = 'detect-previous' WHERE known_posts = 9")
    con.execute("UPDATE core.item_locality_verified SET detect_run_id = 'detect-previous' WHERE member_rows = 9")
    # "none" has no locality row at all
    v1 = state(con, "v1")
    assert {k: (v["eligible"], v["geo_status"], v["locality_basis"], v["locality_status"]) for k, v in v1.items()
            if k in ("d1", "d3", "none")} == {
        "d1": (False, "not_local", "v1", None), "d3": (True, "local", "v1", None), "none": (True, "market_unconfirmed", "v1", None)}
    con.execute("DELETE FROM core.item_state")
    v2 = state(con, "v2")
    assert len(duck.query(con, "SELECT 1 x FROM {core}.item_state WHERE item_id = 'd1'", {})) == 1
    assert {k: (v["eligible"], v["eligible_v1"], v["locality_basis"], v["locality_status"]) for k, v in v2.items()
            if k in ("d1", "d3", "none")} == {
        "d1": (True, False, V2, "market_unconfirmed"),     # v1 rejects, v2 admits
        "d3": (False, True, V2, "not_local"),              # v1 admits, v2 rejects
        "none": (True, True, V2, "unreadable")}            # no row: carried, not dropped, not not_local


def test_in_v1_mode_eligible_is_the_v1_expression_even_when_a_v2_row_disagrees(con):
    world(con)
    put(con, "d3", 8, 0, "not_local")
    put(con, "d1", 3, 3, "market_unconfirmed")
    v1 = state(con, "v1")
    assert (v1["d3"]["eligible"], v1["d1"]["eligible"]) == (True, False) and v1["d3"]["eligible_v1"] is True


def test_a_row_without_a_verification_row_reads_as_missing_in_v2_mode(con):
    world(con)
    put(con, "d3", 8, 0, "not_local")
    con.execute("DELETE FROM core.item_locality_verified")
    v2 = state(con, "v2")["d3"]
    assert (v2["eligible"], v2["locality_status"]) == (True, "unreadable")


def test_the_v1_columns_keep_their_names_and_values_when_v2_is_authoritative(con):
    world(con)
    put(con, "d1", 3, 3, "market_unconfirmed")
    v2 = state(con, "v2")["d1"]
    assert (v2["geo_status"], v2["geo_known_posts7"], v2["local_share"]) == ("not_local", 12, 0.25)


def test_the_insert_names_its_columns_so_the_widened_table_does_not_break_the_positional_insert(con):
    """Review N4: at a80be1d the INSERT has no column list and the SELECT supplies 36 values. The table is 39 columns
    wide now; the insert names all of them and supplies one value for each."""
    table = [r[0] for r in con.execute("DESCRIBE core.item_state").fetchall()]
    assert len(table) == 39 and table[-3:] == ["eligible_v1", "locality_basis", "locality_status"]
    values, named = insert_shape((SQL / "state.sql").read_text(encoding="utf-8"))
    assert values == len(table) and named == table
