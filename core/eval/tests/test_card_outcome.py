"""Card and hold outcome measure (METHOD-GAPS section 7): synthetic briefs and item_state rows with known outcomes.

Every expected value here is written out by hand or taken from scipy's Wilson interval, never read back from the
module under test or from the rows it is scoring."""

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from core.detect import backtest
from core.eval import card_outcome as co

D = dt.date
T = D(2026, 9, 24)
ALL_DAYS = {T + dt.timedelta(days=i) for i in range(0, 20)}  # 24 Sep to 13 Oct, every day has a good detect run
SQL = Path(co.__file__).parent / "sql" / "card_outcome.sql"
SCHEMA = Path(co.__file__).parents[1] / "schema" / "card_outcome.sql"


def card(item_id, rank, state="rising"):
    return {"item_id": item_id, "rank": rank, "state": state, "title": "ignored", "evidence": [{"text": "ignored"}]}


def held(item_id, reason="not_confirmed", rule="G3"):
    return {"item_id": item_id, "reason": reason, "rule": rule, "title": "ignored"}


def brief(market="ZA", day=T, cards=(), more=(), held_items=(), status="published", run_id="r1", published_at=None):
    return {"brief_date": day, "market": market, "run_id": run_id, "status": status,
            "published_at": published_at or dt.datetime(day.year, day.month, day.day, 5, tzinfo=dt.timezone.utc),
            "payload": {"cards": list(cards), "more": list(more), "held_back": {"items": list(held_items)}}}


def st(item_id, offset, state, lane="panel", market="ZA", untested=False):
    return {"metric_date": T + dt.timedelta(days=offset), "market": market, "item_id": item_id, "state": state,
            "main_lane_class": lane, "untested": untested}


def by_item(rows):
    return {r["item_id"]: r for r in rows}


def scenario():
    briefs = [brief(
        cards=[card("A", 1), card("B", 2), card("C", 3), card("D", 4), card("F", 5, "seasonal")],
        more=[card("E", 6), card("G", 7), card("Z", 8, "recurring")],
        held_items=[held("H1"), held("H2"), held("H3", "likely_coordinated", "G4"), held("H4", "explanation_failed", "G10")])]
    states = [
        # A: Rising at t+3, Peaking at t+7 on a panel lane, Fading at t+14: held at 7 days.
        st("A", 3, "rising"), st("A", 7, "peaking"), st("A", 14, "fading"),
        # B: Fading at t+7 is collapsed.
        st("B", 3, "rising"), st("B", 7, "fading"), st("B", 14, "fading"),
        # C: no row at t+7 on a day with a good detect run is absent, which is collapsed.
        # D: Rising at t+7 but only on a search lane: unmeasured, no label either way.
        st("D", 7, "rising", lane="search_presence"), st("D", 3, "rising", lane="search_presence"),
        # E: Mainstream at t+7 is neither held nor collapsed.
        st("E", 7, "mainstream", lane="unbiased_rank"),
        # F: a seasonal card that is Recurring at t+7 does not count as held.
        st("F", 7, "recurring", lane="unbiased_counter"),
        # G: a row with no main series lane at all is unmeasured.
        st("G", 7, "emerging", lane=None),
        # Z: seasonal stratum, Rising at t+7 measured.
        st("Z", 7, "rising"),
        # Held items: H1 Emerging at t+7, H2 Fading, H3 absent, H4 Rising on a watchlist lane.
        st("H1", 0, "emerging"), st("H1", 7, "emerging"), st("H2", 0, "rising"), st("H2", 7, "fading"),
        st("H3", 0, "spike"), st("H4", 0, "rising"), st("H4", 7, "rising", lane="watchlist"),
    ]
    return briefs, states


