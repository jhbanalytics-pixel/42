from copy import deepcopy
import json
from pathlib import Path

import pytest

from core.api import today
from core.api import history
from core.api.store import BigQueryStore
from core.api.tests.test_today import D30, Patched, _passing_specificity_card, _today_store, market


def critic(card, **changes):
    return {"item_id": card["item_id"], "ruled_out": True, "news_driven": False,
            "scheduled_event": False, "local_reaction": False, "local_why_now": True,
            "reason": "Several creators repeat the same supported practice.",
            "non_cultural_explanation": "One fixture could explain discussion, but not this repeated practice.",
            **changes}


def saved_store(card, critics):
    base = _today_store([card])

    def briefs(day):
        rows = deepcopy(base.briefs(day))
        next(r for r in rows if r["market"] == "ZA")["payload"]["critic"] = critics
        return rows

    return Patched(briefs=briefs)


@pytest.mark.parametrize("case", ["reaction", "missing", "malformed", "conflict", "unnamed"])
def test_old_saved_cards_are_held_without_rewriting_or_losing_evidence(case):
    card = _passing_specificity_card()
    critics = [critic(card)]
    if case == "reaction":
        critics = [critic(card, ruled_out=False, scheduled_event=True, local_reaction=True)]
    elif case == "missing":
        critics = []
    elif case == "malformed":
        critics = [critic(card, ruled_out="true")]
    elif case == "conflict":
        critics.append(critic(card, ruled_out=False))
    else:
        card.update(kind="topic", title="Topic 01186adf", title_written=None)
    store = saved_store(card, critics)
    before = deepcopy(store.briefs(D30))
    za = market(today.build_today(store, D30), "ZA")
    assert not za["cards"] + za["more"]
    held = next(h for h in za["held_back"]["items"] if h["item_id"] == card["item_id"])
    assert held["reason"] == "not_confirmed"
    assert held["explanation"] is None and held["claims"] == []
    assert held["numbers"] == card["numbers"] and held["evidence"] == card["evidence"]
    assert today.build_trends(store, "ZA", D30)["cards"] == []
    detail = today.build_trend(store, card["item_id"], "ZA", D30)
    assert detail["explained"] is False and detail["claims"] == []
    assert detail["held_back"]["reason"] == "not_confirmed"
    assert detail["numbers"] == card["numbers"] and detail["evidence"] == card["evidence"]
    payload = before[0]["payload"]
    assert card["item_id"] in today.brief_counts(payload)["held"]
    assert store.briefs(D30) == before


def test_supported_sporting_practice_and_meaningful_old_label_remain_published():
    card = _passing_specificity_card()
    card.update(title="Fans remake match chants as dance clips", title_written=None)
    store = saved_store(card, [critic(card)])
    za = market(today.build_today(store, D30), "ZA")
    assert [c["item_id"] for c in za["cards"]] == [card["item_id"]]
    assert today.build_trend(store, card["item_id"], "ZA", D30)["explained"] is True


@pytest.mark.parametrize("field", ["ruled_out", "local_why_now", "news_driven", "scheduled_event", "local_reaction"])
@pytest.mark.parametrize("invalid_first", [False, True])
def test_duplicate_numeric_boolean_critic_is_unassessed_in_either_order(field, invalid_first):
    card = _passing_specificity_card()
    valid = critic(card)
    malformed = {**valid, field: int(valid[field])}
    critics = [malformed, valid] if invalid_first else [valid, malformed]
    store = saved_store(card, critics)
    za = market(today.build_today(store, D30), "ZA")
    assert not za["cards"] + za["more"]
    held = next(h for h in za["held_back"]["items"] if h["item_id"] == card["item_id"])
    assert held["reason_text"] == "The cultural pattern has not been assessed"
    assert today.build_trends(store, "ZA", D30)["cards"] == []
    detail = today.build_trend(store, card["item_id"], "ZA", D30)
    assert detail["held_back"]["reason_text"] == "The cultural pattern has not been assessed"
    assert detail["explained"] is False and detail["claims"] == []


