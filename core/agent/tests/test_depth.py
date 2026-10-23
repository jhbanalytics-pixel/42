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


def test_deep_needs_t2_ready_and_the_switch(monkeypatch):
    assert depth.DEEP_FOR_PLAIN_ASKS is True  # Albert approved Deep for plain asks on 16 Oct 2026
    monkeypatch.setattr(depth, "DEEP_FOR_PLAIN_ASKS", False)
    assert depth.deep_open(True) is False
    monkeypatch.setattr(depth, "DEEP_FOR_PLAIN_ASKS", True)
    assert depth.deep_open(True) is True
    assert depth.deep_open(False) is False
    assert depth.deep_open("1") is False


def test_share_follows_the_days_ask_credits():
    assert depth.levels(deep=False, daily=1200)[2]["share_of_daily"] == 0.25
    assert depth.levels(deep=False)[2]["share_of_daily"] == 0.5


def test_depth_reads_no_environment():
    import inspect

    source = inspect.getsource(depth)
    assert "os.environ" not in source and "getenv" not in source and "import os" not in source


def test_quick_reads_the_warehouse_only():
    quick = next(lv for lv in depth.levels(deep=False, daily=600) if lv["tier"] == "T0")
    assert "live" not in quick["reads"].lower()
    assert "only" in quick["reads"]
    assert quick["max_credits"] == 0 and quick["max_live_calls"] == 0 and quick["share_of_daily"] == 0
