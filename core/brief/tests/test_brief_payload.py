"""The briefs payload for one market (BUILD.md 1.12), in the shape of core/api/contract.md sections 4 and 8."""

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from core.brief.payload import brief_row, build_market_payload

D = "2026-09-30"

# Contract version 1, section 4, less what f42-api computes (tag, dropped), plus the headline object section 8
# puts on each market row, and coverage carrying only the issues the brief itself found (f42-api adds the rest).
MARKET_KEYS = {"market", "label", "status", "headline", "banners", "cards", "more", "held_back", "moments", "boards",
               "coverage", "critic"}
CARD_KEYS = {
    "item_id", "market", "date", "rank", "kind", "title", "state", "state_word", "flag", "flag_word",
    "explained", "explanation", "explanation_claim_ids", "claims", "count_line", "numbers", "sparkline",
    "thumbnails", "evidence_ids", "evidence", "ask", "explanation_status", "failed_reason", "also",
    "market_scope", "market_posts7", "total_posts7", "market_share7", "specificity", "news_driven",
}
HEADLINE_KEYS = {"text", "market", "item_id", "claim_ids"}
HELD_KEYS = {"count", "text", "items"}
HELD_ITEM_KEYS = {"item_id", "title", "rule", "reason", "reason_text", "evidence_ids", "evidence", "numbers",
                  "count_line", "failed_reason"}
MOMENT_KEYS = {"date", "name", "kind", "source", "item_ids"}
SPARK_POINT_KEYS = {"date", "value", "expected_low", "expected_high"}
NUMBER_KEYS = {"value", "unit", "query_id", "run_id", "result_hash"}
EVIDENCE_REQUIRED = {"id", "platform", "handle", "url", "posted_at", "market", "text"}
FLAG_WORD = {
    None: None, "likely_coordinated": "Likely coordinated", "check_pattern": "Check pattern",
    "market_unconfirmed": "Market unconfirmed", "not_assessed": "Not assessed (thin sample)",
    "data_issue": "Data issue",
}
FLAGS = set(FLAG_WORD)
HELD_REASONS = {
    "data_issue", "likely_coordinated", "political_unconfirmed", "paid_led", "not_local",
    "too_few_creators", "not_confirmed", "explanation_failed",
}
STATE_WORDS = {
    "new_to_42": "New to 42", "spike": "Spike", "on_the_boards": "On the boards", "emerging": "Emerging",
    "rising": "Rising", "peaking": "Peaking", "mainstream": "Mainstream", "fading": "Fading",
    "recurring": "Recurring", "seasonal": "Seasonal",
}


def num(value, unit, i=0):
    return {"value": value, "unit": unit, "query_id": f"q_{i}", "run_id": "r_1", "result_hash": f"sha256:{i}"}


def ev(eid, thumb=True):
    e = {
        "id": eid, "platform": "tiktok", "handle": "@a", "url": f"https://x/{eid}",
        "posted_at": "2026-09-29T19:40:00+02:00", "market": "ZA", "text": "caption",
        "engagement": {"views": 10}, "flags": [],
    }
    if thumb:
        e["thumbnail_url"] = f"https://img/{eid}.jpg"
    return e


def claim(cid):
    return {"id": cid, "text": "People post the step.", "label": "observed", "kind": "observation",
            "evidence_ids": ["tt_1"]}


def decision(where="today", publish=None, flag=None, reason=None, rule=None, numbers_only=False):
    if publish is None:
        publish = where != "held_back"
    return {"publish": publish, "where": where, "flag": flag, "reason": reason, "rule": rule,
            "numbers_only": numbers_only}


def spark():
    start = date(2026, 9, 24)
    pts = [{"date": (start + timedelta(days=i)).isoformat(), "value": None if i == 1 else i + 2,
            "expected_low": 0.0, "expected_high": 6.0} for i in range(7)]
    return {"unit": "posts a day", "points": pts}


