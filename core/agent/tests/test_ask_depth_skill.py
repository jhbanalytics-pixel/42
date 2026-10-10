"""Ask depth, item 5: the culture-read skill tells a named topic at T1 to spend up to the 55% first round share across
TikTok and two other platforms, and to pass the market on every live search."""

from datetime import date, datetime

from core.agent import ask, skills
from core.agent.tools.socialcrawl import MARKET_PARAMS

LINE = next(line for line in skills.load_skill("culture-read").splitlines() if line.startswith("4 Fetch fresh data"))


def test_the_live_search_line_keeps_its_gap_rule_and_its_page_stop():
    assert "only for gaps or data older than 48 hours" in LINE
    assert "At T0 do not call socialcrawl_call" in LINE
    assert "Check budget_status before each live call" in LINE
    assert "under 20% new authors; three pages at most" in LINE


def test_a_named_topic_at_t1_may_spend_the_first_round_share_across_tiktok_and_two_other_platforms():
    for phrase in ("named topic", "T1", "55%", "TikTok", "two other platforms"):
        assert phrase in LINE, phrase
    assert "thin" in LINE


def test_every_live_search_passes_the_market_in_the_routes_own_market_parameter():
    assert "every live search" in LINE and "market" in LINE
    assert all(name in LINE for name in MARKET_PARAMS)


def test_the_rendered_skill_keeps_the_credit_and_call_figures():
    text = ask.render_skill(as_of=datetime(2026, 10, 10, 8, 0), markets=["ZA"], window=(date(2026, 10, 4), date(2026, 10, 10)),
                            tier="T1")
    assert "{credits}" not in text and "{calls}" not in text and "60 live credits and 20 live calls" in text and "55%" in text