def test_known_outcomes_give_exact_rows():
    briefs, states = scenario()
    rows = by_item(co.build_outcomes(briefs, states, ALL_DAYS))
    assert len(rows) == 12
    assert {(r["kind"], r["market"], r["run_date"]) for r in rows.values()} == {
        ("published", "ZA", T), ("held", "ZA", T)}
    want = {  # item: (outcome_t7, state_t7, measured, held, collapsed)
        "A": ("held", "peaking", True, True, False),
        "B": ("collapsed", "fading", True, False, True),
        "C": ("collapsed", "absent", True, False, True),
        "D": ("unmeasured", "rising", False, False, False),
        "E": ("other", "mainstream", True, False, False),
        "F": ("other", "recurring", True, False, False),
        "G": ("unmeasured", "emerging", False, False, False),
        "Z": ("held", "rising", True, True, False),
        "H1": ("held", "emerging", True, True, False),
        "H2": ("collapsed", "fading", True, False, True),
        "H3": ("collapsed", "absent", True, False, True),
        "H4": ("unmeasured", "rising", False, False, False),
    }
    for item, (outcome, state7, measured, is_held, collapsed) in want.items():
        r = rows[item]
        assert (r["outcome_t7"], r["state_t7"], r["measured"], r["held"], r["collapsed"]) == (
            outcome, state7, measured, is_held, collapsed), item


def test_state_and_rank_at_t_and_other_horizons():
    briefs, states = scenario()
    rows = by_item(co.build_outcomes(briefs, states, ALL_DAYS))
    a = rows["A"]
    assert (a["kind"], a["rank"], a["state_t"], a["surface"], a["hold_reason"]) == ("published", 1, "rising", "cards", None)
    assert (a["state_t3"], a["outcome_t3"], a["state_t14"], a["outcome_t14"]) == ("rising", "held", "fading", "collapsed")
    assert rows["E"]["rank"] == 6 and rows["E"]["surface"] == "more"
    h1 = rows["H1"]
    assert (h1["kind"], h1["rank"], h1["state_t"], h1["hold_reason"], h1["rule"]) == ("held", None, "emerging", "not_confirmed", "G3")
    assert rows["H3"]["hold_reason"] == "likely_coordinated" and rows["H3"]["rule"] == "G4"
    assert rows["H4"]["hold_reason"] == "explanation_failed"
    assert rows["C"]["state_t3"] == "absent" and rows["C"]["outcome_t3"] == "collapsed"


def test_strata_seasonal_and_recurring_are_split_from_trends():
    briefs, states = scenario()
    rows = by_item(co.build_outcomes(briefs, states, ALL_DAYS))
    assert rows["F"]["stratum"] == "scheduled" and rows["Z"]["stratum"] == "scheduled"
    assert all(rows[i]["stratum"] == "trend" for i in ("A", "B", "C", "D", "E", "G", "H1", "H2", "H3", "H4"))


def test_card_whose_later_rows_come_only_from_search_lanes_is_unmeasured():
    briefs = [brief(cards=[card("S", 1)])]
    states = [st(i, o, "rising", lane=lane) for o in (3, 7, 14) for i, lane in
              (("S", "search_presence"),)]
    r = co.build_outcomes(briefs, states, ALL_DAYS)[0]
    assert (r["outcome_t3"], r["outcome_t7"], r["outcome_t14"]) == ("unmeasured",) * 3
    assert r["measured"] is False and r["held"] is False and r["collapsed"] is False
    for lane in ("search_presence", "watchlist", "legacy", "agent_live", "placebo", "seed", "x_trends", "", None):
        r = co.build_outcomes(briefs, [st("S", 7, "peaking", lane=lane)], ALL_DAYS)[0]
        assert r["outcome_t7"] == "unmeasured", lane
        assert r["held"] is False


def test_measured_lanes_are_pinned_not_read_from_input():
    assert co.MEASURED_LANES == frozenset({"unbiased_rank", "unbiased_counter", "panel"})
    briefs = [brief(cards=[card("S", 1)])]
    row = dict(st("S", 7, "rising", lane="search_presence"), measured=True, lane_is_measured=True)
    r = co.build_outcomes(briefs, [row], ALL_DAYS)[0]
    assert r["outcome_t7"] == "unmeasured"


def test_day_without_a_good_detect_run_is_pending_not_absent():
    briefs = [brief(cards=[card("P", 1)])]
    days = ALL_DAYS - {T + dt.timedelta(days=7)}
    r = co.build_outcomes(briefs, [], days)[0]
    assert (r["outcome_t3"], r["outcome_t7"], r["state_t7"]) == ("collapsed", "pending", None)
    assert r["measured"] is False and r["held"] is False and r["collapsed"] is False
    # Beyond the last detect day t + 14 is pending too.
    r = co.build_outcomes(briefs, [], {T + dt.timedelta(days=i) for i in range(0, 9)})[0]
    assert (r["outcome_t7"], r["outcome_t14"]) == ("collapsed", "pending")


