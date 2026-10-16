"""The depth slider's levels (core/agent/depth.py) carry the tiers' own caps and the asks' own model holds."""

from core.agent import ask, depth
from core.agent.context import TIERS


def test_levels_are_the_tiers_caps_lowest_first():
    levels = depth.levels(deep=True, daily=600, model=ask.MODEL)
    assert [lv["tier"] for lv in levels] == ["T0", "T1", "T2"]
    for lv in levels:
        tier = TIERS[lv["tier"]]
        assert lv["max_credits"] == tier["credits"] and lv["max_live_calls"] == tier["calls"]
        assert lv["max_model_usd"] == round(ask.hold_usd(lv["tier"], ask.MODEL) + ask.figures_usd(ask.MODEL), 2)
        assert lv["share_of_daily"] == round(tier["credits"] / 600, 3)
    assert [lv["max_credits"] for lv in levels] == sorted(lv["max_credits"] for lv in levels)
    assert [lv["cost"] for lv in levels] == ["low", "normal", "highest"]


def test_deep_is_closed_with_a_reason_unless_opened():
    closed = depth.levels(deep=False, daily=600)
    assert [lv["open"] for lv in closed] == [True, True, False]
    assert closed[2]["closed_reason"]
    assert all(lv["open"] and lv["closed_reason"] is None for lv in depth.levels(deep=True, daily=600))


def test_deep_needs_both_switches_set_to_exactly_1(monkeypatch):
    for ready, deep, expected in (("1", "1", True), ("1", None, False), (None, "1", False), ("1", "true", False)):
        for name, value in (("F42_T2_READY", ready), ("F42_ASK_DEEP", deep)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        assert depth.deep_open() is expected


def test_share_follows_ask_daily_and_survives_a_bad_value(monkeypatch):
    monkeypatch.setenv("ASK_DAILY", "1200")
    assert depth.levels(deep=False)[2]["share_of_daily"] == 0.25
    monkeypatch.setenv("ASK_DAILY", "lots")
    assert depth.levels(deep=False)[2]["share_of_daily"] == 0.5