def cand(n, worth=None, untested=False, state="rising", **kw):
    c = {
        "item_id": f"it_{n}", "kind": "hashtag", "state": state, "state_raw": state, "untested": untested,
        "main_y": 12, "main_mu": 4.2, "main_ratio": 2.8, "creators3": 31, "posts3": 40,
        "top_creator_share3": 0.1, "authenticity": "clear", "geo_status": "local", "worth_raw": worth if worth
        is not None else 1 - n / 100, "title": f"#tag{n}", "decision": decision(),
        "market_scope": "market", "market_posts7": 6, "total_posts7": 8, "market_share7": 0.75,
        "explanation": f"Explanation {n}.",
        "claims": [claim("c1"), claim("c2")], "explanation_claim_ids": ["c1", "c2"],
        "numbers": [num(31, "creators in 3 days", 1), num(2.8, "times usual", 2)],
        "sparkline": spark(),
        "evidence": [ev("tt_1", thumb=False), ev("tt_2"), ev("ig_3"), ev("yt_4")],
    }
    c.update(kw)
    return c


def build(cands, **kw):
    args = {"moments": [], "boards": [], "banners": []}
    args.update(kw)
    return build_market_payload("ZA", D, cands, **args)


# The full shape


def test_full_payload_has_every_contract_field_for_market_and_card():
    cands = [cand(i) for i in range(1, 13)]
    cands.append(cand(20, decision=decision("held_back", rule="G5", flag="Paid-led", reason="Paid-led")))
    cands.append(cand(21, state="seasonal", decision=decision("moments", rule="G8")))
    boards = [{"platform": "tiktok", "list": "Hashtag board, 7 days",
               "entries": [{"rank": 1, "title": "#tag1", "item_id": "it_1"}]}]
    moments = [{"date": "2026-10-01", "name": "Heritage Day", "kind": "holiday", "source": "calendar",
                "item_ids": []}]
    banners = [{"kind": "warming_up", "text": "Warming up: day 2 of 14"}]
    headline = {"text": "One sentence.", "market": "ZA", "item_id": "it_1", "claim_ids": ["c1"]}
    p = build(cands, moments=moments, boards=boards, banners=banners, headline=headline)
    assert set(p["headline"]) == HEADLINE_KEYS

    assert set(p) == MARKET_KEYS
    assert (p["market"], p["label"], p["status"]) == ("ZA", "South Africa", "published")
    assert p["banners"] == banners and p["boards"] == boards
    assert p["headline"] == headline
    assert len(p["cards"]) == 5 and len(p["more"]) == 5
    assert set(p["held_back"]) == HELD_KEYS
    for item in p["held_back"]["items"]:
        assert set(item) == HELD_ITEM_KEYS
        assert item["reason"] in HELD_REASONS
    for m in p["moments"]:
        assert set(m) == MOMENT_KEYS
        assert m["kind"] in {"holiday", "festival", "fixture", "release", "seasonal_item"}
    for c in p["cards"] + p["more"]:
        assert set(c) == CARD_KEYS
        assert (c["market"], c["date"], c["kind"]) == ("ZA", D, "hashtag")
        assert isinstance(c["rank"], int)
        assert c["state"] in STATE_WORDS and c["state_word"] == STATE_WORDS[c["state"]]
        assert c["flag"] in FLAGS
        assert isinstance(c["explained"], bool)
        assert set(c["sparkline"]) == {"unit", "points"}
        for pt in c["sparkline"]["points"]:
            assert set(pt) == SPARK_POINT_KEYS
        for n in c["numbers"]:
            assert set(n) == NUMBER_KEYS
        for e in c["evidence"]:
            assert EVIDENCE_REQUIRED <= set(e)
        assert set(c["thumbnails"]) <= set(c["evidence_ids"])
        assert c["evidence_ids"] == [e["id"] for e in c["evidence"]]
        assert set(c["explanation_claim_ids"]) <= {cl["id"] for cl in c["claims"]}
        assert isinstance(c["count_line"], str) and isinstance(c["ask"], str)


def test_card_values_match_the_contract_example():
    c = build([cand(1, title="#example")])["cards"][0]
    assert c["rank"] == 1
    assert c["title"] == "#example"
    assert c["state_word"] == "Rising"
    assert (c["flag"], c["flag_word"]) == (None, None)
    assert (c["market_scope"], c["market_posts7"], c["total_posts7"], c["market_share7"]) == (
        "market", 6, 8, 0.75)
    assert c["explained"] is True
    assert c["explanation"] == "Explanation 1."
    assert c["explanation_claim_ids"] == ["c1", "c2"]
    assert c["count_line"] == "31 creators in 3 days, 2.8 times usual"
    assert c["thumbnails"] == ["tt_2", "ig_3"]
    assert c["evidence_ids"] == ["tt_1", "tt_2", "ig_3", "yt_4"]
    assert c["ask"] == "What is behind #example in South Africa this week?"
    assert c["sparkline"]["points"][1]["value"] is None
    assert c["sparkline"]["points"][0]["expected_high"] == 6.0