def test_row_on_a_day_without_a_detect_run_is_ignored():
    briefs = [brief(cards=[card("P", 1)])]
    days = ALL_DAYS - {T + dt.timedelta(days=7)}
    r = co.build_outcomes(briefs, [st("P", 7, "rising")], days)[0]
    assert r["outcome_t7"] == "pending"


def test_markets_are_kept_apart():
    briefs = [brief(market="ZA", cards=[card("A", 1)]), brief(market="NG", cards=[card("A", 1)], run_id="r2")]
    states = [st("A", 7, "rising", market="ZA"), st("A", 7, "fading", market="NG")]
    rows = {r["market"]: r for r in co.build_outcomes(briefs, states, ALL_DAYS)}
    assert rows["ZA"]["outcome_t7"] == "held" and rows["NG"]["outcome_t7"] == "collapsed"


def test_data_issue_brief_is_skipped_and_latest_brief_per_day_and_market_wins():
    old = brief(cards=[card("OLD", 1)], run_id="r0", published_at=dt.datetime(2026, 9, 24, 4, tzinfo=dt.timezone.utc))
    new = brief(cards=[card("NEW", 1)], run_id="r1")
    bad = brief(market="KE", cards=[card("BAD", 1)], status="data_issue", run_id="r9")
    ids = {r["item_id"] for r in co.build_outcomes([old, new, bad], [], ALL_DAYS)}
    assert ids == {"NEW"}


def test_payload_may_arrive_as_json_text():
    b = brief(cards=[card("A", 1)])
    b["payload"] = json.dumps(b["payload"])
    assert co.build_outcomes([b], [st("A", 7, "rising")], ALL_DAYS)[0]["outcome_t7"] == "held"


def test_duplicate_state_rows_fail_loudly():
    with pytest.raises(ValueError):
        co.build_outcomes([brief(cards=[card("A", 1)])], [st("A", 7, "rising"), st("A", 7, "fading")], ALL_DAYS)


def test_persisting_is_the_backtest_constant_and_fading_is_not_in_it():
    assert co.PERSISTING is backtest.PERSISTING
    assert backtest.PERSISTING == ("emerging", "rising", "peaking")
    assert "fading" not in co.PERSISTING
    assert co.COLLAPSED_STATES == ("fading",)
    assert not set(co.PERSISTING) & set(co.COLLAPSED_STATES)


def test_fading_card_is_collapsed_and_never_held():
    briefs = [brief(cards=[card("B", 1)], held_items=[held("H")])]
    rows = by_item(co.build_outcomes(briefs, [st("B", 7, "fading"), st("H", 7, "fading")], ALL_DAYS))
    for item in ("B", "H"):
        assert rows[item]["held"] is False and rows[item]["collapsed"] is True and rows[item]["outcome_t7"] == "collapsed"
    s = co.summarize(list(rows.values()), end=T, min_n=1)
    pub = next(g for g in s if (g["kind"], g["market"], g["hold_reason"], g["stratum"]) == ("published", "ZA", None, "all"))
    assert (pub["n"], pub["held"], pub["collapsed"]) == (1, 0, 1)


def synthetic_rows(kind, market, reason, held, collapsed, other, unmeasured=0, pending=0, day=T, stratum="trend"):
    out = []
    spec = [("held", held), ("collapsed", collapsed), ("other", other), ("unmeasured", unmeasured), ("pending", pending)]
    for outcome, count in spec:
        for i in range(count):
            out.append({"run_date": day, "market": market, "item_id": f"{outcome}{i}", "kind": kind,
                        "hold_reason": reason, "stratum": stratum, "outcome_t7": outcome})
    return out


def group(summary, kind, market, reason=None, stratum="all"):
    return next(g for g in summary if (g["kind"], g["market"], g["hold_reason"], g["stratum"]) == (kind, market, reason, stratum))


