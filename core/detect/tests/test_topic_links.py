"""Dated, market-aware topic links: the counted set has at most one dated topic item per post and market, legacy rows
are never re-ranked, a link counts only from its start date and until its end date, the scan is bounded, and the
audit reports the counted figure (must be 0), the raw doubles that precedence suppressed and the legacy doubles
that history keeps. Lane 5 places this at core/detect/tests/test_topic_links.py."""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from core.detect import sqlrun
from core.detect.tests import duck

SQL = Path(__file__).resolve().parents[1] / "sql"
UTC = timezone.utc
E = date(2026, 10, 7)                                         # the effective date of the split of B into C


def d(n):
    return date(2026, 10, n)


@pytest.fixture
def con():
    """Items: A is a pooled-run topic, B a market-run topic (A sorts before B on purpose), C the child of B's split."""
    c = duck.connect()
    for post, market in (("p1", "NG"), ("p2", "NG"), ("p3", "NG"), ("p3", "KE"), ("p4", "NG"), ("p5", "NG"), ("p6", "NG"),
                         ("p7", "NG"), ("p8", "NG"), ("p9", "NG")):
        duck.load(c, "core.post_observations", [{"post_id": post, "observed_at": datetime(2026, 10, 1, tzinfo=UTC),
                                                  "observed_date": d(1), "market": market, "lane": "sweep",
                                                  "lane_class": "unbiased_rank"}])
    for item in ("A", "B", "C", "E"):
        duck.load(c, "core.cultural_map", [{"item_id": item, "kind": "topic", "status": "active",
                                            "valid_from": datetime(2026, 9, 1, tzinfo=UTC), "valid_to": None}])
    duck.load(c, "core.cultural_map", [{"item_id": "H", "kind": "hashtag", "status": "active",
                                        "valid_from": datetime(2026, 9, 1, tzinfo=UTC), "valid_to": None}])
    duck.load(c, "core.post_items", [
        {"post_id": "p1", "item_id": "B", "via": "cluster", "linked_on": d(2), "link_market": "NG"},
        {"post_id": "p1", "item_id": "A", "via": "cluster_pan", "linked_on": d(2), "link_market": None},
        {"post_id": "p2", "item_id": "A", "via": "cluster_pan", "linked_on": d(2), "link_market": None},
        {"post_id": "p3", "item_id": "B", "via": "cluster", "linked_on": d(2), "link_market": "NG"},
        {"post_id": "p3", "item_id": "E", "via": "cluster", "linked_on": d(2), "link_market": "KE"},
        {"post_id": "p4", "item_id": "B", "via": "cluster", "linked_on": None, "link_market": None},    # a legacy row
        {"post_id": "p5", "item_id": "H", "via": "hashtag", "linked_on": None, "link_market": None},
        {"post_id": "p6", "item_id": "B", "via": "cluster", "linked_on": d(20), "link_market": "NG"},    # a future link
        {"post_id": "p7", "item_id": "B", "via": "cluster", "linked_on": d(2), "link_market": "NG"},
        # two market-run links of the same market and the same rank: the lower item id wins
        {"post_id": "p9", "item_id": "E", "via": "cluster", "linked_on": d(2), "link_market": "NG"},
        {"post_id": "p9", "item_id": "B", "via": "cluster", "linked_on": d(2), "link_market": "NG"},
        # two legacy rows written before this contract: cluster links with no market and no start date. They cannot
        # be told apart as market-run or pooled-run, so history keeps both (review N8)
        {"post_id": "p8", "item_id": "Zmarket", "via": "cluster", "linked_on": None, "link_market": None},
        {"post_id": "p8", "item_id": "Apooled", "via": "cluster", "linked_on": None, "link_market": None}])
    duck.load(c, "core.post_item_lineage", [{"post_id": "p4", "item_id": "C", "linked_on": E, "link_market": "NG",
                                              "lineage_id": "split-1"}])
    duck.load(c, "core.post_item_end", [
        {"post_id": "p4", "item_id": "B", "ended_on": E, "reason": "split", "lineage_id": "split-1"},
        {"post_id": "p7", "item_id": "B", "ended_on": E, "reason": "false_match_removal", "lineage_id": "fm-1"}])
    return c


def counted(con, day):
    rows = duck.query(con, "SELECT post_id, item_id, market FROM {core}.tvf_post_items(@d)", {"d": day})
    return sorted((r["post_id"], r["item_id"], r["market"]) for r in rows)


def test_the_market_run_link_wins_over_the_pooled_run_link_in_the_same_market(con):
    got = counted(con, d(5))
    assert ("p1", "B", "NG") in got and ("p1", "A", "NG") not in got
    assert ("p2", "A", "NG") in got                                         # a pooled link alone still counts


def test_one_post_can_belong_to_a_different_topic_in_each_market(con):
    got = counted(con, d(5))
    assert ("p3", "B", "NG") in got and ("p3", "E", "KE") in got


def test_a_link_counts_only_from_its_start_date_and_a_future_link_never(con):
    assert not [r for r in counted(con, d(1)) if r[0] in ("p1", "p2", "p3")]
    assert not [r for r in counted(con, d(5)) if r[0] == "p6"]


def test_a_split_moves_the_post_on_the_effective_date_with_no_double_count(con):
    before, on = counted(con, d(6)), counted(con, E)
    assert [r for r in before if r[0] == "p4"] == [("p4", "B", None)]
    assert [r for r in on if r[0] == "p4"] == [("p4", "C", "NG")]


def test_a_false_match_removal_ends_the_link_with_nothing_to_replace_it(con):
    assert [r for r in counted(con, d(6)) if r[0] == "p7"] == [("p7", "B", "NG")]
    assert [r for r in counted(con, E) if r[0] == "p7"] == []


def test_legacy_rows_are_never_ranked_so_history_does_not_move(con):
    """Review N8: the first draft gave this post to Apooled because its id sorts first. Now both rows count, as before."""
    assert [r for r in counted(con, d(5)) if r[0] == "p8"] == [("p8", "Apooled", None), ("p8", "Zmarket", None)]


def test_a_non_topic_link_passes_unchanged(con):
    assert [r for r in counted(con, d(5)) if r[0] == "p5"] == [("p5", "H", None)]


def test_the_observation_scan_is_bounded_to_the_28_day_window():
    text = (SQL / "locality_views.sql").read_text(encoding="utf-8")
    text = text[text.index("TABLE FUNCTION {core}.tvf_post_items"):]      # the counted-link function only
    assert "o.observed_date BETWEEN DATE_SUB(d, INTERVAL 27 DAY) AND d" in text


def test_a_tie_between_two_links_of_the_same_rank_goes_to_the_lower_item_id(con):
    got = [r for r in counted(con, d(5)) if r[0] == "p9"]
    assert got == [("p9", "B", "NG")]