def test_market_labels_and_date_objects():
    p = build_market_payload("KE", date(2026, 9, 30), [cand(1)], moments=[], boards=[], banners=[])
    assert p["label"] == "Kenya"
    assert p["cards"][0]["date"] == D
    assert p["cards"][0]["ask"].endswith("in Kenya this week?")
    assert build_market_payload("NG", D, [], moments=[], boards=[], banners=[])["label"] == "Nigeria"


# Ranking


def test_rank_by_worth_raw_among_today_only_five_cards_then_more():
    cands = [cand(i, worth=w) for i, w in enumerate([0.1, 0.9, 0.5, 0.7, 0.3, 0.8, 0.2, 0.6, 0.4, 0.05, 0.01], 1)]
    cands.append(cand(99, worth=5.0, decision=decision("held_back", rule="G6", reason="Not in this market")))
    p = build(cands)
    assert [c["item_id"] for c in p["cards"]] == ["it_2", "it_6", "it_4", "it_8", "it_3"]
    assert [c["rank"] for c in p["cards"]] == [1, 2, 3, 4, 5]
    assert [c["item_id"] for c in p["more"]] == ["it_9", "it_5", "it_7", "it_1", "it_10"]
    assert [c["rank"] for c in p["more"]] == [6, 7, 8, 9, 10]
    shown = {c["item_id"] for c in p["cards"] + p["more"]}
    assert "it_99" not in shown and "it_11" not in shown


def test_fewer_than_five_gives_empty_more():
    p = build([cand(1), cand(2)])
    assert len(p["cards"]) == 2 and p["more"] == []


# Held back


@pytest.mark.parametrize("rule,reason", [
    ("G1", "data_issue"), ("G4", "likely_coordinated"), ("G4b", "political_unconfirmed"),
    ("G5", "paid_led"), ("G5b", "paid_led"), ("G6", "not_local"), ("G3", "not_confirmed"),
    ("G10", "explanation_failed"),
])
def test_gate_rules_map_to_contract_reason_codes(rule, reason):
    p = build([cand(1, decision=decision("held_back", rule=rule, reason="Plain words from the gate"))])
    item = p["held_back"]["items"][0]
    assert (item["item_id"], item["title"], item["rule"], item["reason"]) == ("it_1", "#tag1", rule, reason)
    assert item["reason_text"] == "Plain words from the gate"
    assert item["evidence_ids"] == ["tt_1", "tt_2", "ig_3", "yt_4"]
    assert item["evidence"][0]["id"] == "tt_1"
    assert p["cards"] == []


def test_missing_floors_is_too_few_creators():
    p = build([cand(1, creators3=3, posts3=5, decision=decision("held_back", rule=None, reason=None))])
    item = p["held_back"]["items"][0]
    assert item["reason"] == "too_few_creators"
    assert item["reason_text"] == "Too few creators"


def test_held_back_without_rule_but_floors_met_is_not_confirmed():
    p = build([cand(1, decision=decision("held_back", rule=None, reason=None))])
    assert p["held_back"]["items"][0]["reason"] == "not_confirmed"


def test_held_back_count_and_text():
    held = decision("held_back", rule=None)
    p = build([cand(i, creators3=2, decision=held) for i in range(3)] + [cand(9)])
    assert p["held_back"]["count"] == 3
    assert p["held_back"]["text"] == "3 held back: too few creators"


def test_held_back_text_with_mixed_reasons_counts_each():
    held = decision("held_back", rule=None)
    cands = [cand(i, creators3=2, decision=held) for i in range(2)]
    cands.append(cand(5, decision=decision("held_back", rule="G5", reason="Paid-led")))
    assert build(cands)["held_back"]["text"] == "3 held back: 2 too few creators, 1 paid-led"


