"""Review Q14: how many label-only changes a shared-member threshold of 1, 2 and 3 would let through. The statement is
the measurement of the decision on MIN_SHARED_MEMBERS (typed 2 on 9 Oct 2026); run on the retained clusters of 5 to
8 Oct during the shadow period it returns the three counts per day. Read only."""

from datetime import date
from pathlib import Path

import pytest

from core.detect.tests import duck

SQL = Path(__file__).resolve().parents[1] / "sql"


@pytest.fixture
def con():
    c = duck.connect()
    c.execute("ALTER TABLE core.clusters ADD COLUMN label VARCHAR")
    d1, d2 = date(2026, 10, 6), date(2026, 10, 7)
    for cid, day, item, label in (("c0", d1, "A", "old name"), ("c1", d2, "A", "new name"),
                                   ("c2", d1, "B", "same"), ("c3", d2, "B", "same"),
                                   ("c4", d1, "C", "one"), ("c5", d2, "C", "two"), ("c6", d1, "D", "x"), ("c7", d2, "D", "y"),
                                   ("c8", date(2026, 10, 5), "A", "older name")):    # two days back: not a consecutive pair
        duck.load(c, "core.clusters", [{"cluster_date": day, "cluster_id": cid, "market": "NG", "item_id": item,
                                         "match_kind": "match", "label": label}])
    for cid, post in (("c0", "p1"), ("c0", "p2"), ("c1", "p1"), ("c1", "p2"), ("c1", "p9"),      # A: 2 shared
                      ("c4", "p3"), ("c5", "p3"), ("c5", "p8"),                                    # C: 1 shared
                      ("c6", "p5"), ("c7", "p6"), ("c8", "p1"), ("c8", "p2")):                                                  # D: none shared
        duck.load(c, "core.cluster_members", [{"cluster_id": cid, "post_id": post, "probability": 1.0}])
    return c


def test_the_measurement_counts_label_changes_by_shared_members(con):
    text = (SQL / "label_shared_members.sql").read_text(encoding="utf-8")
    rows = duck.query(con, text, {"start": date(2026, 10, 7), "end": date(2026, 10, 7)})
    assert rows == [{"cluster_date": date(2026, 10, 7), "label_changes": 3, "at_1": 2, "at_2": 1, "at_3": 0}]
    # B kept its label and is not a change at any threshold
