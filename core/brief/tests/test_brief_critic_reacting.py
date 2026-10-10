"""The critic row records how many different local creators reacted (N24). Record only: the verdict, the standing
words in the detail and the 2-creator rule (NEWS_MIN_CREATORS) are exactly as before."""

import pytest

from core.brief.explain import _critic_row

NEWS = {"non_cultural_explanation": "a news event", "ruled_out": False, "news_driven": True,
        "scheduled_event": False, "local_reaction": True, "local_why_now": True, "reason": "posts react to the news"}


@pytest.mark.parametrize(("reacting", "verdict", "standing"), [
    (0, "cut", "not ruled out"), (1, "cut", "not ruled out"),
    (2, "pass", "news-driven with local reaction"), (5, "pass", "news-driven with local reaction"),
])
def test_the_row_records_the_reacting_creator_count_and_the_verdict_is_unchanged(reacting, verdict, standing):
    row = _critic_row(NEWS, reacting)
    assert row["verdict"] == verdict
    assert f"; {standing}: posts react to the news; reacting local creators: {reacting}; local why-now checked" in row["detail"]
    assert row["detail"].endswith("local why-now checked")


def test_a_row_made_without_a_count_records_zero():
    assert "; reacting local creators: 0; local why-now checked" in _critic_row(NEWS)["detail"]
