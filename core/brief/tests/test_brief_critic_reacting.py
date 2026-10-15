"""Reacting creator counts remain auditable but cannot grant cultural qualification."""

import pytest

from core.brief.explain import _critic_row

NEWS = {"non_cultural_explanation": "a news event", "ruled_out": False, "news_driven": True,
        "scheduled_event": False, "local_reaction": True, "local_why_now": True, "reason": "posts react to the news"}


@pytest.mark.parametrize(("reacting", "verdict", "standing"), [
    (0, "cut", "not ruled out"), (1, "cut", "not ruled out"),
    (2, "cut", "not ruled out"), (5, "cut", "not ruled out"),
])
def test_the_row_records_count_without_qualifying_ordinary_reaction(reacting, verdict, standing):
    row = _critic_row(NEWS, reacting)
    assert row["verdict"] == verdict
    assert f"; {standing}: posts react to the news; reacting local creators: {reacting}; local why-now checked" in row["detail"]
    assert row["detail"].endswith("local why-now checked")


def test_a_row_made_without_a_count_records_zero():
    assert "; reacting local creators: 0; local why-now checked" in _critic_row(NEWS)["detail"]