def test_nothing_held_back():
    hb = build([cand(1)])["held_back"]
    assert hb == {"count": 0, "text": "Nothing held back", "items": []}


# Moments


def test_moments_decisions_become_seasonal_items_after_calendar_moments():
    cal = [{"date": "2026-10-01", "name": "Heritage Day", "kind": "holiday", "source": "calendar", "item_ids": []}]
    s = cand(4, state="seasonal", title="#heritageday", decision=decision("moments", rule="G8"))
    p = build([cand(1), s], moments=cal)
    assert p["moments"][0] == cal[0]
    assert p["moments"][1] == {"date": D, "name": "#heritageday", "kind": "seasonal_item", "source": "detect",
                               "item_ids": ["it_4"]}
    assert [c["item_id"] for c in p["cards"]] == ["it_1"]


# Flags and states


@pytest.mark.parametrize("given,code,word", [
    ("Market unconfirmed", "market_unconfirmed", "Market unconfirmed"),
    ("market_unconfirmed", "market_unconfirmed", "Market unconfirmed"),
    ("Check pattern", "check_pattern", "Check pattern"),
    ("likely_coordinated", "likely_coordinated", "Likely coordinated"),
    ("Not assessed", "not_assessed", "Not assessed (thin sample)"),
    ("Data issue", "data_issue", "Data issue"),
    ("clear", None, None),
    (None, None, None),
])
def test_flag_code_and_word(given, code, word):
    c = build([cand(1, decision=decision(flag=given))])["cards"][0]
    assert (c["flag"], c["flag_word"]) == (code, word)


@pytest.mark.parametrize("state,word", list(STATE_WORDS.items()))
def test_state_words(state, word):
    c = build([cand(1, state=state)])["cards"][0]
    assert (c["state"], c["state_word"]) == (state, word)


# Numbers only (G10)


def test_numbers_only_card_is_not_explained_and_market_is_partial():
    p = build([cand(1, decision=decision(numbers_only=True, rule="G10")), cand(2)])
    c = p["cards"][0]
    assert c["explained"] is False
    assert c["explanation"] is None and c["claims"] == [] and c["explanation_claim_ids"] == []
    assert c["numbers"] and c["evidence"] and c["count_line"]
    assert p["status"] == "partial"


def test_missing_explanation_counts_as_explain_failure():
    p = build([cand(1, explanation=None)])
    assert p["cards"][0]["explained"] is False and p["cards"][0]["claims"] == []
    assert p["status"] == "partial"


def test_numbers_only_card_in_more_still_makes_partial():
    cands = [cand(i) for i in range(1, 6)] + [cand(6, worth=0.0, decision=decision(numbers_only=True))]
    p = build(cands)
    assert p["more"][0]["explained"] is False
    assert p["status"] == "partial"


def test_explanation_failed_hold_makes_partial():
    p = build([cand(1), cand(2, decision=decision("held_back", rule="G10"))])
    assert p["status"] == "partial"


# Count line and warm-up


def test_count_line_uses_only_numbers_values():
    c = build([cand(1, creators3=999, main_ratio=9.9, numbers=[num(12, "creators in 3 days"),
                                                             num(1840, "posts in 3 days", 1)])])["cards"][0]
    assert c["count_line"] == "12 creators and 1,840 posts in 3 days"
    assert "999" not in c["count_line"] and "9.9" not in c["count_line"]


def test_count_line_empty_without_numbers():
    assert build([cand(1, numbers=[])])["cards"][0]["count_line"] == ""


def test_untested_card_has_no_growth_ratio_and_no_expected_band():
    c = build([cand(1, state="new_to_42", untested=True)])["cards"][0]
    assert c["count_line"] == "31 creators in 3 days"
    assert all("times" not in n["unit"] for n in c["numbers"])
    for pt in c["sparkline"]["points"]:
        assert pt["expected_low"] is None and pt["expected_high"] is None
    assert c["sparkline"]["points"][2]["value"] == 4


def test_tested_card_keeps_growth_and_band():
    c = build([cand(1, untested=False)])["cards"][0]
    assert "2.8 times usual" in c["count_line"]
    assert c["sparkline"]["points"][0]["expected_low"] == 0.0


