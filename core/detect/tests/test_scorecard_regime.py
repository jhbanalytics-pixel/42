"""C4 v3 section 11.4: the weekly scorecard carries a regime marker for the rule that wrote item_state.eligible, so a
week that straddles the switch is marked and no week before and after it is compared as one series.

The marker is read from the item_state rows of the week and the week before (locality_basis, NULL for rows written
before the column existed, which were written under v1), not from a release date held in code. It rides inside every
Figure, so the stored row keeps its columns; the rule version of the scorecard moves because the Figure shape did."""

from datetime import date, timedelta

import pytest

from core.conftest import set_locality_authority
from core.detect import scorecard
from core.detect.tests import duck
from core.detect.tests.fixtures import rid, run
from core.detect.tests.test_detect_scorecard import EXTRA_TABLES, FIGURES, RUN_ID, WEEK, d, state, world  # noqa: F401

V2 = "locality_v2.1"


@pytest.fixture(autouse=True)
def rows_are_read_under_the_v2_authority(monkeypatch):
    """The marker reads item_state.locality_basis, a column the table has once the switch release has applied it; under
    the v1 authority the statement reads NULL, which is v1 (sqlrun.for_authority)."""
    set_locality_authority(monkeypatch, "v2")


@pytest.fixture
def con():
    c = duck.connect()
    days = [WEEK - timedelta(days=14) + timedelta(days=i) for i in range(21)]
    duck.load(c, "agent.runs", [run("detect", x) for x in days])
    return c


def basis_rows(con, spec, market="ZA"):
    """spec: {date: basis}. One eligible row per day under the good detect run of that day."""
    duck.load(con, "core.item_state", [
        state("A", when, "rising", market=market, locality_basis=basis, run_id=rid("detect", when))
        for when, basis in spec.items()])


def regime(con, market="ZA", window=None):
    """The marker over a window (since, until), the week by default, as run_scorecard asks for it."""
    sql = scorecard.queries([{"id": "x", "market": "ZA", "title": "t", "event_date": date(2026, 1, 1),
                              "match_terms": ["termterm"], "added": "a", "source": "s"}])["locality_regime"]
    since, until = window or (WEEK, WEEK + timedelta(days=6))
    rows = duck.query(con, sql, {**scorecard.params(market, WEEK), "since": since, "until": until})
    return scorecard.locality_regime(rows)


def week(basis_by_offset):
    return {WEEK + timedelta(days=k): b for k, b in basis_by_offset.items()}


def test_a_week_and_the_week_before_under_one_rule_are_comparable(con):
    basis_rows(con, week({-7: "v1", -3: None, 0: "v1", 4: "v1"}))
    got = regime(con)
    assert (got["locality_basis"], got["previous_week_basis"], got["comparable_with_previous_week"]) == ("v1", "v1", True)


def test_a_week_that_straddles_the_switch_is_marked_mixed_and_not_comparable(con):
    basis_rows(con, week({-7: "v1", 0: "v1", 2: "v1", 3: V2, 6: V2}))
    got = regime(con)
    assert (got["locality_basis"], got["comparable_with_previous_week"]) == ("mixed", False)
    assert got["days_by_basis"] == {"v1": 2, V2: 2}


def test_the_first_whole_week_after_the_switch_is_not_comparable_with_the_week_before_it(con):
    basis_rows(con, week({-7: "v1", -2: "v1", 0: V2, 5: V2}))
    got = regime(con)
    assert (got["locality_basis"], got["previous_week_basis"], got["comparable_with_previous_week"]) == (V2, "v1", False)


def test_two_whole_weeks_under_the_new_rule_are_comparable(con):
    basis_rows(con, week({-7: V2, -1: V2, 0: V2, 6: V2}))
    got = regime(con)
    assert (got["locality_basis"], got["comparable_with_previous_week"]) == (V2, True)


def test_a_week_with_no_state_rows_has_no_basis_and_is_not_comparable(con):
    basis_rows(con, week({-7: "v1"}))
    got = regime(con)
    assert (got["locality_basis"], got["comparable_with_previous_week"]) == (None, False)


def test_another_market_and_a_failed_detect_run_do_not_count(con):
    basis_rows(con, week({-7: "v1", 0: "v1"}))
    basis_rows(con, week({1: V2}), market="NG")
    duck.load(con, "core.item_state", [state("B", WEEK + timedelta(days=2), "rising", locality_basis=V2, run_id="detect-bad")])
    assert regime(con)["locality_basis"] == "v1"


def test_every_figure_of_every_row_carries_the_marker_and_the_scorecard_rule_version_moved(world):  # noqa: F811
    for row in world["rows"].values():
        assert row["rule_version"] == "scorecard-2"
        for name in FIGURES:
            marker = row[name]["regime"]
            assert marker["comparable_with_previous_week"] in (True, False)
            assert marker["locality_basis"] == (None if row["market"] == "KE" else "v1")   # KE has no state rows
            assert marker["query_id"].startswith("q_locality_regime_") and marker["result_hash"].startswith("sha256:")
            assert marker["run_id"] == RUN_ID


# Each Figure is marked over the window it reads (11.4)

