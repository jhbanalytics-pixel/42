"""W8-DEC-11 on the read side: v_collection_health_current reads the day before a zero_yield row as zero_yield
too, and G1 (core/trust/gate.py) holds a card on the rows it now sees. G1's rule and window are pinned unchanged."""

import hashlib
from pathlib import Path

import pytest

from core.brief.tests.test_brief_gatectx import ctx, item, series_platform, world
from core.detect.tests import duck
from core.detect.tests.fixtures import D, day, health, rid, run
from core.trust.gate import gate_card

ROOT = Path(__file__).resolve().parents[2]


def zero(series, d, **kw):
    return dict(health(series, d, **kw), items=0, valid=False, invalid_reason="zero_yield")


def first_zero(series, d, **kw):
    """The first day of a zero run as the writer stores it: every call answered, nothing landed, still valid."""
    return dict(health(series, d, **kw), items=0)


def view(con):
    rows = duck.query(con, "SELECT v.day, v.market, v.series, v.valid, v.invalid_reason, v.run_id "
                           "FROM {core}.v_collection_health_current v ORDER BY v.day, v.market, v.series")
    return {(r["day"], r["market"], r["series"]): (r["valid"], r["invalid_reason"]) for r in rows}


def loaded(rows, platform=None):
    con = world()
    if platform:
        series_platform(con, platform, SERIES)
    duck.load(con, "core.collection_health", rows)
    return con


def test_the_day_before_a_zero_yield_day_reads_zero_yield_too():
    got = view(loaded([first_zero("panel_culture_desk", day(1), lane_class="panel", platform=None),
                       zero("panel_culture_desk", D, lane_class="panel", platform=None)]))
    assert got[(day(1), "ZA", "panel_culture_desk")] == (False, "zero_yield")
    assert got[(D, "ZA", "panel_culture_desk")] == (False, "zero_yield")


def test_a_chain_of_zero_days_reads_zero_yield_from_the_first_day():
    got = view(loaded([first_zero("p", day(2)), zero("p", day(1)), zero("p", D)]))
    assert [got[(d, "ZA", "p")] for d in (day(2), day(1), D)] == [(False, "zero_yield")] * 3


def test_the_day_before_keeps_its_own_reason_when_it_was_already_invalid():
    got = view(loaded([dict(health("p", day(1)), valid=False, invalid_reason="items"), zero("p", D)]))
    assert got[(day(1), "ZA", "p")] == (False, "items")


def test_other_markets_series_and_days_are_not_marked():
    got = view(loaded([
        first_zero("p", day(1)), first_zero("p", day(1), market="NG"), first_zero("q", day(1)),
        first_zero("p", day(3)), zero("p", D)]))
    assert got[(day(1), "ZA", "p")] == (False, "zero_yield")
    assert got[(day(1), "NG", "p")] == (True, None)
    assert got[(day(1), "ZA", "q")] == (True, None)
    assert got[(day(3), "ZA", "p")] == (True, None)


def test_only_the_protocol_that_landed_nothing_is_marked():
    con = loaded([first_zero("p", day(1), protocol="p1"), health("p", day(1), protocol="p2"),
                  zero("p", D, protocol="p1")])
    rows = duck.query(con, "SELECT v.protocol, v.valid FROM {core}.v_collection_health_current v "
                           "WHERE v.day = @d ORDER BY v.protocol", {"d": day(1)})
    assert rows == [{"protocol": "p1", "valid": False}, {"protocol": "p2", "valid": True}]


def rot(protocol, d, *, route="prism/profiles", series="panel_culture_desk", items=50, **kw):
    row = health(series, d, protocol=protocol, lane_class="panel", platform=None, **kw)
    return dict(row, route=route, items=items)


def rotating_protocols():
    """Three days of a curated panel whose protocol changes every day, beside a desk panel that does not."""
    rows = []
    for n, d in enumerate((day(2), day(1), D)):
        rows.append(dict(rot("panel:desk", d), items=4))
        rows.append(dict(rot(f"panel:rot{n}", d), items=0))
    rows[-1] = dict(rows[-1], valid=False, invalid_reason="zero_yield")
    rows[-3] = dict(rows[-3], valid=False, invalid_reason="zero_yield")
    return rows


def by_protocol(con):
    rows = duck.query(con, "SELECT v.day, v.protocol, v.valid, v.invalid_reason "
                           "FROM {core}.v_collection_health_current v")
    return {(r["day"], r["protocol"]): (r["valid"], r["invalid_reason"]) for r in rows}


def test_three_rotating_curated_protocols_read_zero_yield_from_the_first_day():
    got = by_protocol(loaded(rotating_protocols()))
    assert [got[(d, f"panel:rot{n}")] for n, d in enumerate((day(2), day(1), D))] == [(False, "zero_yield")] * 3


def test_a_live_desk_row_on_the_shared_route_is_not_flipped_by_a_dead_curated_row():
    got = by_protocol(loaded(rotating_protocols()))
    assert [got[(d, "panel:desk")] for d in (day(2), day(1), D)] == [(True, None)] * 3


def test_the_v2_switch_day_flips_the_day_before_whatever_the_protocol():
    got = by_protocol(loaded([rot("panel:abc", day(1), items=0), dict(rot("panel:abc:v2", D, items=0),
                                                                    valid=False, invalid_reason="zero_yield")]))
    assert got[(day(1), "panel:abc")] == (False, "zero_yield")


def test_a_day_that_was_not_a_zero_day_is_not_flipped_by_a_zero_yield_row_of_its_route():
    partly = dict(rot("panel:partly", day(1), items=0), calls_ok=1)
    nocalls = dict(rot("panel:nocalls", day(1), items=0), calls=0, calls_ok=0)
    got = by_protocol(loaded([partly, nocalls, dict(rot("panel:today", D, items=0),
                                                    valid=False, invalid_reason="zero_yield")]))
    assert got[(day(1), "panel:partly")] == (True, None)
    assert got[(day(1), "panel:nocalls")] == (True, None)


