"""RB-C3: the curated creator panel and the culture desk read one route, prism/profiles, in one lane, and used to
share the series panel_culture_desk. Alternating zero days of the two then raised a spurious G1 data issue. The
curated panel now has its own series. No network."""

import pytest

from core.brief.tests.test_brief_gatectx import ctx, item, series_platform, world
from core.collect import job, parse, writers
from core.collect.tests.test_job import FakeClient, _desk, all_planned, collect
from core.collect.tests.test_parse import FakeGeo, fixture
from core.collect.tests.test_parse import run as parse_run
from core.collect.tests.test_zero_yield import prior_query, rec, written
from core.detect.tests import duck
from core.detect.tests.fixtures import D, day, rid
from core.trust.gate import gate_card

DESK = "panel_culture_desk"
CURATED = "panel_curated_creators"


def panel_series():
    """The series the job plans for the desk and for the curated panel: read from the planned calls, never typed."""
    desk, curated = planned_panels()
    (desk_series,), (curated_series,) = {c.series() for c in desk}, {c.series() for c in curated}
    return desk_series, curated_series


def planned_panels(market="ZA"):
    calls = [c for c in all_planned() if c.market == market and c.route == "prism/profiles"]
    desk = [c for c in calls if _desk(c)]
    curated = [c for c in calls if not _desk(c)]
    assert desk and curated
    return desk, curated


PLAN_DESK, PLAN_CURATED = panel_series()


def test_the_curated_panel_has_its_own_series_and_the_desk_keeps_its_own():
    desk, curated = planned_panels()
    assert {c.series() for c in desk} == {DESK}
    assert {c.series() for c in curated} == {CURATED}
    assert {c.series_protocol() for c in desk}.isdisjoint({c.series_protocol() for c in curated})


def test_the_series_name_follows_the_panel_naming_style():
    assert parse.CURATED_PANEL_SERIES == CURATED
    assert CURATED.startswith("panel_") and CURATED != DESK


def test_every_market_plans_the_split_not_only_za():
    for market in job.MARKETS:
        desk, curated = planned_panels(market)
        assert {c.series() for c in desk} == {DESK} and {c.series() for c in curated} == {CURATED}


def test_a_run_writes_each_panels_rows_and_health_under_its_own_series():
    run_ = collect(FakeClient())
    desk_protocols = {c.series_protocol() for c in all_planned() if c.route == "prism/profiles" and _desk(c)}
    profiles = [r for r in run_.records if r["route"] == "prism/profiles"]
    desk_rows = [r for r in profiles if r["protocol"] in desk_protocols]
    curated_rows = [r for r in profiles if r["protocol"] not in desk_protocols]
    assert desk_rows and curated_rows
    assert {r["series"] for r in desk_rows} == {DESK}
    assert {r["series"] for r in curated_rows} == {CURATED}
    seen = {(o["series"], o["protocol"]) for o in run_.observations if o["route"] == "prism/profiles"}
    assert seen and all((s == DESK) == (p in desk_protocols) for s, p in seen)
    assert {s for s, _ in seen} == {DESK, CURATED}


def test_parse_keeps_the_desk_series_by_default_and_names_the_curated_one_on_request():
    body = fixture("panels", "prism_profiles")
    params = {"include": "posts", "since": "2026-09-27"}
    desk = parse_run("prism/profiles", params, "ZA", body, geo=FakeGeo())
    curated = parse_run("prism/profiles", params, "ZA", body, geo=FakeGeo(), curated=True)
    assert {o["series"] for o in desk["observations"]} == {DESK}
    assert {o["series"] for o in curated["observations"]} == {CURATED}
    assert {o["lane"] for o in curated["observations"]} == {"panel"}
    assert {o["lane_class"] for o in curated["observations"]} == {"panel"}


def test_only_prism_profiles_can_be_read_as_the_curated_panel():
    body = fixture("panels", "twitter_tweets")
    with pytest.raises(ValueError):
        parse_run("twitter/user/tweets", {"handle": "SundayTimesZA", "since": "2026-09-27"}, "ZA", body,
                  curated=True)


# The days of one market's two panels, written the way the collect job writes them, one run a day.

