"""Value readiness and the brief's outcome per market (W01-01, W26-05): a morning can publish a brief and pass the
operational status with no credible card. today_value says so on its own signal, keeps the brief status as it is and
keeps every hold reason, and Coverage carries it with the zero-card history."""

import copy
import json

import pytest

from core.api import coverage, today
from core.api.store import FixtureStore
from core.api.tests.test_today import _fixture_critics, _passing_specificity_card

pytestmark = pytest.mark.usefixtures("old_model_cap_schedule")

D30 = "2026-09-30"


def held(item_id, reason, rule):
    return {"item_id": item_id, "title": f"#{item_id}", "reason": reason, "rule": rule,
            "reason_text": "Held back", "evidence": [], "evidence_ids": []}


def not_assessed(*items, judged=10, pool=90):
    return {"detect_run_id": "r_detect_x", "pool_limit": pool, "judged_limit": judged, "count": len(items),
            "items": [{"item_id": i, "title": f"#{i}", "status": "not_assessed", "reason": reason, "sql_rank": rank,
                       "pool_rank": rank if reason != "outside_candidate_pool" else None, "market_scope": "market"}
                      for i, reason, rank in items]}


class Store(FixtureStore):
    """The fixtures with the brief rows for some dates replaced: {date: {market: (status, payload)}}."""

    def __init__(self, days):
        super().__init__()
        self.days = days

    def _rows(self):
        base = [r for r in self._load("briefs") if r["brief_date"] not in self.days]
        for date, markets in self.days.items():
            for m, (status, payload) in markets.items():
                base.append({"brief_date": date, "market": m, "run_id": f"r_brief_{date}", "status": status,
                             "published_at": f"{date}T06:10:00+02:00", "payload": payload,
                             "rule_version": "r1"})
        return base

    def briefs(self, date):
        return [r for r in self._rows() if r["brief_date"] == date]

    def brief_history(self, limit, before, market=None):
        rows = [r for r in self._rows() if (before is None or r["brief_date"] < before)
                and (market is None or r["market"] == market)]
        dates = sorted({r["brief_date"] for r in rows}, reverse=True)[:limit]
        return [{k: r.get(k) for k in ("brief_date", "market", "run_id", "status", "published_at", "payload")}
                for r in rows if r["brief_date"] in dates]


def empty(market, **extra):
    return {"market": market, "status": "published", "banners": [], "cards": [], "more": [],
            "held_back": {"count": 0, "items": []}, "moments": [], "boards": [], **extra}


def all_held(market):
    items = [held(f"{market.lower()}{i}", "explanation_failed", "G10") for i in range(1, 4)]
    items.append(held(f"{market.lower()}9", "data_issue", "G1"))
    return empty(market, held_back={"count": len(items), "items": items})


def credible(market="ZA"):
    """The ZA fixture brief with one card Today admits."""
    card = _passing_specificity_card()
    payload = next(r for r in FixtureStore().briefs(D30) if r["market"] == "ZA")["payload"]
    return dict(copy.deepcopy(payload), market=market, cards=[card], more=[], critic=_fixture_critics([card]),
                headline=None)


def zero_card_day():
    return {m: ("published", all_held(m)) for m in today.MARKETS}


def test_a_published_brief_with_zero_credible_cards_fails_value_readiness_and_keeps_every_hold_reason():
    s = Store({D30: zero_card_day()})
    value = today.today_value(s, D30)
    assert today.today_status(s, D30) == "published"  # the brief status stays honest
    assert value["brief_status"] == "published"
    assert value["status"] == "no_credible_cards" and value["value_ready"] is False
    assert value["cards"] == 0
    for m in value["markets"]:
        assert m["outcome"] == "all_held" and m["cards"] == 0 and m["held"] == 4
        assert m["held_by_reason"] == {"explanation_failed": 3, "data_issue": 1}
        assert m["held_by_rule"] == {"G10": 3, "G1": 1}


