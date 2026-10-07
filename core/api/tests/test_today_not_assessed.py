"""Today omission disclosure is separate from held and shown content."""
from copy import deepcopy
import json

import pytest

from core.api import skins, today
from core.api.store import FixtureStore
from core.api.tests.test_today import _passing_specificity_card, _today_store


DAY = "2026-09-30"


def omission(item_id, rank, reason):
    return {"item_id": item_id, "title": f"Topic {rank}", "status": "not_assessed", "reason": reason,
            "reason_text": "Checks did not run.", "sql_rank": rank,
            "pool_rank": rank if rank <= 90 else None, "market_scope": "market"}


def block(items):
    return {"detect_run_id": "detect-fixture", "pool_limit": 90, "judged_limit": 10,
            "count": len(items), "items": items}


def market(payload):
    return today._market(FixtureStore(), "NG", DAY, {"status": "published", "payload": payload},
                         [], [], {"active": False}, True, False, {})


def test_reader_projects_omissions_and_excludes_contradictory_held_rows():
    held = {"item_id": "held-topic", "title": "Held topic", "rule": "G10", "reason": "explanation_failed"}
    data = {"cards": [], "more": [], "held_back": {"items": [held]}, "not_assessed": block([
        omission("outside", 149, "outside_candidate_pool"), omission("limited", 18, "judged_limit_reached"),
        omission("held-topic", 19, "judged_limit_reached")])}
    result = market(data)
    assert result["not_assessed"]["count"] == 2
    assert {item["item_id"] for item in result["not_assessed"]["items"]} == {"outside", "limited"}
    assert result["held_back"]["count"] == 1 and result["held_back"]["items"][0]["item_id"] == "held-topic"
    assert result["cards"] == result["more"] == []


def test_legacy_missing_audit_is_unknown():
    assert market({})["not_assessed"] is None


def test_omitted_titles_pass_through_existing_suppression():
    item = omission("outside", 149, "outside_candidate_pool")
    item["title"] = "A topic by @hidden_handle"
    data = {"not_assessed": block([item])}
    masked = today.without_hidden(deepcopy(data), ({"twitter:hidden_handle"}, set(), set()))
    result = market(masked)
    assert result["not_assessed"]["count"] == 1
    assert "hidden_handle" not in json.dumps(result["not_assessed"])


def test_previous_card_with_explicit_omission_is_not_called_unconfirmed():
    data = {"not_assessed": block([omission("previous", 18, "judged_limit_reached")])}
    previous = ("2026-09-29", {"payload": {"cards": [{"item_id": "previous", "title": "Previous topic"}]}})
    [item] = today._dropped(data, previous, set(), [])["items"]
    assert item["reason"] == "not_assessed" and item["reason_text"] == "Not assessed this morning"


def test_skin_narrows_omissions_without_adding_them_to_held_or_shown_counts():
    first = omission("boxing", 18, "judged_limit_reached")
    first["title"] = "Boxing topic"
    data = {"markets": [{"market": "NG", "cards": [], "more": [], "held_back": {"count": 0, "items": []},
                        "dropped": {"items": []}, "not_assessed": block([
                            first, omission("unrelated", 149, "outside_candidate_pool")])}]}
    result = skins.narrow_today(data, {"markets": ["NG"], "terms": ["boxing"], "hashtags": []})
    [market] = result["markets"]
    assert market["not_assessed"]["count"] == 1
    assert [item["item_id"] for item in market["not_assessed"]["items"]] == ["boxing"]
    assert market["held_back"]["count"] == market["skin_note"]["kept"] == 0


@pytest.mark.parametrize("reason", [[], {}])
def test_malformed_audit_reason_is_unknown_without_losing_today_cards_or_holds(reason):
    card = _passing_specificity_card()
    held = {"item_id": "held-topic", "title": "Held topic", "rule": "G10", "reason": "explanation_failed"}
    store = _today_store([card], held_back={"count": 1, "items": [held]})
    baseline = today.build_today(store, date=DAY)
    original_briefs = store.briefs

    def briefs(day):
        rows = original_briefs(day)
        if day == DAY:
            for row in rows:
                if row["market"] == "ZA":
                    item = omission("outside", 149, "outside_candidate_pool")
                    item["reason"] = reason
                    row["payload"]["not_assessed"] = block([item])
        return rows

    store.briefs = briefs
    result = today.build_today(store, date=DAY)
    previous = next(market for market in baseline["markets"] if market["market"] == "ZA")
    current = next(market for market in result["markets"] if market["market"] == "ZA")
    assert len(previous["cards"]) == previous["held_back"]["count"] == 1
    assert current["not_assessed"] is None
    assert current["cards"] == previous["cards"] and current["more"] == previous["more"]
    assert current["held_back"] == previous["held_back"]
