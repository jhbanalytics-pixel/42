"""A market with no brief reads as one on the Today page because of one sentence. The page's hasNoBrief (app/frontend/src/
today42.jsx) accepts a market when its status is data_issue, its held_back count is 0 with no items, and a data_issue
banner starts with NO_BRIEF_BANNER, /^Data issue: no brief was published for /. The frontend tests feed it that
sentence by hand; this pins the API side, so a reworded banner in core/api/today.py cannot turn "42 has no brief for
Kenya" back into a held-back list that says nothing was held."""
import re

import pytest

from core.api import today
from core.api.store import FixtureStore
from core.api.tests.test_today import Patched, market

DAY = "2026-09-30"
FRONTEND_PATTERN = re.compile(r"^Data issue: no brief was published for ")
NAMES = {"ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya"}


def without_brief(code):
    return Patched(briefs=lambda date: [r for r in FixtureStore().briefs(date) if r["market"] != code])


def accepted_as_no_brief(m):
    """hasNoBrief, as the page writes it."""
    held = m.get("held_back")
    return (m.get("status") == "data_issue" and bool(held) and held.get("count") == 0
            and (not isinstance(held.get("items"), list) or len(held["items"]) == 0)
            and isinstance(m.get("banners"), list)
            and any(isinstance(b, dict) and b.get("kind") == "data_issue"
                    and FRONTEND_PATTERN.search(str(b.get("text") or "")) for b in m["banners"]))


@pytest.mark.parametrize("code", ["ZA", "NG", "KE"])
def test_a_market_with_no_brief_carries_the_exact_banner_sentence_the_page_matches(code):
    out = market(today.build_today(without_brief(code), DAY), code)
    banners = [b for b in out["banners"] if b["kind"] == "data_issue"]
    assert {"kind": "data_issue", "text": f"Data issue: no brief was published for {NAMES[code]}"} in banners
    assert accepted_as_no_brief(out)


@pytest.mark.parametrize("code", ["ZA", "NG", "KE"])
def test_a_market_that_has_a_brief_is_not_taken_for_one_without(code):
    out = market(today.build_today(FixtureStore(), "2026-09-30"), code)
    assert not any(FRONTEND_PATTERN.search(b["text"]) for b in out["banners"])
    assert not accepted_as_no_brief(out)


def test_the_fixture_day_with_no_kenya_brief_reads_as_no_brief():
    out = market(today.build_today(FixtureStore(), "2026-09-29"), "KE")
    assert accepted_as_no_brief(out)
    assert out["held_back"]["count"] == 0 and out["held_back"]["items"] == []