def test_summary_counts_exact_and_unmeasured_and_pending_stay_out_of_the_denominator():
    rows = synthetic_rows("published", "ZA", None, held=24, collapsed=10, other=6, unmeasured=5, pending=3)
    g = group(co.summarize(rows, end=T), "published", "ZA")
    assert (g["n"], g["held"], g["collapsed"], g["other"], g["unmeasured"], g["pending"], g["total"]) == (40, 24, 10, 6, 5, 3, 48)
    assert g["rate"] == pytest.approx(0.6)
    assert g["lo"] == pytest.approx(0.445959, abs=1e-6) and g["hi"] == pytest.approx(0.736517, abs=1e-6)
    assert g["text"].startswith("60.0%")


def test_below_thirty_prints_not_enough_data_and_no_rate():
    for n in (0, 1, 29):
        rows = synthetic_rows("published", "ZA", None, held=n, collapsed=0, other=0)
        g = group(co.summarize(rows, end=T), "published", "ZA") if n else None
        if g is None:
            assert co.summarize(rows, end=T) == []
            continue
        assert g["n"] == n and g["rate"] is None and g["lo"] is None and g["hi"] is None
        assert g["text"] == "not enough data"
    rows = synthetic_rows("published", "ZA", None, held=9, collapsed=21, other=0)
    g = group(co.summarize(rows, end=T), "published", "ZA")
    assert g["n"] == 30 and g["rate"] == pytest.approx(0.3)
    assert g["lo"] == pytest.approx(0.166647, abs=1e-6) and g["hi"] == pytest.approx(0.478758, abs=1e-6)


def test_wilson_edges_stay_inside_zero_and_one():
    g = group(co.summarize(synthetic_rows("held", "NG", "not_confirmed", 0, 30, 0), end=T), "held", "NG", "not_confirmed")
    assert g["rate"] == 0 and g["lo"] == pytest.approx(0.0, abs=1e-12) and g["hi"] == pytest.approx(0.113513, abs=1e-6)
    g = group(co.summarize(synthetic_rows("held", "NG", "not_confirmed", 30, 0, 0), end=T), "held", "NG", "not_confirmed")
    assert g["rate"] == 1 and g["lo"] == pytest.approx(0.886487, abs=1e-6) and g["hi"] == pytest.approx(1.0, abs=1e-12)


def test_summary_pools_four_weeks_and_drops_older_rows():
    end = D(2026, 10, 1)
    inside = synthetic_rows("published", "ZA", None, 5, 0, 0, day=end - dt.timedelta(days=27))
    outside = synthetic_rows("published", "ZA", None, 0, 7, 0, day=end - dt.timedelta(days=28))
    later = synthetic_rows("published", "ZA", None, 0, 0, 3, day=end + dt.timedelta(days=1))
    g = group(co.summarize(inside + outside + later, end=end), "published", "ZA")
    assert (g["n"], g["held"], g["collapsed"], g["other"]) == (5, 5, 0, 0)
    assert g["window_start"] == D(2026, 9, 4) and g["window_end"] == end


def test_summary_groups_per_market_per_reason_with_all_rollup_and_strata():
    rows = (synthetic_rows("held", "ZA", "not_confirmed", 2, 1, 0) + synthetic_rows("held", "NG", "not_confirmed", 0, 4, 0)
            + synthetic_rows("held", "ZA", "explanation_failed", 1, 0, 0)
            + synthetic_rows("held", "ZA", "not_confirmed", 3, 0, 0, stratum="scheduled"))
    s = co.summarize(rows, end=T, min_n=1)
    assert (group(s, "held", "ZA", "not_confirmed")["n"], group(s, "held", "ZA", "not_confirmed")["held"]) == (6, 5)
    assert (group(s, "held", "ZA", "not_confirmed", "scheduled")["n"], group(s, "held", "ZA", "not_confirmed", "scheduled")["held"]) == (3, 3)
    assert (group(s, "held", "ZA", "not_confirmed", "trend")["n"], group(s, "held", "ZA", "not_confirmed", "trend")["held"]) == (3, 2)
    assert group(s, "held", "NG", "not_confirmed")["held"] == 0
    assert group(s, "held", "ZA", "explanation_failed")["n"] == 1
    allm = group(s, "held", "ALL", "not_confirmed")
    assert (allm["n"], allm["held"]) == (10, 5)
    assert group(s, "held", "ALL", None)["n"] == 11


