"""Review N3: an unreadable or missing locality row is carried so the brief can hold it, so eligible is true for it.
The side consumers (watches, seeds, the scorecard) must not act on it. The edits are in the repository SQL;
watches.sql is also run on the harness. """

import re
from pathlib import Path

import pytest

from core.detect import sqlrun
from core.detect.tests import duck

REPO = Path(__file__).resolve().parents[1] / "sql"
NOT_READABLE = "IFNULL(s.locality_status, '') NOT IN ('unreadable', 'missing')"



def edited(name):
    """The statement as the repository has it: the clause of N3 is in the file itself."""
    return (REPO / name).read_text(encoding="utf-8")


def items_statement(text):
    pieces = {re.search(r"^--\s*name:\s*(\w+)", p, re.M).group(1): p for p in sqlrun.split(text) if "name:" in p}
    return sqlrun._strip_leading_comments(pieces["items"].split("\n", 1)[1] if pieces["items"].startswith("--") else pieces["items"])


@pytest.fixture
def con():
    return duck.connect()


def put(con, key, **over):
    row = {"metric_date": __import__("datetime").date(2026, 9, 20), "market": "ZA", "item_id": key, "kind": "hashtag",
           "state": "rising", "main_ratio": 2.0, "creators3": 9, "eligible": True, "authenticity": "clear",
           "geo_status": "local", "sponsored_share": 0.0, "run_id": "r1", "locality_basis": "locality_v2.1",
           "locality_status": "local", "eligible_v1": True}
    row.update(over)
    duck.load(con, "core.item_state", [row])
    duck.load(con, "core.cultural_map", [{"item_id": key, "kind": "hashtag", "canonical_key": key, "label": key, "status": "active"}])


def ids(con, text):
    rows = duck.query(con, text, {"d": __import__("datetime").date(2026, 9, 20), "run_id": "r1"})
    return sorted(r["item_id"] for r in rows)


def test_the_watch_items_statement_skips_unreadable_and_missing_and_still_admits_readable_and_v1_rows(con):
    put(con, "ok")
    put(con, "unread", locality_status="unreadable")
    put(con, "miss", locality_status="missing")
    put(con, "v1row", locality_basis="v1", locality_status=None)
    put(con, "v1bad", locality_basis="v1", locality_status=None, geo_status="not_local")
    put(con, "v2geo", geo_status="not_local")          # v1 would reject it; under v2 authority the v2 row decides
    got = ids(con, items_statement(edited("watches.sql")))
    assert got == ["ok", "v1row", "v2geo"]


@pytest.mark.parametrize("name", ["watches.sql", "seeds.sql", "scorecard.sql"])
def test_every_side_consumer_names_the_unreadable_rule_once(name):
    text = edited(name)
    assert text.count("'unreadable', 'missing'") == 1