def test_sparkline_without_band_fields_gives_nulls():
    sp = {"unit": "posts a day", "points": [{"date": "2026-09-29", "value": 3}]}
    c = build([cand(1, sparkline=sp)])["cards"][0]
    assert c["sparkline"]["points"] == [{"date": "2026-09-29", "value": 3, "expected_low": None,
                                         "expected_high": None}]


def test_thumbnails_at_most_two_and_only_with_thumbnail_url():
    c = build([cand(1, evidence=[ev("a", False), ev("b"), ev("c", False)])])["cards"][0]
    assert c["thumbnails"] == ["b"]


# Status and headline


def test_data_issue_banner_gives_data_issue_status():
    p = build([cand(1, decision=decision(numbers_only=True))],
              banners=[{"kind": "data_issue", "text": "Detection failed"}])
    assert p["status"] == "data_issue"


def test_empty_market_is_published_with_empty_lists():
    p = build([])
    assert p["status"] == "published" and p["cards"] == [] and p["more"] == [] and p["headline"] is None


def test_headline_from_unexplained_card_is_refused_and_market_partial():
    head = {"text": "Big news.", "item_id": "it_1", "claim_ids": ["c1"]}
    p = build([cand(1, decision=decision(numbers_only=True))], headline=head)
    assert p["headline"] is None and p["status"] == "partial"


def test_headline_citing_a_claim_not_on_the_card_is_refused():
    head = {"text": "Big news.", "item_id": "it_1", "claim_ids": ["c9"]}
    p = build([cand(1)], headline=head)
    assert p["headline"] is None and p["status"] == "partial"


def test_headline_naming_an_item_not_shown_is_refused():
    head = {"text": "Big news.", "item_id": "it_7", "claim_ids": ["c1"]}
    assert build([cand(1)], headline=head)["headline"] is None


def test_headline_is_the_contract_object_with_the_market_filled_in():
    head = {"text": "Big news.", "item_id": "it_1", "claim_ids": ["c2"]}
    p = build([cand(1)], headline=head)
    assert p["headline"] == {"text": "Big news.", "market": "ZA", "item_id": "it_1", "claim_ids": ["c2"]}
    assert p["status"] == "published"


def test_headline_may_cite_any_surviving_claim_on_the_card():
    head = {"text": "Big news.", "market": "ZA", "item_id": "it_1", "claim_ids": ["c2"]}
    p = build([cand(1, explanation_claim_ids=["c1"])], headline=head)
    assert p["headline"]["claim_ids"] == ["c2"]


def test_headline_for_another_market_is_refused():
    head = {"text": "Big news.", "market": "NG", "item_id": "it_1", "claim_ids": ["c1"]}
    p = build([cand(1)], headline=head)
    assert p["headline"] is None and p["status"] == "partial"


def test_headline_with_no_claim_ids_is_refused():
    head = {"text": "Big news.", "market": "ZA", "item_id": "it_1", "claim_ids": []}
    assert build([cand(1)], headline=head)["headline"] is None


# explanation_claim_ids


def test_explanation_claim_ids_come_from_the_candidate():
    c = build([cand(1, explanation_claim_ids=["c2"])])["cards"][0]
    assert c["explained"] is True
    assert c["explanation_claim_ids"] == ["c2"]
    assert [cl["id"] for cl in c["claims"]] == ["c1", "c2"]


@pytest.mark.parametrize("ids", [["c1", "c9"], [], None])
def test_explanation_claim_ids_not_on_the_card_make_it_unexplained(ids):
    p = build([cand(1, explanation_claim_ids=ids)])
    c = p["cards"][0]
    assert c["explained"] is False
    assert c["explanation"] is None and c["claims"] == [] and c["explanation_claim_ids"] == []
    assert p["status"] == "partial"


# The item_state flag when the gate gives none


@pytest.mark.parametrize("auth,geo,code", [
    ("check_pattern", "local", "check_pattern"),
    ("not_assessed", "local", "not_assessed"),
    ("clear", "local", None),
    ("likely_coordinated", "local", None),
    ("clear", "market_unconfirmed", "market_unconfirmed"),
    ("check_pattern", "market_unconfirmed", "market_unconfirmed"),
    ("not_assessed", "market_unconfirmed", "market_unconfirmed"),
])
def test_card_flag_from_item_state(auth, geo, code):
    c = build([cand(1, authenticity=auth, geo_status=geo)])["cards"][0]
    assert (c["flag"], c["flag_word"]) == (code, FLAG_WORD[code])


