"""The W8-DEC-17 label (C4 v3 ruling finding F2) and constants parity (test L-12).

W8-DEC-17: removal stays at 8 known posts and a local share under 0.6. The label Local additionally needs the lower
end of the 95% Wilson interval of local_share to be 0.5 or more, else the card carries Market unconfirmed. So `local`
is an admission status and not a label: the label is a second, derived result of the same counts."""

import re
from pathlib import Path

import pytest

from core.trust import locality as loc

SQL = Path(__file__).resolve().parents[2] / "detect" / "sql"


@pytest.mark.parametrize(("known", "local", "lower"), [
    (8, 8, 0.676), (8, 7, 0.529), (8, 6, 0.409), (8, 5, 0.306), (10, 6, 0.313), (15, 9, 0.357), (20, 12, 0.387),
    (30, 18, 0.423), (50, 30, 0.462), (100, 60, 0.502)])
def test_the_wilson_lower_bound_matches_the_rulings_computation(known, local, lower):
    assert round(loc.wilson_lower(known, local), 3) == lower


@pytest.mark.parametrize(("status", "known", "local", "want"), [
    ("local", 8, 8, "local"), ("local", 8, 7, "local"),
    ("local", 8, 6, "market_unconfirmed"),                    # 0.75 of 8 passes removal and fails the label
    ("local", 8, 5, "market_unconfirmed"),
    ("local", 10, 9, "local"), ("local", 10, 8, "market_unconfirmed"),
    ("local", 15, 12, "local"), ("local", 15, 11, "market_unconfirmed"),
    ("local", 20, 15, "local"), ("local", 20, 14, "market_unconfirmed"),
    ("local", 100, 60, "local"),                              # exactly 0.6 reaches the label only at 100 known
    ("local", 95, 57, "market_unconfirmed"),
    ("market_unconfirmed", 7, 7, "market_unconfirmed"),       # under the floor, whatever the share
    ("market_unconfirmed", 0, 0, "market_unconfirmed"),
    ("not_local", 8, 4, "not_local"),                         # removed by G6, never labelled Local
    ("not_local", 200, 119, "not_local"),                     # a Wilson bound above 0.5 cannot rescue a share under 0.6
])
def test_label_truth_table(status, known, local, want):
    assert loc.label(status, known, local) == want


def test_unreadable_has_no_label():
    assert loc.label("unreadable", None, None) is None


def test_minimum_local_posts_for_the_label_at_the_rulings_sizes():
    def need(known):
        return min(n for n in range(known + 1) if loc.label(loc.status_of(known, n), known, n) == "local")
    assert [need(k) for k in (8, 10, 15, 20)] == [7, 9, 12, 15]


def test_a_share_of_exactly_point_six_reaches_the_label_only_at_100_known_or_more():
    reach = [k for k in range(5, 400, 5) if loc.label("local", k, k * 3 // 5) == "local"]
    assert min(reach) == 100


def test_all_local_reaches_the_label_from_four_known_which_the_floor_of_eight_already_exceeds():
    assert min(k for k in range(1, 20) if loc.wilson_lower(k, k) >= loc.LABEL_FLOOR) == 4


def test_the_label_constants_are_pinned_and_the_default_authority_is_the_branch_default():
    assert (loc.WILSON_Z, loc.LABEL_FLOOR, loc.BLOCK_VERSION, loc.MIN_KNOWN) == (1.96, 0.5, 1, 8)
    assert loc.MIN_SHARED_MEMBERS == 2                        # Q14, typed 9 Oct 2026


# L-12: the literals in the SQL equal the module's constants.

def _text(name):
    return (SQL / name).read_text(encoding="utf-8")


def _strings(fragment):
    return tuple(re.findall(r"'([^']+)'", fragment))


def test_members_sql_literals_equal_the_module():
    text = _text("locality_members.sql")
    assert float(re.search(r"geo_confidence, 0\) >= ([\d.]+)", text).group(1)) == loc.MIN_CONFIDENCE
    assert set(_strings(re.search(r"geo_source IN \(([^)]*)\)", text).group(1))) == set(loc.VALID_SOURCES)
    assert set(_strings(re.search(r"lane_class IN \(([^)]*)\)", text).group(1))) == set(loc.LANE_CLASSES)
    assert re.search(r"route, ''\) != '([^']+)'", text).group(1) == loc.FEED_EXCLUDED_ROUTE
    assert int(re.search(r"INTERVAL (\d+) DAY\) AND @d\s*\n\s*AND po\.observed_at", text).group(1)) == loc.WINDOW_DAYS - 1
    zones = dict(re.findall(r"'([A-Z]{2})'[^'\n]*'([A-Za-z]+/[A-Za-z]+)'", re.search(r"WITH zones AS \((.*?)\),\s*seen", text, re.S).group(1)))
    assert zones == loc.ZONES


def test_summary_sql_literals_equal_the_module():
    text = _text("locality_summary.sql")
    assert int(re.search(r"known_posts, 0\) < (\d+)", text).group(1)) == loc.MIN_KNOWN
    den, num = re.search(r"WHEN (\d+) \* a\.local_posts >= (\d+) \* a\.known_posts", text).groups()
    assert (int(num), int(den)) == (loc.SHARE_NUM, loc.SHARE_DEN)


def test_views_sql_literals_equal_the_module():
    text = _text("locality_views.sql")
    assert int(re.search(r"l\.known_posts < (\d+)", text).group(1)) == loc.MIN_KNOWN
    den, num = re.search(r"WHEN (\d+) \* l\.local_posts >= (\d+) \* l\.known_posts", text).groups()
    assert (int(num), int(den)) == (loc.SHARE_NUM, loc.SHARE_DEN)
    assert f"metric_version = '{loc.METRIC_VERSION}'" in text
    assert str(loc.WILSON_Z) in text and f">= {loc.LABEL_FLOOR}" in text
