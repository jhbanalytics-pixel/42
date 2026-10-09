"""The SQL side of the reader: v_item_locality_checked must agree with core/trust/locality.py read_locality on every
row the table can hold, must show only keys that passed write-time verification, and must work while the detect run
is still running. Lane 5 places this at core/detect/tests/test_locality_views.py."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.detect import sqlrun
from core.detect.tests import duck
from core.trust.locality import digest, read_locality, verify

SQL = Path(__file__).resolve().parents[1] / "sql"
UTC = timezone.utc
D = date(2026, 10, 7)
T = datetime(2026, 10, 7, 12, tzinfo=UTC)


def row(item, known, local, unknown=0, **over):
    foreign = known - local
    base = {"run_date": D, "market": "NG", "item_id": item, "detect_run_id": "detect-1", "population_cutoff": T,
            "metric_version": "locality_v2.1", "schema_version": 1, "computed_at": T,
            "population_posts": known + unknown, "known_posts": known, "local_posts": local,
            "foreign_posts": foreign, "unknown_posts": unknown, "feed_only_posts": 0, "vetoed_feed_posts": 0,
            "local_creators": local, "known_creators": known, "feed_only_creators": 0, "breadth_creators": local,
            "status": "market_unconfirmed" if known < 8 else ("local" if 5 * local >= 3 * known else "not_local"),
            "local_share": local / known if known else None, "population_digest": "d" * 64}
    base.update(over)
    return base


def load(con, rows, verified=True):
    duck.load(con, "core.item_locality", rows)
    if verified:
        mark(con, rows)


def mark(con, rows):
    if True:
        for r in rows:
            duck.load(con, "core.item_locality_verified", [{
                **{k: r[k] for k in ("run_date", "market", "item_id", "detect_run_id", "metric_version")},
                "verified_at": T, "member_rows": r["population_posts"], "population_digest": r["population_digest"]}])


CASES = [
    row("c01", 0, 0), row("c02", 0, 0, unknown=12), row("c03", 7, 7), row("c04", 7, 0), row("c05", 8, 4),
    row("c06", 8, 5), row("c07", 10, 6), row("c08", 15, 9), row("c09", 10, 5), row("c10", 9, 5), row("c11", 8, 0),
    row("x01", 8, 5, foreign_posts=4),                              # counts that do not add up
    row("x02", 8, 5, unknown_posts=3),
    row("x03", 8, 4, status="local"),                               # the row's own claim disagrees
    row("x04", 8, 5, status="not_local"),
    row("x05", 8, 5, local_share=0.1),                              # share disagrees
    row("x06", 0, 0, local_share=0.0),
    row("x07", 8, 5, local_share=None),
    row("x08", 8, 5, local_posts=-1, foreign_posts=9, status="not_local", local_share=-0.125),   # negative only
]


@pytest.fixture
def con():
    c = duck.connect()
    for stmt in sqlrun.split(sqlrun.render((SQL / "locality_views.sql").read_text(encoding="utf-8"), "core", "agent")):
        c.execute(duck.create_statement(sqlrun._strip_leading_comments(stmt)))
    duck.load(c, "agent.runs", [{"run_id": "detect-1", "stage": "detect", "run_date": D, "status": "ok",
                                  "started_at": T, "finished_at": T + timedelta(minutes=20)}])
    return c


def test_sql_and_python_readers_agree_on_every_storable_row(con):
    load(con, CASES)
    got = {r["item_id"]: r["checked_status"] for r in duck.query(con, "SELECT * FROM {core}.v_item_locality_checked", {})}
    for case in CASES:
        assert got[case["item_id"]] == read_locality(case).status, case["item_id"]


def test_a_row_of_another_metric_version_is_invisible_to_every_consumer_join(con):
    load(con, [row("a", 8, 5), row("a", 8, 5, metric_version="locality_v2.2")])
    rows = duck.query(con, "SELECT * FROM {core}.v_item_locality_current WHERE item_id = 'a'", {})
    assert len(rows) == 1 and rows[0]["metric_version"] == "locality_v2.1"


def test_the_current_view_is_empty_while_the_detect_run_is_running_and_the_checked_view_is_not(con):
    con.execute("UPDATE agent.runs SET status = 'running', finished_at = NULL WHERE run_id = 'detect-1'")
    load(con, [row("a", 8, 5)])
    assert duck.query(con, "SELECT * FROM {core}.v_item_locality_current", {}) == []
    mine = duck.query(con, "SELECT * FROM {core}.v_item_locality_checked WHERE detect_run_id = @r", {"r": "detect-1"})
    assert [r["checked_status"] for r in mine] == ["local"]


def test_only_the_good_detect_run_of_the_date_is_current(con):
    duck.load(con, "agent.runs", [{"run_id": "detect-0", "stage": "detect", "run_date": D, "status": "failed",
                                    "started_at": T - timedelta(hours=3), "finished_at": T - timedelta(hours=2)}])
    load(con, [row("a", 8, 5), row("a", 8, 0, detect_run_id="detect-0", status="not_local")])
    rows = duck.query(con, "SELECT detect_run_id, checked_status FROM {core}.v_item_locality_current", {})
    assert rows == [{"detect_run_id": "detect-1", "checked_status": "local"}]


def test_a_key_without_a_verification_row_is_invisible_to_both_views(con):
    load(con, [row("good", 8, 5)])
    load(con, [row("unverified", 8, 5)], verified=False)
    for view in ("v_item_locality_checked", "v_item_locality_current"):
        ids = [r["item_id"] for r in duck.query(con, "SELECT item_id FROM {core}." + view, {})]
        assert ids == ["good"], view


def test_the_summary_that_agrees_with_itself_but_not_with_its_members_gets_no_verification_row(con):
    members = [{"post_id": f"p{n}", "locality_class": c, "creator_key": f"t:{n}", "feed_sighted": False}
               for n, c in enumerate(["local"] * 5 + ["foreign"] * 3)]
    honest = row("honest", 8, 5, population_digest=digest(members))
    forged = row("forged", 8, 6, population_digest=digest(members))        # consistent counts, one member too many local
    keys = [r for r in (honest, forged) if verify(r, members)]
    load(con, [honest, forged], verified=False)
    mark(con, keys)
    assert read_locality(forged).status == "local"                          # the reader alone would have accepted it
    ids = [r["item_id"] for r in duck.query(con, "SELECT item_id FROM {core}.v_item_locality_current", {})]
    assert ids == ["honest"]


def test_a_duplicated_verification_row_does_not_duplicate_the_locality_row(con):
    r = row("a", 8, 5)
    load(con, [r])
    mark(con, [r])
    assert len(duck.query(con, "SELECT * FROM {core}.v_item_locality_checked", {})) == 1