def test_each_figure_has_its_own_window():
    start, end = WEEK, WEEK + timedelta(days=6)
    assert scorecard.figure_windows(WEEK) == {
        "time_to_detect": (scorecard.DATA_START, end),
        "lead_time": (start - timedelta(days=scorecard.LEAD_WINDOW), end),
        "recall": (start, end + timedelta(days=scorecard.RECALL_DAYS)),
        "precision": (start, end), "breadth_platforms": (start, end),
        "expansion_cluster_share": (start, end), "expansion_platform_share": (start, end),
        "expansion_language_share": (start, end), "cost_per_confirmed": (start, end)}


def v1_then_v2(con):
    """v1 days at W-20 and W-15, the new rule from W-7 on: the review's probe."""
    days = [WEEK - timedelta(days=20), WEEK - timedelta(days=15)]
    duck.load(con, "agent.runs", [run("detect", x) for x in days])
    basis_rows(con, {**{x: "v1" for x in days}, **{WEEK + timedelta(days=k): V2 for k in range(-7, 7)}})


def test_a_long_window_that_holds_both_rules_is_mixed_while_the_week_alone_is_comparable(con):
    v1_then_v2(con)
    week_only = regime(con)
    assert (week_only["locality_basis"], week_only["comparable_with_previous_week"]) == (V2, True)
    lead = regime(con, window=scorecard.figure_windows(WEEK)["lead_time"])
    assert (lead["locality_basis"], lead["comparable_with_previous_week"]) == ("mixed", False)
    ttd = regime(con, window=scorecard.figure_windows(WEEK)["time_to_detect"])
    assert (ttd["locality_basis"], ttd["comparable_with_previous_week"]) == ("mixed", False)


def test_the_window_before_is_the_same_window_a_week_earlier(con):
    basis_rows(con, week({-9: "v1", -3: "v1", 0: V2}))
    got = regime(con, window=(WEEK - timedelta(days=2), WEEK + timedelta(days=3)))
    assert got["locality_basis"] == V2 and got["previous_week_basis"] == "v1" and got["comparable_with_previous_week"] is False


def test_the_run_marks_every_figure_over_its_own_window(con):
    v1_then_v2(con)
    con.execute(EXTRA_TABLES)
    rows = scorecard.run_scorecard(duck.Client(con), WEEK, run_id=RUN_ID, core="core", agent="agent")
    za = {r["market"]: r for r in rows}["ZA"]
    windows = scorecard.figure_windows(WEEK)
    for name in FIGURES:
        marker = za[name]["regime"]
        assert (marker["window"]["since"], marker["window"]["until"]) == tuple(d.isoformat() for d in windows[name]), name
    assert za["precision"]["regime"]["comparable_with_previous_week"] is True
    assert za["recall"]["regime"]["comparable_with_previous_week"] is True
    for name in ("time_to_detect", "lead_time"):
        assert za[name]["regime"]["locality_basis"] == "mixed" and za[name]["regime"]["comparable_with_previous_week"] is False


# The side consumers skip a row whose locality row cannot be read (review N3)

def _ttd_rows(statuses):
    """time_to_detect rows for one item that reached Mainstream in the week, its Emerging and Rising rows (the rows that
    flag it, which the scorecard reads through its skip of unreadable rows) carrying each status."""
    from core.detect.tests.fixtures import item_daily
    from core.detect.tests.test_detect_scorecard import REFERENCE as _R  # noqa: F401

    out = {}
    for status in statuses:
        c = duck.connect()
        c.execute(EXTRA_TABLES)
        days = [d(9, 20) + timedelta(days=i) for i in range(30)]
        duck.load(c, "agent.runs", [run(stage, x) for x in days for stage in ("collect", "aggregate", "stats", "detect")])
        duck.load(c, "core.item_daily", [item_daily("A", d(9, 28), 4, lane_class="unbiased_rank", platform="tiktok",
                                                    series=None, protocol=None)])
        duck.load(c, "core.item_state", [state("A", d(9, 30), "emerging", locality_status=status),
                                         state("A", d(10, 1), "rising", locality_status=status),
                                         state("A", d(10, 6), "mainstream", found=3)])
        sql = scorecard.queries([{"id": "x", "market": "ZA", "title": "t", "event_date": date(2026, 1, 1),
                                  "match_terms": ["termterm"], "added": "a", "source": "s"}])["time_to_detect"]
        out[status] = duck.query(c, sql, scorecard.params("ZA", WEEK))
    return out


def test_the_scorecard_does_not_flag_an_item_by_a_row_whose_locality_row_is_unreadable_or_missing():
    got = _ttd_rows([None, "local", "not_local", "unreadable", "missing"])
    for status in (None, "local", "not_local"):
        assert [r["days"] for r in got[status]] == [2], status
    assert [r["days"] for r in got["unreadable"]] == [None] and [r["days"] for r in got["missing"]] == [None]


def test_two_mixed_weeks_are_not_comparable():
    rows = [{"this_week": True, "locality_basis": "v1", "days": 3}, {"this_week": True, "locality_basis": V2, "days": 4},
            {"this_week": False, "locality_basis": "v1", "days": 2}, {"this_week": False, "locality_basis": V2, "days": 5}]
    got = scorecard.locality_regime(rows)
    assert (got["locality_basis"], got["previous_week_basis"], got["comparable_with_previous_week"]) == (
        "mixed", "mixed", False)


def test_two_empty_weeks_are_not_comparable():
    got = scorecard.locality_regime([])
    assert (got["locality_basis"], got["previous_week_basis"], got["comparable_with_previous_week"]) == (None, None, False)
