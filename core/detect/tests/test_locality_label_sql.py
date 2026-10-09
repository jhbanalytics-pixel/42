"""The SQL side of the W8-DEC-17 label: v_item_locality_checked.checked_label must give the module's label on every
(known, local) pair the table can hold, so the two readers stay one rule (ruling finding F2)."""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from core.detect import sqlrun
from core.detect.tests import duck
from core.trust.locality import label, read_locality

SQL = Path(__file__).resolve().parents[1] / "sql"
D = date(2026, 10, 7)
T = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
N = 160                                                       # every pair with known up to 159, about 12,800 rows


@pytest.fixture
def con():
    c = duck.connect()
    for stmt in sqlrun.split(sqlrun.render((SQL / "locality_views.sql").read_text(encoding="utf-8"), "core", "agent")):
        c.execute(duck.create_statement(sqlrun._strip_leading_comments(stmt)))
    c.execute("""
        INSERT INTO core.item_locality
        SELECT DATE '2026-10-07', 'NG', 'k' || k || 'l' || l, 'detect-1', TIMESTAMPTZ '2026-10-07 12:00:00+00',
               'locality_v2.1', 1, TIMESTAMPTZ '2026-10-07 12:00:00+00', k, k, l, k - l, 0, 0, 0, l, k, 0, l,
               CASE WHEN k < 8 THEN 'market_unconfirmed' WHEN 5 * l >= 3 * k THEN 'local' ELSE 'not_local' END,
               CASE WHEN k = 0 THEN NULL ELSE l / k END, repeat('d', 64)
        FROM range(0, ?) t(k), range(0, ?) u(l) WHERE l <= k""", [N, N])
    c.execute("""
        INSERT INTO core.item_locality_verified
        SELECT run_date, market, item_id, detect_run_id, metric_version, TIMESTAMPTZ '2026-10-07 12:00:00+00',
               population_posts, population_digest FROM core.item_locality""")
    return c


def test_checked_label_equals_the_module_label_on_every_storable_pair(con):
    rows = duck.query(con, "SELECT known_posts, local_posts, status, checked_status, checked_label "
                           "FROM {core}.v_item_locality_checked", {})
    assert len(rows) == N * (N + 1) // 2
    wrong = [(r["known_posts"], r["local_posts"], r["checked_label"], label(r["checked_status"], r["known_posts"], r["local_posts"]))
             for r in rows if r["checked_label"] != label(r["checked_status"], r["known_posts"], r["local_posts"])]
    assert wrong == []
    assert {r["checked_label"] for r in rows} == {"local", "market_unconfirmed", "not_local"}


def test_an_unreadable_row_has_a_null_label_in_sql_as_in_python(con):
    con.execute("UPDATE core.item_locality SET status = 'local' WHERE item_id = 'k8l0'")
    row = duck.query(con, "SELECT checked_status, checked_label FROM {core}.v_item_locality_checked WHERE item_id = 'k8l0'", {})[0]
    assert row == {"checked_status": "unreadable", "checked_label": None}
    assert read_locality({"metric_version": "locality_v2.1", "population_posts": 8, "known_posts": 8, "local_posts": 0,
                          "foreign_posts": 8, "unknown_posts": 0, "status": "local", "local_share": 0.0}).status == "unreadable"
