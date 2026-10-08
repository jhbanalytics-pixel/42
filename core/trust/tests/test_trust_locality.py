"""W8-DEC-17: G6 removal is unchanged (8 or more located posts and a local_share under 0.6 is out); the Local label
also needs the lower end of the 95% Wilson interval of local_share to be 0.5 or more, else the card carries Market
unconfirmed. Boundary values are pinned and the rule is scanned against an independent algebraic form."""

import hashlib
import math
from pathlib import Path

import pytest

from core.trust import locality

STATE_SQL = Path(__file__).parents[2] / "detect" / "sql" / "state.sql"
G6_REMOVAL = (
    "    CASE WHEN IFNULL(c.geo_known_posts7, 0) < 8 THEN 'market_unconfirmed'\n"
    "         WHEN c.local_posts7 / c.geo_known_posts7 < .6 THEN 'not_local' ELSE 'local' END geo_status,\n"
)
Z = 1.959963984540054  # the 97.5th percentile of the standard normal, written out here and not read from the code


def algebraic_local(local, known):
    """Lower end of the Wilson interval at 0.5 or more, solved for local without the interval itself:
    (local - known/2)^2 >= z^2 (local (known - local) / known + z^2 / 4), with local above half; and share 0.6."""
    lower_ok = 2 * local > known and (local - known / 2) ** 2 >= Z * Z * (local * (known - local) / known + Z * Z / 4)
    return lower_ok and local / known >= 0.6


@pytest.mark.parametrize("local,known,lower", [
    (5, 8, 0.305742), (6, 8, 0.409275), (7, 8, 0.529112), (8, 8, 0.675592), (4, 8, 0.215216),
    (60, 100, 0.502003), (59, 100, 0.492014), (30, 50, 0.461814), (16, 20, 0.583983), (12, 20, 0.386582),
    (0, 8, 0.0),
])
def test_wilson_lower_end_pinned(local, known, lower):
    assert locality.wilson_lower(local, known) == pytest.approx(lower, abs=1e-6)


def test_five_of_eight_local_has_a_lower_end_near_point_three_and_is_not_confirmed():
    assert locality.wilson_lower(5, 8) == pytest.approx(0.306, abs=0.001)
    assert locality.is_local(5, 8) is False


@pytest.mark.parametrize("local,known,expected", [
    (7, 8, True), (8, 8, True), (6, 8, False), (5, 8, False), (4, 8, False),
    (9, 10, True), (8, 10, False), (60, 100, True), (59, 100, False), (30, 50, False), (32, 50, True),
])
def test_boundaries(local, known, expected):
    assert locality.is_local(local, known) is expected


def test_every_count_up_to_300_matches_the_algebraic_form():
    for known in range(1, 301):
        for local in range(0, known + 1):
            assert locality.is_local(local, known) == algebraic_local(local, known), (local, known)


def test_an_empty_sample_confirms_nothing():
    assert locality.wilson_lower(0, 0) == 0.0
    assert locality.is_local(0, 0) is False


def test_the_share_floor_of_the_removal_rule_stays_part_of_local():
    # 114 of 200 is 0.57: the interval's lower end passes 0.5, the removal share of 0.6 does not.
    assert locality.wilson_lower(114, 200) >= 0.5
    assert locality.is_local(114, 200) is False


def test_the_constants_are_the_decided_ones():
    assert (locality.MIN_LOCAL_LOWER, locality.REMOVE_BELOW, locality.Z95) == (0.5, 0.6, Z)


# The row's own fields: a count the share cannot give back is not confirmed.


def row(status="local", share=0.875, known=8, **extra):
    return {"geo_status": status, "local_share": share, "geo_known_posts7": known, **extra}


def test_local_posts_come_back_from_the_share_and_the_known_count():
    assert locality.local_posts(0.875, 8) == 7
    assert locality.local_posts(5 / 8, 8) == 5
    assert locality.local_posts(0.6, 8) is None  # 4.8 posts
    for bad in ((None, 8), (0.5, None), (float("nan"), 8), (0.5, 0), (0.5, -8), (1.2, 8), (-0.1, 8),
                (True, 8), (0.5, True), ("0.5", 8), (0.5, "8"), (0.5, 8.0)):
        assert locality.local_posts(*bad) is None, bad


@pytest.mark.parametrize("share,known,expected", [
    (7 / 8, 8, "local"), (5 / 8, 8, "market_unconfirmed"), (0.9, 10, "local"), (0.6, 100, "local"),
    (0.6, 8, "market_unconfirmed"), (0.2, 12, "market_unconfirmed"), (float("nan"), 12, "market_unconfirmed"),
    (0.95, "12", "market_unconfirmed"),
])
def test_effective_status_of_a_local_row(share, known, expected):
    assert locality.geo_status(row(share=share, known=known)) == expected


@pytest.mark.parametrize("status", ["not_local", "market_unconfirmed", None, "other"])
def test_a_status_other_than_local_is_never_changed(status):
    assert locality.geo_status(row(status=status, share=5 / 8)) == status


def test_a_row_with_no_counts_at_all_keeps_its_status():
    assert locality.geo_status({"geo_status": "local"}) == "local"
    assert locality.geo_status({"geo_status": "local", "local_share": None, "geo_known_posts7": None}) == "local"


def test_one_missing_count_is_not_confirmed():
    assert locality.geo_status({"geo_status": "local", "local_share": 0.9}) == "market_unconfirmed"
    assert locality.geo_status({"geo_status": "local", "geo_known_posts7": 10}) == "market_unconfirmed"


def test_the_row_is_not_changed():
    r = row(share=5 / 8)
    before = dict(r)
    assert locality.geo_status(r) == "market_unconfirmed"
    assert r == before


# G6's removal code is not touched.


def test_the_removal_code_in_state_sql_is_byte_identical_to_the_base():
    text = STATE_SQL.read_bytes().decode("utf-8")
    assert text.count(G6_REMOVAL) == 1
    assert hashlib.sha256(G6_REMOVAL.encode("utf-8")).hexdigest() == (
        "2f44a2a1876eb8e3a4c554dbf87ef9e552cc970ce6a3488dcdf8c496e62ae7c5")


def test_nothing_in_the_wilson_rule_is_a_nan_or_an_inf():
    for known in (8, 50, 10_000):
        for local in (0, known // 2, known):
            assert math.isfinite(locality.wilson_lower(local, known))