def test_one_credible_card_passes_value_readiness_and_is_reported_without_a_yield_threshold():
    days = zero_card_day()
    days["NG"] = ("published", credible("NG"))
    value = today.today_value(Store({D30: days}), D30)
    assert value["status"] == "cards" and value["value_ready"] is True and value["cards"] == 1
    by_market = {m["market"]: m for m in value["markets"]}
    assert by_market["NG"]["outcome"] == "cards" and by_market["NG"]["cards"] == 1
    assert by_market["NG"]["held_by_reason"] == {"too_few_creators": 1, "data_issue": 1}  # the fixture's own holds
    assert by_market["ZA"]["outcome"] == "all_held" and by_market["ZA"]["held_by_reason"]["explanation_failed"] == 3


def test_no_brief_is_not_ready():
    value = today.today_value(Store({}), "2026-09-28")
    assert value["status"] == "not_ready" and value["value_ready"] is False and value["brief_status"] == "not_ready"
    assert {m["outcome"] for m in value["markets"]} == {"no_brief"}


def test_the_outcomes_tell_no_candidate_all_held_not_assessed_data_issue_and_a_published_card_apart():
    cases = {
        "no_candidates": ("published", empty("ZA")),
        "all_held": ("published", all_held("ZA")),
        "not_assessed": ("published", empty("ZA", not_assessed=not_assessed(
            ("za20", "judged_limit_reached", 11), ("za99", "outside_candidate_pool", 120)))),
        "data_issue": ("data_issue", empty("ZA", status="data_issue",
                                           banners=[{"kind": "data_issue", "text": "Data issue"}])),
        "cards": ("published", credible()),
    }
    for expected, row in cases.items():
        [za] = [m for m in today.today_value(Store({D30: {"ZA": row}}), D30)["markets"] if m["market"] == "ZA"]
        assert za["outcome"] == expected, (expected, za)
        assert za["text"]
    [za] = [m for m in today.today_value(Store({D30: {"ZA": cases["not_assessed"]}}), D30)["markets"]
            if m["market"] == "ZA"]
    assert za["not_assessed"] == 2 and za["judged_limit"] == 10
    assert za["not_assessed_by_reason"] == {"judged_limit_reached": 1, "outside_candidate_pool": 1}


def test_a_card_today_does_not_admit_counts_as_held_not_as_a_credible_card():
    broken = _passing_specificity_card()
    broken["explained"] = False
    s = Store({D30: {"NG": ("published", empty("NG", cards=[broken], critic=_fixture_critics([broken])))}})
    [ng] = [m for m in today.today_value(s, D30)["markets"] if m["market"] == "NG"]
    assert ng["cards"] == 0 and ng["outcome"] == "all_held"
    assert ng["held_by_reason"] == {today.TODAY_REJECTED: 1}


def test_one_zero_card_day_is_information_and_a_run_of_them_asks_for_a_review():
    one = today.today_value(Store({"2026-09-29": {m: ("published", empty(m)) for m in ("ZA", "NG")},
                                   D30: zero_card_day()}), D30)
    assert [d["date"] for d in one["history"]["days"]][:2] == [D30, "2026-09-29"]
    assert one["history"]["zero_card_days"] == 2
    assert one["history"]["action"]
    single = today.today_value(Store({"2026-09-29": {"ZA": ("published", credible())}, D30: zero_card_day()}),
                               D30)["history"]
    assert single["zero_card_days"] == 1 and single["action"] is None
    assert single["days"][1]["cards"] > 0
    day = one["history"]["days"][0]
    assert {m["market"]: m["held_by_reason"] for m in day["markets"]}["KE"] == {"explanation_failed": 3,
                                                                                  "data_issue": 1}


def test_coverage_carries_the_brief_outcome_and_a_failed_read_never_reads_as_ready(monkeypatch):
    out = coverage.build_coverage(Store({D30: zero_card_day()}), D30)
    assert out["brief_outcome"]["status"] == "no_credible_cards"
    assert out["brief_outcome"]["value_ready"] is False
    json.dumps(out)

    class Broken(FixtureStore):
        def briefs(self, date):
            raise RuntimeError("briefs unreadable")

    out = coverage.build_coverage(Broken(), D30)
    assert out["brief_outcome"]["status"] == "unknown" and out["brief_outcome"]["value_ready"] is False