def test_gate_flag_wins_over_item_state():
    c = build([cand(1, authenticity="check_pattern", geo_status="market_unconfirmed",
                    decision=decision(flag="Data issue"))])["cards"][0]
    assert (c["flag"], c["flag_word"]) == ("data_issue", "Data issue")


# The gate's Decision dataclass


@dataclass(frozen=True)
class Decision:
    """Mirror of core/trust/gate.py Decision, which this test does not import."""
    publish: bool
    where: str
    flag: str | None
    reason: str | None
    rule: str | None
    numbers_only: bool


def test_decision_dataclass_is_read_by_attribute():
    today = Decision(True, "today", "Market unconfirmed", "Market unconfirmed: 3 posts", "G6", True)
    held = Decision(False, "held_back", "Paid-led", "Paid-led: share 0.62", "G5", False)
    season = Decision(True, "moments", None, "Seasonal: Heritage Day", "G8", False)
    p = build([cand(1, decision=today), cand(2, decision=held), cand(3, state="seasonal", decision=season)])
    c = p["cards"][0]
    assert (c["item_id"], c["flag"], c["explained"]) == ("it_1", "market_unconfirmed", False)
    assert p["held_back"]["items"][0]["reason"] == "paid_led"
    assert p["held_back"]["items"][0]["reason_text"] == "Paid-led: share 0.62"
    assert p["moments"][0]["item_ids"] == ["it_3"]
    assert p["status"] == "partial"


# The briefs row


def test_brief_row_has_the_briefs_columns():
    p = build([cand(1, decision=decision(numbers_only=True))])
    at = datetime(2026, 9, 30, 6, 14, 40, tzinfo=timezone(timedelta(hours=2)))
    row = brief_row("ZA", date(2026, 9, 30), "r_1", at, p, "rv_2026_09")
    assert set(row) == {"brief_date", "market", "run_id", "published_at", "status", "payload", "rule_version"}
    assert row["brief_date"] == D
    assert row["published_at"] == "2026-09-30T06:14:40+02:00"
    assert (row["market"], row["run_id"], row["rule_version"]) == ("ZA", "r_1", "rv_2026_09")
    assert row["status"] == "partial"
    assert row["payload"] is p


def test_brief_row_accepts_strings():
    p = build([cand(1)])
    row = brief_row("ZA", D, "r_1", "2026-09-30T06:14:40+02:00", p, "v1")
    assert (row["brief_date"], row["published_at"], row["status"]) == (D, "2026-09-30T06:14:40+02:00", "published")


def test_count_line_names_a_shared_window_once():
    c = build([cand(1, numbers=[num(10, "creators in 3 days"), num(10, "posts in 3 days", 1)])])["cards"][0]
    assert c["count_line"] == "10 creators and 10 posts in 3 days"
    c = build([cand(1, numbers=[num(10, "creators in 3 days"), num(20, "posts in 3 days", 1),
                                num(2.5, "times usual", 2)])])["cards"][0]
    assert c["count_line"] == "10 creators and 20 posts in 3 days, 2.5 times usual"


def test_explanation_status_is_explained_or_what_the_job_says_else_not_run():
    g10 = decision(numbers_only=True)
    p = build([cand(1), cand(2, explanation_status="failed_checks", decision=g10),
               cand(3, explanation_status="not_run", decision=g10), cand(4, decision=g10)])
    assert [c["explanation_status"] for c in p["cards"]] == ["explained", "failed_checks", "not_run", "not_run"]
    assert [c["explained"] for c in p["cards"]] == [True, False, False, False]


def test_coverage_issues_pass_through_and_default_to_none():
    assert build([cand(1)])["coverage"] == {"issues": []}
    assert build([cand(1)], issues=["2 board entries without a name"])["coverage"] == {
        "issues": ["2 board entries without a name"]}


