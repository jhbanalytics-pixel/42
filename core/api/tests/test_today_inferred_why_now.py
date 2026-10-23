"""Today admits a card whose why-now is inferred from code-counted adoption, as the brief job writes it (W07-01):
the critic rules the simpler explanation out, no cited post states the local cause, and the producer's adoption count
(critic.why_now_adoption) shows 3 or more independent local creators on 2 or more days."""

import copy

import pytest

from core.api import today
from core.api.tests.test_today import D30, _fixture_critics, _passing_specificity_card, _today_store, market

ADOPTION = {"pattern": "the step", "post_ids": ["p1", "p2", "p3"], "creators": 3, "days": 2}


def inferred_brief(adoption=ADOPTION, **critic):
    card = _passing_specificity_card()
    card["specificity"]["why_now_basis"] = "inferred"
    [entry] = _fixture_critics([card])
    entry.update(local_why_now=False, **critic)
    if adoption is not None:
        entry["why_now_adoption"] = copy.deepcopy(adoption)
    return card, {"cards": [card], "more": [], "critic": [entry], "held_back": {"count": 0, "items": []}}


def test_an_inferred_why_now_card_with_counted_adoption_is_admitted_and_shown():
    card, payload = inferred_brief()
    assert today._publication_fault(card, payload) is None
    assert today._stored_card_fault(card, payload) is None
    store = _today_store([card])
    fixture_briefs = store.briefs

    def briefs(date):
        rows = fixture_briefs(date)
        for row in rows:
            if date == D30 and row["market"] == "ZA":
                row["payload"]["critic"] = copy.deepcopy(payload["critic"])
        return rows

    store.briefs = briefs
    za = market(today.build_today(store, D30), "ZA")
    assert [c["item_id"] for c in za["cards"]] == [card["item_id"]]
    assert za["cards"][0]["specificity"]["why_now_basis"] == "inferred"


@pytest.mark.parametrize("adoption, critic", [
    (None, {}),
    ("three creators", {}),
    ({**ADOPTION, "creators": 2}, {}),
    ({**ADOPTION, "days": 1}, {}),
    ({**ADOPTION, "post_ids": ["p1", "p1", "p2"]}, {}),
    (ADOPTION, {"ruled_out": False}),
])
def test_without_a_counted_adoption_floor_or_with_the_rival_standing_the_card_is_not_admitted(adoption, critic):
    card, payload = inferred_brief(adoption, **critic)
    assert today._publication_fault(card, payload) is not None