def write_days(spec, desk_series=None, curated_series=None):
    """spec: (date, desk lands, curated lands, curated protocol) per day, oldest first. Returns every row written,
    each day's run reading the stored rows of the days before as the zero-yield prior."""
    runs, rows = {}, []
    planned_desk, planned_curated = panel_series()
    desk_series, curated_series = desk_series or planned_desk, curated_series or planned_curated
    for d, desk_lands, curated_lands, curated_protocol in spec:
        iso = d.isoformat()
        prior = {iso: {(r["market"], r["series"], r["route"], r["lane_class"])
                       for r in prior_query(runs, rows, d) if writers.zero_yield_route(r["route"], r["lane_class"])}}
        run_id = rid("collect", d)
        runs[run_id] = (d, 8, "ok")
        records = [
            rec(iso, series=desk_series, protocol="panel:desk", items=4 if desk_lands else 0,
                post_ids=[f"d{iso}"] if desk_lands else []),
            rec(iso, series=curated_series, protocol=curated_protocol, items=3 if curated_lands else 0,
                post_ids=[f"c{iso}"] if curated_lands else [])]
        rows += written(records, run_id, prior)
    return rows


def stored(rows):
    con = world()
    duck.load(con, "core.collection_health", rows)
    return con


def current(con):
    got = duck.query(con, "SELECT v.day, v.series, v.protocol, v.valid, v.invalid_reason "
                          "FROM {core}.v_collection_health_current v ORDER BY v.day, v.series, v.protocol")
    return {(r["day"], r["series"], r["protocol"]): (r["valid"], r["invalid_reason"]) for r in got}


def card(con, series=None, protocol="panel:desk"):
    """G1's window and decision for an item whose main series is the panel's, as the publish gate reads it."""
    sid = f"i1|ZA|{series or panel_series()[0]}|{protocol}"
    series_platform(con, "instagram", sid)
    c = ctx(con, item(main_series_id=sid))
    return c["valid_days"], gate_card({"item_id": "i1"}, c)


PROBE = [(day(2), True, True, "panel:rot0"), (day(1), False, True, "panel:rot1"), (D, True, False, "panel:rot2")]


def test_probe_g1_a_desk_zero_day_then_a_curated_zero_day_raises_no_data_issue():
    """The reviewer's probe (REVIEW closure A, G1-a): the desk lands nothing on day 1 while the curated panel lands;
    on day 2 the desk lands and the next curated rotation lands nothing. Neither panel had two zero days."""
    rows = write_days(PROBE)
    con = stored(rows)
    days, decision = card(con)
    assert decision is None or decision.rule != "G1", (days, decision.reason)
    assert days == [True, True, True]
    assert all(v == (True, None) for v in current(con).values())
    assert all(r["valid"] and r["invalid_reason"] is None for r in rows), [
        (r["day"], r["series"], r["protocol"], r["invalid_reason"]) for r in rows if not r["valid"]]


def test_the_same_probe_for_a_card_on_the_curated_panel_raises_no_data_issue():
    days, decision = card(stored(write_days(PROBE)), panel_series()[1], "panel:rot2")
    assert days[0] is not False
    assert decision is None or decision.rule != "G1"


def test_each_panel_still_gets_zero_yield_after_two_zero_days_of_its_own():
    desk_dead = write_days([(day(2), True, True, "panel:rot0"), (day(1), False, True, "panel:rot1"),
                            (D, False, True, "panel:rot2")])
    got = {(r["day"], r["series"]): r["invalid_reason"] for r in desk_dead if r["protocol"] == "panel:desk"}
    assert got == {(day(2), PLAN_DESK): None, (day(1), PLAN_DESK): None, (D, PLAN_DESK): "zero_yield"}
    assert all(r["invalid_reason"] is None for r in desk_dead if r["series"] == PLAN_CURATED)
    con = stored(desk_dead)
    assert current(con)[(day(1), PLAN_DESK, "panel:desk")] == (False, "zero_yield")
    days, decision = card(con)
    assert days == [False, False, True]
    assert decision is not None and decision.rule == "G1"

    curated_dead = write_days([(day(2), True, True, "panel:rot0"), (day(1), True, False, "panel:rot1"),
                               (D, True, False, "panel:rot2")])
    got = {(r["day"], r["protocol"]): r["invalid_reason"] for r in curated_dead if r["series"] == PLAN_CURATED}
    assert got == {(day(2), "panel:rot0"): None, (day(1), "panel:rot1"): None, (D, "panel:rot2"): "zero_yield"}
    assert all(r["invalid_reason"] is None for r in curated_dead if r["series"] == PLAN_DESK)
    assert current(stored(curated_dead))[(day(1), PLAN_CURATED, "panel:rot1")] == (False, "zero_yield")