def test_a_zero_yield_row_of_another_route_lane_or_market_flips_nothing():
    dead = dict(rot("panel:today", D, items=0), valid=False, invalid_reason="zero_yield")
    others = [rot("panel:other_route", day(1), items=0, route="twitter/user/tweets"),
              dict(rot("panel:other_lane", day(1), items=0), lane_class="watchlist"),
              rot("panel:other_market", day(1), items=0, market="NG")]
    got = by_protocol(loaded(others + [dead]))
    assert {k[1]: v for k, v in got.items() if k[0] == day(1)} == {
        "panel:other_route": (True, None), "panel:other_lane": (True, None), "panel:other_market": (True, None)}


def test_a_day_invalid_for_any_other_reason_marks_nothing():
    for reason in ("calls", "items", "effort", "drift"):
        got = view(loaded([health("p", day(1)), dict(health("p", D), valid=False, invalid_reason=reason)]))
        assert got[(day(1), "ZA", "p")] == (True, None), reason


def test_a_zero_yield_day_does_not_mark_the_day_after_it():
    got = view(loaded([zero("p", day(1)), health("p", D)]))
    assert got[(D, "ZA", "p")] == (True, None)


def test_a_zero_yield_row_of_a_run_that_is_not_the_good_run_marks_nothing():
    duck.load(con := world(), "agent.runs", [run("collect", D, run_id="collect-late-fail", status="failed", hour=20)])
    duck.load(con, "core.collection_health", [health("p", day(1)), dict(zero("p", D), run_id="collect-late-fail")])
    assert view(con)[(day(1), "ZA", "p")] == (True, None)


def test_a_repair_run_that_reads_the_key_valid_clears_the_mark():
    con = world()
    duck.load(con, "agent.runs", [run("collect", D, run_id="collect-repair", hour=18)])
    duck.load(con, "core.collection_health", [
        health("p", day(1)), zero("p", D), dict(health("p", D), run_id="collect-repair")])
    got = view(con)
    assert got[(D, "ZA", "p")] == (True, None) and got[(day(1), "ZA", "p")] == (True, None)


# G1 reads the new rows

SERIES = "i1|ZA|panel_culture_desk|p1"


def culture_desk_rows(*, dead):
    desk = dict(platform=None, lane_class="panel", k=5.0)
    first = dict(health("panel_culture_desk", day(1), **desk), items=0) if dead else health(
        "panel_culture_desk", day(1), **desk)
    second = zero("panel_culture_desk", D, **desk) if dead else health("panel_culture_desk", D, **desk)
    return [health("panel_culture_desk", day(2), **desk), first, second]


def test_g1_holds_a_card_whose_main_series_is_a_route_that_landed_nothing_two_days_running():
    got = ctx(loaded(culture_desk_rows(dead=True), "instagram"), item(main_series_id=SERIES))
    assert got["valid_days"] == [False, False, True]
    decision = gate_card({"item_id": "i1"}, got)
    assert (decision.rule, decision.flag, decision.publish) == ("G1", "Data issue", False)
    assert "2 of the last 3 market-days invalid" in decision.reason


def test_the_same_series_with_landing_rows_is_not_held_by_g1():
    got = ctx(loaded(culture_desk_rows(dead=False), "instagram"), item(main_series_id=SERIES))
    assert got["valid_days"] == [True, True, True]
    assert gate_card({"item_id": "i1"}, got).rule != "G1"


def test_one_zero_day_does_not_hold_a_card():
    rows = [health("panel_culture_desk", day(2), platform=None, lane_class="panel", k=5.0),
            health("panel_culture_desk", day(1), platform=None, lane_class="panel", k=5.0),
            dict(health("panel_culture_desk", D, platform=None, lane_class="panel", k=5.0), items=0)]
    assert ctx(loaded(rows, "instagram"), item(main_series_id=SERIES))["valid_days"] == [True, True, True]


# G1's hold rule and its d, d-1, d-2 window are the code that shipped at B0. Each digest is a SHA-256 of the
# text, pinned here, never read from the code it checks.

def digest(path, start, end):
    text = (ROOT / path).read_text(encoding="utf-8").replace("\r\n", "\n")
    block = text[text.index(start):text.index(end, text.index(start)) + len(end)]
    return hashlib.sha256(block.encode("utf-8")).hexdigest()


PINNED = {
    "g1_rule": ("trust/gate.py", '    invalid = [i for i, ok in enumerate(ctx["valid_days"]) if ok is False]',
                '"Data issue",\n        )', "PIN_G1_RULE"),
    "g1_window_sql": ("brief/sql/gatectx.sql", "-- name: health", "GROUP BY h.day;", "PIN_G1_SQL"),
    "g1_window_python": ("brief/gatectx.py", "    valid_days = [days.get(d - timedelta(days=i)) for i in range(3)]",
                         "for i in range(3)]", "PIN_G1_PY"),
}
PIN_G1_RULE = "a711cdebd18c91e15a9324decc22bc0422947e2315a28433e66b70e90141a184"
PIN_G1_SQL = "0eb0182784742689d02b2a43496db6cbf1048acd8c23c5cc076d29260753bb39"
PIN_G1_PY = "91769c4f1208ea06bc7c353a366d4f3fcf14f6fe3d170115e6667e8d8ab41ea3"


@pytest.mark.parametrize("name", sorted(PINNED))
def test_g1_rule_and_window_are_byte_identical_to_b0(name):
    path, start, end, pin = PINNED[name]
    assert digest(path, start, end) == globals()[pin]