def test_identical_valid_duplicate_critics_remain_admissible():
    card = _passing_specificity_card()
    valid = critic(card)
    store = saved_store(card, [valid, deepcopy(valid)])
    assert market(today.build_today(store, D30), "ZA")["cards"][0]["item_id"] == card["item_id"]
    assert today.build_trends(store, "ZA", D30)["cards"][0]["item_id"] == card["item_id"]
    assert today.build_trend(store, card["item_id"], "ZA", D30)["explained"] is True


def test_history_projection_carries_retained_critic_and_counts_same_admission():
    card = _passing_specificity_card()
    store = saved_store(card, [critic(card)])
    row = next(r for r in store.briefs(D30) if r["market"] == "ZA")
    store.brief_history = lambda *args: [dict(row, brief_date=D30)]
    out = history.build_history_briefs(store, market="ZA")
    figures = out["dates"][0]["markets"][0]
    assert figures["cards"]["value"] == 1
    assert figures["held"]["value"] == len(today.brief_counts(row["payload"])["held"])
    sql = []
    bq = object.__new__(BigQueryStore)
    bq._query = lambda query, **kwargs: sql.append(query) or []
    bq._t = lambda name: name
    bq.brief_history(30, None, "ZA")
    assert "'critic', JSON_QUERY(b.payload, '$.critic')" in sql[0]


def test_producer_rejects_placeholder_names_without_losing_real_labels():
    from core.brief.job import readable_title

    assert readable_title("Topic 01186adf", "0" * 64) is None
    assert readable_title("Unnamed topic", "0" * 64) is None
    assert readable_title("Fans remix chants", "0" * 64) == "Fans remix chants"


def test_four_retained_nigeria_cards_fail_new_admission_without_mutation():
    record = json.loads((Path(__file__).parent / "fixtures" / "ng_four_cards_publication.json").read_text(encoding="utf-8"))
    before = deepcopy(record)
    payload = {"cards": record["cards"], "more": [], "critic": record["critic"]}
    assert len(payload["cards"]) == 4
    assert all(today._today_card_fault(c) is None for c in payload["cards"])
    assert all(today._publication_fault(c, payload) is not None for c in payload["cards"])
    assert today.brief_counts(payload) == {"shown": [], "held": [c["item_id"] for c in payload["cards"]]}
    assert [today._publication_fault(c, payload) for c in payload["cards"]].count("No readable name") == 2
    day = "2026-10-15"
    store = Patched(briefs=lambda date: [{"brief_date": day, "market": "NG", "status": "partial",
                                         "run_id": "retained-native", "payload": payload}],
                    latest_brief_date=lambda: day)
    ng = market(today.build_today(store, day), "NG")
    assert not ng["cards"] + ng["more"]
    assert {h["item_id"] for h in ng["held_back"]["items"]} == {c["item_id"] for c in record["cards"]}
    assert today.build_trends(store, "NG", day)["cards"] == []
    for card in record["cards"]:
        detail = today.build_trend(store, card["item_id"], "NG", day)
        assert detail["held_back"]["reason"] == "not_confirmed"
        assert detail["explained"] is False and detail["explanation"] is None and detail["claims"] == []
        assert detail["numbers"] == card["numbers"]
        assert detail["evidence"] == today._platform_x(card["evidence"])
    assert record == before


def test_existing_held_reason_survives_even_with_passing_stored_critic():
    card = _passing_specificity_card()
    payload = {"cards": [card], "critic": [critic(card)],
               "held_back": {"items": [{"item_id": card["item_id"], "title": card["title"],
                                         "reason": "data_issue", "reason_text": "Evidence could not be read"}]}}
    assert today.brief_counts(payload) == {"shown": [], "held": [card["item_id"]]}
