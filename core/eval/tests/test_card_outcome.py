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


def st(item_id, offset, state, lane="panel", market="ZA", untested=False, base=None, signals=None):
    return {"metric_date": T + dt.timedelta(days=offset), "market": market, "item_id": item_id, "state": state,
            "main_lane_class": lane, "untested": untested, "base_state": base,
            "signal_lanes": [lane] if signals is None else signals}


def by_item(rows):
    return {r["item_id"]: r for r in rows}


def scenario():
    briefs = [brief(
        cards=[card("A", 1), card("B", 2), card("C", 3), card("D", 4), card("F", 5, "seasonal")],
        more=[card("E", 6), card("G", 7), card("Z", 8, "recurring"), card("K", 9, "spike"), card("L", 10, "new_to_42"),
              card("M", 11, "on_the_boards"), card("N", 12, "spike")],
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
        st("F", 7, "recurring", lane="unbiased_counter", base="rising"),
        # G: a row with no main series lane at all is unmeasured.
        st("G", 7, "emerging", lane=None),
        # Z: seasonal stratum, Rising at t+7 measured.
        st("Z", 7, "rising"),
        # K Spike is held; L New to 42 and M On the boards are only listed (outside the product's active28); N has a row
        # with no state at all, which is neither.
        st("K", 7, "spike"), st("L", 7, "new_to_42", lane="unbiased_rank"),
        st("M", 7, "on_the_boards", lane="unbiased_rank"), st("N", 7, None),
        # Held items: H1 Emerging at t+7, H2 Fading, H3 absent, H4 Rising on a watchlist lane.
        st("H1", 0, "emerging"), st("H1", 7, "emerging"), st("H2", 0, "rising"), st("H2", 7, "fading"),
        st("H3", 0, "spike"), st("H4", 0, "rising"), st("H4", 7, "rising", lane="watchlist"),
    ]
    return briefs, states


def test_known_outcomes_give_exact_rows():
    briefs, states = scenario()
    rows = by_item(co.build_outcomes(briefs, states, ALL_DAYS))
    assert len(rows) == 16
    assert {(r["kind"], r["market"], r["run_date"]) for r in rows.values()} == {
        ("published", "ZA", T), ("held", "ZA", T)}
    want = {  # item: (outcome_t7, state_t7, measured, held, collapsed)
        "A": ("held", "peaking", True, True, False),
        "B": ("collapsed", "fading", True, False, True),
        "C": ("collapsed", "absent", True, False, True),
        "D": ("unmeasured", "rising", False, False, False),
        "E": ("held", "mainstream", True, True, False),
        "F": ("held", "recurring", True, True, False),
        "K": ("held", "spike", True, True, False),
        "L": ("listed", "new_to_42", True, False, False),
        "M": ("listed", "on_the_boards", True, False, False),
        "N": ("other", None, True, False, False),
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
    assert all(rows[i]["stratum"] == "trend" for i in ("A", "B", "C", "D", "E", "G", "K", "L", "M", "N", "H1", "H2", "H3", "H4"))


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


def synthetic_rows(kind, market, reason, held, collapsed, other, unmeasured=0, pending=0, day=T, stratum="trend",
                   confirmed=None, backtest=None, trust=None, listed=0):
    """held rows are confirmed (default all of them) or spike; trust and backtest of the confirmed are in those sets."""
    confirmed = held if confirmed is None else confirmed
    backtest = confirmed if backtest is None else backtest
    trust = backtest if trust is None else trust
    out = []
    spec = [("confirmed", confirmed), ("unconfirmed", held - confirmed), ("listed", listed), ("collapsed", collapsed),
            ("other", other), ("unmeasured", unmeasured), ("pending", pending)]
    for cls, count in spec:
        for i in range(count):
            outcome = "held" if cls in ("confirmed", "unconfirmed") else cls
            out.append({"run_date": day, "market": market, "item_id": f"{cls}{i}", "kind": kind,
                        "hold_reason": reason, "stratum": stratum, "class_t7": cls, "outcome_t7": outcome,
                        "eff_state_t7": None, "trust_t7": cls == "confirmed" and i < trust,
                        "backtest_t7": cls == "confirmed" and i < backtest})
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
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|EXPORT|CALL|EXECUTE|LOAD|GRANT|BEGIN|DECLARE|SET)\b", body), name
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
    for col in ("run_date", "market", "item_id", "kind", "hold_reason", "state_t", "state_t7", "outcome_t7", "class_t7",
                "held_trust", "held_any", "stratum", "definition", "scored_at"):
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
    assert sum(mix.values()) == 16


REPO = Path(__file__).resolve().parents[3]
STATE_SQL_TEXT = (REPO / "core" / "detect" / "sql" / "state.sql").read_text(encoding="utf-8")
TRUST_TEXT = (REPO / "docs" / "full-42" / "TRUST.md").read_text(encoding="utf-8")


def product_active28():
    """The product's own active set, read from state.sql."""
    m = re.search(r"LOGICAL_OR\(s\.state IN \(([^)]*)\)\) active28", STATE_SQL_TEXT)
    return set(re.findall(r"'(\w+)'", m.group(1)))


def one_card_outcome(state, lane="panel", base=None):
    states = [st("X", 7, state, lane=lane, base=base)] if state != "absent" else []
    return co.build_outcomes([brief(cards=[card("X", 1)])], states, ALL_DAYS)[0]


# state at t+7 on a measured lane: (class, held, rising or peaking, in the backtest PERSISTING set). Written by hand
# from state.sql active28 (spike, emerging, rising, peaking, mainstream, recurring, seasonal) and TRUST.md section 7.
HAND = {
    "emerging": ("confirmed", True, False, True), "rising": ("confirmed", True, True, True),
    "peaking": ("confirmed", True, True, True), "mainstream": ("confirmed", True, False, False),
    "spike": ("unconfirmed", True, False, False),
    "on_the_boards": ("listed", False, False, False), "new_to_42": ("listed", False, False, False),
    "fading": ("collapsed", False, False, False), "absent": ("collapsed", False, False, False),
}


@pytest.mark.parametrize("state", sorted(HAND))
def test_every_non_overlay_state_has_its_class_trust_and_backtest_flag(state):
    cls, is_held, trust, backtest = HAND[state]
    r = one_card_outcome(state)
    assert r["class_t7"] == cls and r["held"] is is_held and r["held_trust"] is trust and r["held_backtest"] is backtest
    assert r["collapsed"] is (cls == "collapsed") and r["held_any"] is (is_held or cls == "listed")
    other_lane = one_card_outcome(state, lane="search_presence")
    expected = "collapsed" if state == "absent" else "unmeasured"
    assert other_lane["class_t7"] == expected
    if state != "absent":
        assert other_lane["held"] is False and other_lane["held_trust"] is False
        assert other_lane["held_backtest"] is False and other_lane["held_any"] is False


OVERLAYS = {  # (state, base_state): (class, held, rising or peaking, PERSISTING): the overlay is classed by its base
    "rising": ("confirmed", True, True, True), "emerging": ("confirmed", True, False, True),
    "spike": ("unconfirmed", True, False, False), "new_to_42": ("listed", False, False, False),
    None: ("other", False, False, False),
}


@pytest.mark.parametrize("overlay", ["recurring", "seasonal"])
@pytest.mark.parametrize("base", sorted(OVERLAYS, key=str))
def test_recurring_and_seasonal_are_classed_by_their_base_state(overlay, base):
    cls, is_held, trust, backtest = OVERLAYS[base]
    r = one_card_outcome(overlay, base=base)
    assert (r["class_t7"], r["held"], r["held_trust"], r["held_backtest"]) == (cls, is_held, trust, backtest)
    assert r["state_t7"] == overlay


def test_held_is_the_product_active28_set_read_from_state_sql_not_copied():
    active = product_active28()
    assert active == {"spike", "emerging", "rising", "peaking", "mainstream", "recurring", "seasonal"}
    assert set(co.HELD_STATES) == active - set(co.OVERLAY_STATES)
    assert set(co.OVERLAY_STATES) == {"recurring", "seasonal"}
    assert not set(co.LISTED_STATES) & active and set(co.LISTED_STATES) == {"on_the_boards", "new_to_42"}
    for state in sorted(active - set(co.OVERLAY_STATES)):
        assert one_card_outcome(state)["held"] is True, state
    for state in ("on_the_boards", "new_to_42", "fading"):
        assert one_card_outcome(state)["held"] is False, state


def test_trust_column_is_rising_or_peaking_as_trust_md_says():
    assert re.search(r"Rising holds at 7 days \| Still rising or peaking", TRUST_TEXT)
    assert co.TRUST_STATES == ("rising", "peaking")


def test_overlay_base_states_are_the_four_state_sql_keeps():
    seg = STATE_SQL_TEXT.split("the state without the Seasonal and Recurring override", 1)[1].split("END base_state", 1)[0]
    assert set(re.findall(r"THEN '(\w+)'", seg)) == {"rising", "emerging", "spike", "new_to_42"}
    assert set(co.BASE_STATES) == {"rising", "emerging", "spike", "new_to_42"}
    assert STATE_SQL_TEXT.count("WHEN (c.rising OR c.emerging OR c.spike OR c.fresh) AND") == 2


def test_state_vocabulary_is_fully_classified_with_nothing_in_two_classes():
    from core.brief.payload import STATE_WORDS

    groups = [set(co.HELD_STATES), set(co.OVERLAY_STATES), set(co.LISTED_STATES), set(co.COLLAPSED_STATES)]
    assert set().union(*groups) == set(STATE_WORDS)
    assert sum(len(g) for g in groups) == len(set(STATE_WORDS))
    assert set(co.PERSISTING) <= set(co.CONFIRMED_STATES)
    assert co.HELD_STATES == co.CONFIRMED_STATES + co.UNCONFIRMED_STATES


def test_a_search_lane_never_sets_the_backtest_or_trust_flag():
    for state in ("rising", "peaking", "emerging"):
        r = one_card_outcome(state, lane="search_presence")
        assert r["backtest_t7"] is False and r["trust_t7"] is False and r["held_any"] is False


def test_state_distribution_at_t_t3_t7_and_t14_is_exact():
    briefs, states = scenario()
    rows = co.build_outcomes(briefs, states, ALL_DAYS)
    dist = {(d["kind"], d["when"], d["state"]): d["n"] for d in co.state_distribution(rows)}
    assert dist[("published", "t", "rising")] == 6 and dist[("published", "t", "spike")] == 2
    assert dist[("published", "t", "new_to_42")] == 1 and dist[("published", "t", "on_the_boards")] == 1
    assert dist[("published", "t+7", "absent")] == 1 and dist[("published", "t+7", "fading")] == 1
    assert dist[("published", "t+7", "spike")] == 1 and dist[("published", "t+7", "rising")] == 2
    assert dist[("published", "t+3", "rising")] == 3 and dist[("published", "t+3", "absent")] == 9
    assert dist[("held", "t", "emerging")] == 1 and dist[("held", "t+7", "absent")] == 1
    assert dist[("published", "t+7", None)] == 1  # N: a row with no state
    for kind in ("published", "held"):
        for when in ("t", "t+3", "t+7", "t+14"):
            assert sum(n for (k, w, _), n in dist.items() if (k, w) == (kind, when)) == (12 if kind == "published" else 4)


def test_summary_has_product_trust_backtest_and_old_any_columns_with_their_own_wilson_intervals():
    rows = synthetic_rows("published", "ZA", None, held=12, confirmed=8, backtest=6, trust=4, listed=12, collapsed=10,
                          other=6)
    g = group(co.summarize(rows, end=T), "published", "ZA")
    assert (g["n"], g["held"], g["confirmed"], g["unconfirmed"], g["listed"], g["backtest"], g["trust"]) == (
        40, 12, 8, 4, 12, 6, 4)
    assert g["rate"] == pytest.approx(0.3) and g["lo"] == pytest.approx(0.180748, abs=1e-6)
    assert g["hi"] == pytest.approx(0.4543, abs=1e-6)
    assert g["rate_trust"] == pytest.approx(0.1) and g["lo_trust"] == pytest.approx(0.03958, abs=1e-6)
    assert g["hi_trust"] == pytest.approx(0.230518, abs=1e-6)
    assert g["rate_backtest"] == pytest.approx(0.15) and g["lo_backtest"] == pytest.approx(0.070612, abs=1e-6)
    assert g["hi_backtest"] == pytest.approx(0.290723, abs=1e-6)
    assert g["any"] == 24 and g["rate_any"] == pytest.approx(0.6) and g["lo_any"] == pytest.approx(0.445959, abs=1e-6)
    assert g["hi_any"] == pytest.approx(0.736517, abs=1e-6)
    small = group(co.summarize(synthetic_rows("published", "ZA", None, 20, 5, 0, confirmed=3), end=T), "published", "ZA")
    assert small["text"] == small["text_trust"] == small["text_backtest"] == small["text_any"] == "not enough data"
    assert small["rate_trust"] is None and small["lo_backtest"] is None and small["rate_any"] is None


def test_listed_rows_are_decided_and_split_into_on_the_boards_and_new_to_42():
    rows = co.build_outcomes(
        [brief(cards=[card("A", 1), card("B", 2), card("C", 3)])],
        [st("A", 7, "on_the_boards"), st("B", 7, "new_to_42"), st("C", 7, "recurring", base="new_to_42")], ALL_DAYS)
    g = group(co.summarize(rows, end=T, min_n=1), "published", "ZA")
    assert (g["n"], g["held"], g["listed"], g["on_the_boards"], g["new_to_42"], g["any"]) == (3, 0, 3, 1, 2, 3)


def test_markdown_prints_the_state_distribution_before_any_rate():
    briefs, states = scenario()
    rows = co.build_outcomes(briefs, states, ALL_DAYS)
    md = co.report_markdown(rows, {h: co.summarize(rows, end=T, horizon=h, min_n=1) for h in co.HORIZONS}, end=T)
    assert md.index("State at t") < md.index("held at 7 days")
    assert "| on_the_boards |" in md and "| absent |" in md


def test_distribution_labels_a_day_without_a_detect_run_pending_not_no_state():
    days = {T + dt.timedelta(days=i) for i in range(0, 9)}
    rows = co.build_outcomes([brief(cards=[card("P", 1)])], [], days)
    dist = {(d["when"], d["state"]): d["n"] for d in co.state_distribution(rows)}
    assert dist == {("t", "rising"): 1, ("t+3", "absent"): 1, ("t+7", "absent"): 1, ("t+14", "pending"): 1}


def test_states_query_reads_base_state_and_every_table_is_partition_and_stage_pinned():
    q = co.split_queries(SQL.read_text(encoding="utf-8"))
    states, days, briefs = q["states"], q["detect_days"], q["briefs"]
    assert "s.base_state" in states
    assert "s.metric_date BETWEEN @start AND @last" in states
    assert "st.metric_date BETWEEN @start AND @last" in states
    assert "b.brief_date BETWEEN @start AND @end" in states and "b.brief_date BETWEEN @start AND @end" in briefs
    assert "IF(l.lanes = 1, l.lane_class, NULL) main_lane_class" in states
    assert "v_item_state_current" in states and "v_series_test_current" in states
    assert "g.stage = 'detect'" in days and "g.run_date BETWEEN @start AND @last" in days
    assert "v_good_runs" in days
    assert "ROW_NUMBER() OVER (PARTITION BY s.metric_date, s.market, s.item_id ORDER BY s.run_id DESC) = 1" in states


def test_schema_carries_the_definition_and_the_columns_the_module_emits():
    text = SCHEMA.read_text(encoding="utf-8")
    assert co.DEFINITION == "active28_by_base_v3"
    assert co.DEFINITION in text
    for col in ("definition STRING NOT NULL", "class_t3", "class_t14", "held_trust", "held_backtest", "held_any"):
        assert col in text, col
    rows = co.build_outcomes([brief(cards=[card("A", 1)])], [st("A", 7, "rising")], ALL_DAYS)
    assert rows[0]["definition"] == co.DEFINITION


def test_markdown_prints_the_run_dates_actually_present():
    briefs = [brief(day=D(2026, 9, 28), cards=[card("A", 1)]), brief(day=D(2026, 10, 1), cards=[card("B", 1)], run_id="r2")]
    rows = co.build_outcomes(briefs, [], ALL_DAYS)
    md = co.report_markdown(rows, {h: co.summarize(rows, end=D(2026, 10, 1), horizon=h) for h in co.HORIZONS},
                            end=D(2026, 10, 1))
    assert "Run dates present: 2026-09-28 to 2026-10-01 (2 dates)" in md


@pytest.mark.parametrize("base", ["peaking", "mainstream", "fading", "on_the_boards", "recurring", "bogus"])
def test_an_overlay_whose_base_is_not_one_of_the_four_state_sql_keeps_is_other(base):
    r = one_card_outcome("seasonal", base=base)
    assert (r["class_t7"], r["held"], r["held_trust"], r["held_backtest"]) == ("other", False, False, False)


# Measured over every series of the item that day (review finding 3), not the main series alone


def one_card_with_signals(state, lane="panel", signals=None, **kw):
    return co.build_outcomes([brief(cards=[card("X", 1)])], [st("X", 7, state, lane=lane, signals=signals, **kw)],
                             ALL_DAYS)[0]


@pytest.mark.parametrize("signals", [
    ["panel", "search_presence"], ["unbiased_rank", "watchlist"], ["panel", "legacy"], ["unbiased_counter", "seed"],
    ["panel", None], ["panel", "no_such_lane"], ["search_presence"]])
def test_a_significant_or_jumping_series_on_an_unmeasured_lane_makes_the_day_unmeasured_even_with_a_panel_main(signals):
    r = one_card_with_signals("rising", lane="panel", signals=signals)
    assert r["class_t7"] == "unmeasured" and r["measured"] is False and r["held"] is False
    assert r["held_trust"] is False and r["held_backtest"] is False and r["held_any"] is False


@pytest.mark.parametrize("signals", [
    [], ["panel"], ["unbiased_rank"], ["unbiased_counter"], ["unbiased_rank", "unbiased_counter", "panel"]])
def test_signals_only_on_measured_lanes_or_none_leave_a_measured_main_series_measured(signals):
    r = one_card_with_signals("on_the_boards", lane="panel", signals=signals)
    assert r["class_t7"] == "listed" and r["measured"] is True


def test_a_main_series_on_a_search_lane_stays_unmeasured_whatever_the_signals_say():
    r = one_card_with_signals("rising", lane="search_presence", signals=["panel"])
    assert r["class_t7"] == "unmeasured"


def test_a_row_that_does_not_say_which_lanes_carried_the_signal_is_unmeasured_not_measured():
    row = st("X", 7, "rising")
    del row["signal_lanes"]
    r = co.build_outcomes([brief(cards=[card("X", 1)])], [row], ALL_DAYS)[0]
    assert r["class_t7"] == "unmeasured" and r["held"] is False
    row["signal_lanes"] = None
    r = co.build_outcomes([brief(cards=[card("X", 1)])], [row], ALL_DAYS)[0]
    assert r["class_t7"] == "unmeasured"


def test_a_search_lane_signal_on_one_day_does_not_unmeasure_another_day():
    states = [st("X", 3, "rising", signals=["panel", "search_presence"]), st("X", 7, "peaking")]
    r = co.build_outcomes([brief(cards=[card("X", 1)])], states, ALL_DAYS)[0]
    assert (r["class_t3"], r["class_t7"]) == ("unmeasured", "confirmed")


def test_states_query_returns_the_lanes_of_every_significant_or_jumping_series_for_the_item_and_day():
    states = co.split_queries(SQL.read_text(encoding="utf-8"))["states"]
    sig = re.search(r"sig AS \((.*?)\),\s*lane AS", states, flags=re.S).group(1)
    assert "v_series_test_current" in sig and "st.metric_date BETWEEN @start AND @last" in sig
    assert "ARRAY_AGG(DISTINCT IFNULL(st.lane_class, 'unknown')) signal_lanes" in sig
    assert "st.significant OR (" in sig and "IGNORE NULLS" not in sig
    assert "GROUP BY st.market, st.item_id, st.metric_date" in sig
    final = states[states.rindex("SELECT s.metric_date"):]
    assert "IFNULL(sg.signal_lanes, []) signal_lanes" in final
    assert "LEFT JOIN sig sg ON sg.market = s.market AND sg.item_id = s.item_id AND sg.metric_date = s.metric_date" in final


def state_jump_predicate():
    m = re.search(r"LOGICAL_OR\((t\.lane_class != 'unbiased_rank' AND [^)]*?)\) jump_today", STATE_SQL_TEXT)
    return re.sub(r"\bt\.", "st.", m.group(1))


def test_the_jump_predicate_in_the_states_query_is_the_one_state_sql_uses_for_jump_today():
    states = co.split_queries(SQL.read_text(encoding="utf-8"))["states"]
    assert state_jump_predicate() == ("st.lane_class != 'unbiased_rank' AND st.obs_prior >= 5 AND st.y >= 8 "
                                      "AND st.y >= 3 * st.med")
    assert f"({state_jump_predicate()})" in states