def test_held_back_text_groups_by_what_held_each_item():
    generic = decision(where="held_back", reason="Platform-generic tag", rule="G2")
    thin = decision(where="held_back", reason="Fewer than 3 posts 42 can show")
    cands = ([cand(i, decision=generic, held_reason="not_confirmed") for i in range(1, 5)]
             + [cand(i, decision=thin, held_reason="not_confirmed") for i in range(5, 7)])
    assert build(cands)["held_back"]["text"] == "6 held back: 4 platform-generic tags, 2 with fewer than 3 posts"
    assert build(cands[:4])["held_back"]["text"] == "4 held back: platform-generic tags"
    assert build(cands[:1])["held_back"]["text"] == "1 held back: platform-generic tag"


def test_failed_reason_is_on_failed_checks_cards_only():
    g10 = decision(numbers_only=True)
    why = "Critic: a simpler explanation was not ruled out: scraping artefact"
    p = build([cand(1, failed_reason=why), cand(2, explanation_status="failed_checks", decision=g10,
                                                failed_reason=why),
               cand(3, explanation_status="not_run", decision=g10, failed_reason=why)])
    assert [c["failed_reason"] for c in p["cards"]] == [None, why, None]



# Items merged into a card, and posts a higher card already shows


def ids(card):
    return [e["id"] for e in card["evidence"]]


def test_a_card_lists_the_items_merged_into_it_and_counts_only_its_own_item():
    also = [{"item_id": "it_7", "title": "#haaland"}, {"item_id": "it_8", "title": "#parody"}]
    p = build([cand(1, title="#football", also=also), cand(2)])
    football, other = p["cards"]
    assert football["also"] == also
    assert football["count_line"] == "31 creators in 3 days, 2.8 times usual (#football only)"
    assert football["numbers"] == cand(1)["numbers"]
    assert other["also"] == [] and other["count_line"] == "31 creators in 3 days, 2.8 times usual"


def test_a_merged_card_without_numbers_keeps_an_empty_count_line():
    c = build([cand(1, numbers=[], also=[{"item_id": "it_7", "title": "#x"}])])["cards"][0]
    assert c["count_line"] == ""


def test_the_first_posts_on_a_card_skip_posts_a_higher_card_shows_when_others_are_there():
    top = cand(1, evidence=[ev("a"), ev("b"), ev("c"), ev("d")])
    second = cand(2, evidence=[ev("a"), ev("b"), ev("e"), ev("f"), ev("g")])
    third = cand(3, evidence=[ev("e"), ev("h"), ev("a")])
    first, two, three = build([top, second, third])["cards"]
    assert ids(first) == ["a", "b", "c", "d"]
    assert ids(two) == ["e", "f", "g", "a", "b"]
    assert two["thumbnails"] == ["e", "f"] and two["evidence_ids"] == ids(two)
    assert ids(three) == ["h", "e", "a"]
    assert three["thumbnails"] == ["h", "e"]


def test_specificity_examples_lead_the_card_evidence_and_thumbnail_order():
    evidence = [ev("unknown"), ev("foreign"), ev("local_1"), ev("local_2")]
    evidence[0]["market"] = None
    evidence[1]["market"] = "NG"
    specificity = {"status": "pass", "local_evidence_ids": ["local_2", "local_1"],
                   "quote": {"evidence_id": "local_2", "text": "Exact local quote"},
                   "why_now": "The local posts show a current use.", "reason": None}

    card = build([cand(1, evidence=evidence, specificity=specificity)])['cards'][0]

    assert card["specificity"] == specificity
    assert ids(card) == ["local_2", "local_1", "unknown", "foreign"]
    assert card["thumbnails"] == ["local_2", "local_1"]


def test_specificity_priority_keeps_posts_shown_above_at_the_end():
    top = cand(1, evidence=[ev("shared"), ev("top_only")])
    evidence = [ev("shared"), ev("foreign"), ev("local_1"), ev("local_2")]
    specificity = {"status": "pass", "local_evidence_ids": ["local_2", "shared"],
                   "quote": {"evidence_id": "local_2", "text": "Exact local quote"},
                   "why_now": "The local posts show a current use.", "reason": None}
    second = cand(2, evidence=evidence, specificity=specificity)

    first_card, second_card = build([top, second])["cards"]

    assert ids(first_card) == ["shared", "top_only"]
    assert ids(second_card) == ["local_2", "foreign", "local_1", "shared"]