# The change day. Rows written before the split stay as written, all under panel_culture_desk, the curated
# rotations beside the desk. Nothing rewrites or deletes them.

BEFORE = [(day(3), True, True, "panel:rot0"), (day(2), True, False, "panel:rot1")]
BEFORE_D = [(day(2), True, True, "panel:rot0"), (day(1), True, False, "panel:rot1")]


def before_the_split(spec=BEFORE):
    old = write_days(spec, desk_series=DESK, curated_series=DESK)
    runs = {rid("collect", d): (d, 8, "ok") for d, *_ in spec}
    return old, runs


def change_day(old, runs, d, desk_items, curated_items, curated_protocol):
    """The first day written by the split code, reading the stored rows of the days before as its prior."""
    iso = d.isoformat()
    prior = {iso: {(r["market"], r["series"], r["route"], r["lane_class"])
                   for r in prior_query(runs, old, d) if writers.zero_yield_route(r["route"], r["lane_class"])}}
    return written([rec(iso, series=PLAN_DESK, protocol="panel:desk", items=desk_items,
                        post_ids=["dD"] if desk_items else []),
                    rec(iso, series=PLAN_CURATED, protocol=curated_protocol, items=curated_items,
                        post_ids=["cD"] if curated_items else [])], rid("collect", d), prior)


def test_change_day_a_curated_zero_day_before_the_split_does_not_mark_the_new_series():
    """The curated rotation landed nothing the day before, under the old series, and lands nothing again on the
    change day under its new one. The new series has no day before, so its first day stays valid."""
    old, runs = before_the_split(BEFORE_D)
    new = change_day(old, runs, D, desk_items=4, curated_items=0, curated_protocol="panel:rot2")
    assert [(r["series"], r["valid"], r["invalid_reason"]) for r in new] == [
        (PLAN_DESK, True, None), (PLAN_CURATED, True, None)]
    con = stored(old + new)
    assert all(v == (True, None) for v in current(con).values())
    days, decision = card(con, protocol="panel:desk")
    assert days == [True, True, True] and (decision is None or decision.rule != "G1")


def test_change_day_residual_a_desk_zero_day_after_an_old_curated_zero_day_is_still_marked():
    """Named residual, not new: the day before the split holds a curated zero day under panel_culture_desk, and the
    desk lands nothing on the change day. The writer reads the old series key as the desk's day before, exactly as
    it did when the two shared the series, so the desk is marked zero_yield on the change day only. From the next
    day the old series is read by the desk alone. The mark fails safe: it holds a card, never passes one."""
    old, runs = before_the_split(BEFORE_D)
    new = change_day(old, runs, D, desk_items=0, curated_items=3, curated_protocol="panel:rot2")
    assert {(r["series"], r["invalid_reason"]) for r in new} == {(PLAN_DESK, "zero_yield"), (PLAN_CURATED, None)}
    days, decision = card(stored(old + new))
    assert days == [False, True, True] and decision is not None and decision.rule == "G1"


def test_after_the_change_day_the_old_series_is_read_by_the_desk_alone():
    old, runs = before_the_split()
    day1 = change_day(old, runs, day(1), desk_items=4, curated_items=3, curated_protocol="panel:rot2")
    runs[rid("collect", day(1))] = (day(1), 8, "ok")
    day0 = change_day(old + day1, runs, D, desk_items=0, curated_items=0, curated_protocol="panel:rot3")
    assert [(r["series"], r["valid"], r["invalid_reason"]) for r in day0] == [
        (PLAN_DESK, True, None), (PLAN_CURATED, True, None)]
    con = stored(old + day1 + day0)
    assert all(v == (True, None) for v in current(con).values())


def test_the_rows_before_the_split_stay_as_written():
    old, _ = before_the_split()
    assert {r["series"] for r in old} == {DESK}
    assert [(r["day"], r["protocol"], r["valid"]) for r in old] == [
        (day(3), "panel:desk", True), (day(3), "panel:rot0", True),
        (day(2), "panel:desk", True), (day(2), "panel:rot1", True)]