def test_markdown_never_prints_a_rate_below_thirty():
    rows = synthetic_rows("published", "ZA", None, 5, 2, 0)
    md = co.to_markdown(co.summarize(rows, end=T), end=T)
    assert "not enough data" in md and "71" not in md


def test_json_roundtrip_has_no_text_or_handles():
    briefs, states = scenario()
    rows = co.build_outcomes(briefs, states, ALL_DAYS)
    blob = json.dumps(rows, default=str)
    assert "ignored" not in blob


def test_sql_is_select_only_and_partition_filtered():
    text = SQL.read_text(encoding="utf-8")
    queries = co.split_queries(text)
    assert set(queries) == {"briefs", "states", "detect_days"}
    for name, q in queries.items():
        body = re.sub(r"--[^\n]*", "", q).strip().upper()
        assert body.startswith("SELECT") or body.startswith("WITH"), name
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|EXPORT|CALL|EXECUTE)\b", body), name
    assert "brief_date BETWEEN @start AND @end" in queries["briefs"]
    assert "metric_date BETWEEN" in queries["states"]
    assert "run_date BETWEEN" in queries["detect_days"]
    assert "payload" in queries["briefs"] and "TO_JSON_STRING(payload)" not in queries["briefs"].replace(" ", "")
    for banned in ("title", "text", "handle", "url", "evidence"):
        assert not re.search(rf"\$\.[a-z_.]*{banned}", queries["briefs"]), banned


def test_schema_file_is_create_if_not_exists_only():
    text = SCHEMA.read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/|--[^\n]*", "", text, flags=re.S)
    statements = [s.strip() for s in body.split(";") if s.strip()]
    assert statements and all(s.upper().startswith("CREATE TABLE IF NOT EXISTS") for s in statements)
    assert "PARTITION BY run_date" in text
    for col in ("run_date", "market", "item_id", "kind", "hold_reason", "state_t", "state_t7", "outcome_t7", "stratum", "scored_at"):
        assert re.search(rf"\b{col}\b", text), col


def test_persisting_seen_counts_persisting_states_at_t_and_later():
    briefs, states = scenario()
    rows = co.build_outcomes(briefs, states, ALL_DAYS)
    # At t: cards A, B, C, D, E, G are rising (6), held H1 emerging, H2 and H4 rising (3). At t+3: A, B, D rising (3).
    # At t+7: A peaking, D rising, G emerging, Z rising, H1 emerging, H4 rising (6). At t+14 only A is fading (0).
    assert co.persisting_seen(rows) == 18
    none = co.build_outcomes([brief(cards=[card("X", 1, "spike")])], [st("X", 7, "on_the_boards")], ALL_DAYS)
    assert co.persisting_seen(none) == 0


def test_markdown_warns_when_no_persisting_state_was_seen_and_not_otherwise():
    rows = synthetic_rows("published", "ZA", None, 0, 0, 40)
    s = co.summarize(rows, end=T)
    assert "\n\nNo Emerging, Rising or Peaking state" in co.to_markdown(s, end=T, persisting_seen=0)
    assert "No Emerging, Rising or Peaking state" not in co.to_markdown(s, end=T, persisting_seen=3)
    assert "No Emerging, Rising or Peaking state" not in co.to_markdown(s, end=T)


def test_state_mix_counts_state_at_t_and_later_state_by_outcome():
    briefs, states = scenario()
    rows = co.build_outcomes(briefs, states, ALL_DAYS)
    mix = {(m["kind"], m["state_t"], m["state_later"], m["outcome"]): m["n"] for m in co.state_mix(rows, horizon=7)}
    assert mix[("published", "rising", "peaking", "held")] == 1
    assert mix[("published", "rising", "absent", "collapsed")] == 1
    assert mix[("published", "rising", "fading", "collapsed")] == 1
    assert mix[("published", "rising", "rising", "unmeasured")] == 1
    assert sum(mix.values()) == 12