def test_a_card_whose_posts_were_all_shown_higher_keeps_them():
    p = build([cand(1, evidence=[ev("a"), ev("b"), ev("c")]), cand(2, evidence=[ev("c"), ev("b"), ev("a")])])
    assert ids(p["cards"][1]) == ["c", "b", "a"]


def test_a_thumbnail_past_the_first_posts_counts_as_shown():
    top = cand(1, evidence=[ev("a", False), ev("b", False), ev("c", False), ev("d")])
    second = cand(2, evidence=[ev("d"), ev("x"), ev("y"), ev("z")])
    first, two = build([top, second])["cards"]
    assert first["thumbnails"] == ["d"]
    assert ids(two) == ["x", "y", "z", "d"]


def test_held_items_do_not_count_as_shown():
    held = cand(1, worth=5.0, evidence=[ev("a"), ev("b"), ev("c")],
                decision=decision("held_back", rule="G6", reason="Not in this market"))
    p = build([held, cand(2, evidence=[ev("a"), ev("b"), ev("x")])])
    assert ids(p["cards"][0]) == ["a", "b", "x"]


def test_held_item_carries_its_numbers_and_count_line():
    item = build([cand(1, decision=decision("held_back", rule="G10"))])["held_back"]["items"][0]
    assert item["numbers"] == [num(31, "creators in 3 days", 1), num(2.8, "times usual", 2)]
    assert item["count_line"] == "31 creators in 3 days, 2.8 times usual"


def test_held_item_drops_growth_numbers_when_untested():
    item = build([cand(1, untested=True, decision=decision("held_back", rule="G10"))])["held_back"]["items"][0]
    assert item["numbers"] == [num(31, "creators in 3 days", 1)]
    assert item["count_line"] == "31 creators in 3 days"


def test_held_item_failed_reason_only_after_failed_checks():
    g10 = decision("held_back", rule="G10")
    failed = cand(1, explanation_status="failed_checks", failed_reason="Support check: a claim was not supported",
                  decision=g10)
    thin = cand(2, failed_reason="Support check: a claim was not supported", decision=g10)
    items = build([failed, thin])["held_back"]["items"]
    assert [i["failed_reason"] for i in items] == ["Support check: a claim was not supported", None]


def test_a_news_driven_card_says_so_and_others_do_not():
    p = build([cand(1, news_driven=True), cand(2)])
    assert [c["news_driven"] for c in p["cards"]] == [True, False]


def test_news_driven_is_dropped_when_the_card_is_not_explained():
    p = build([cand(1, news_driven=True, explanation=None)])
    assert p["cards"][0]["explained"] is False and p["cards"][0]["news_driven"] is False


def test_the_critics_answers_sit_apart_from_cards_and_held_items():
    shown = {"non_cultural_explanation": "a paid campaign", "ruled_out": True, "news_driven": False,
             "local_reaction": False, "local_why_now": True, "reason": "unrelated creators in their own words"}
    held_answer = dict(shown, ruled_out=False, reason="all posts on one day")
    p = build([cand(1, critic=shown),
               cand(2, critic=held_answer, explanation_status="failed_checks",
                    decision=decision("held_back", rule="G10")),
               cand(3)])
    assert p["critic"] == [{"item_id": "it_1", **shown}, {"item_id": "it_2", **held_answer}]
    assert all("critic" not in c for c in p["cards"] + p["more"])
    assert all("critic" not in i for i in p["held_back"]["items"])


def test_the_critics_answers_are_empty_when_no_critic_ran():
    assert build([cand(1), cand(2, decision=decision("held_back", rule="G5"))])["critic"] == []
    assert build([])["critic"] == []


def test_a_suppressed_handle_in_the_critics_answer_is_masked_as_the_job_masks_the_payload():
    from core.api.today import without_hidden

    answer = {"non_cultural_explanation": "one creator, @Kay_Dance, carries the count", "ruled_out": False,
              "news_driven": False, "local_reaction": False, "local_why_now": False,
              "reason": "most posts are by @kay_dance"}
    p = without_hidden(build([cand(1, critic=answer)]), ({"tiktok:kay_dance"}, set(), set()))
    assert "kay_dance" not in json.dumps(p["critic"]).lower()
