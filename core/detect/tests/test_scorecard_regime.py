"""C4 v3 section 11.4: the weekly scorecard carries a regime marker for the rule that wrote item_state.eligible, so a
week that straddles the switch is marked and no week before and after it is compared as one series.

The marker is read from the item_state rows of the week and the week before (locality_basis, NULL for rows written
before the column existed, which were written under v1), not from a release date held in code. It rides inside every
Figure, so the stored row keeps its columns; the rule version of the scorecard moves because the Figure shape did."""

from datetime import date, timedelta

import pytest

from core.detect import scorecard
from core.detect.tests import duck
from core.detect.tests.fixtures import rid, run
from core.detect.tests.test_detect_scorecard import FIGURES, RUN_ID, WEEK, d, state, world  # noqa: F401

V2 = "locality_v2.1"


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


def regime(con, market="ZA"):
    sql = scorecard.queries([{"id": "x", "market": "ZA", "title": "t", "event_date": date(2026, 1, 1),
                              "match_terms": ["termterm"], "added": "a", "source": "s"}])["locality_regime"]
    rows = duck.query(con, sql, scorecard.params(market, WEEK))
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
