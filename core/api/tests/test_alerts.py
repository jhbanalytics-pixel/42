import json

import pytest

from core.api.alerts import evaluate


def item(item_id, market="ZA", kind="topic", key=None, label=None, state=None, ratio=None, reach=None,
         date="2026-10-20"):
    return {"item_id": item_id, "market": market, "kind": kind, "canonical_key": key or item_id,
            "label": label or item_id, "state": state, "main_ratio": ratio, "creators3": reach,
            "metric_date": date}


def watch(watch_id, target, rule, market="ZA", status="active", label="A watch"):
    return {"watch_id": watch_id, "created_at": "2026-10-19T08:00:00+02:00", "who": "passcode",
            "target": target, "market": market, "rule": rule, "label": label, "status": status}


ITEM = {"kind": "item", "item_id": "i1"}


def test_state_in_fires_on_entering_the_state():
    alerts = evaluate([watch("w_1", ITEM, {"state_in": ["rising"]})],
                      [item("i1", state="rising")], [item("i1", state="emerging", date="2026-10-19")])
    assert alerts == [{"watch_id": "w_1", "label": "A watch", "item_id": "i1", "market": "ZA",
                       "fired_because": "Entered Rising", "since": "2026-10-20"}]


def test_state_in_does_not_fire_when_already_in_the_state():
    assert evaluate([watch("w_1", ITEM, {"state_in": ["rising", "peaking"]})],
                    [item("i1", state="peaking")], [item("i1", state="rising")]) == []


def test_state_in_fires_when_item_is_new_today():
    alerts = evaluate([watch("w_1", ITEM, {"state_in": ["emerging"]})], [item("i1", state="emerging")], [])
    assert [a["fired_because"] for a in alerts] == ["Entered Emerging"]


def test_state_not_in_list_does_not_fire():
    assert evaluate([watch("w_1", ITEM, {"state_in": ["rising"]})],
                    [item("i1", state="spike")], [item("i1", state="new_to_42")]) == []


def test_ratio_over_fires_only_on_crossing():
    w = [watch("w_1", ITEM, {"ratio_over": 3})]
    crossed = evaluate(w, [item("i1", ratio=3.4)], [item("i1", ratio=2.1)])
    assert len(crossed) == 1 and crossed[0]["item_id"] == "i1"
    assert "3" in crossed[0]["fired_because"]
    assert evaluate(w, [item("i1", ratio=3.0)], [item("i1", ratio=None)])[0]["watch_id"] == "w_1"
    assert evaluate(w, [item("i1", ratio=4.0)], [item("i1", ratio=3.5)]) == []
    assert evaluate(w, [item("i1", ratio=2.9)], [item("i1", ratio=1.0)]) == []
    assert evaluate(w, [item("i1", ratio=None)], []) == []


def test_reach_over_fires_only_on_crossing():
    w = [watch("w_1", ITEM, {"reach_over": 50})]
    crossed = evaluate(w, [item("i1", reach=50)], [item("i1", reach=12)])
    assert len(crossed) == 1 and "50" in crossed[0]["fired_because"]
    assert evaluate(w, [item("i1", reach=80)], [item("i1", reach=60)]) == []
    assert evaluate(w, [item("i1", reach=49)], []) == []


def test_creator_surge_waits_while_there_are_no_watch_matches_to_read():
    today = [item("i1", kind="creator", state="rising", ratio=9, reach=900)]
    assert evaluate([watch("w_1", ITEM, {"creator_surge": True})], today, [], None) == [
        {"watch_id": "w_1", "waiting": "Waiting for creator views detection"}]


def test_paused_watches_never_fire_or_wait():
    today = [item("i1", state="rising")]
    assert evaluate([watch("w_1", ITEM, {"state_in": ["rising"]}, status="paused"),
                     watch("w_2", ITEM, {"breakout": True}, status="paused")], today, []) == []


def test_market_filters_and_all_matches_every_market():
    today = [item("i1", market="ZA", state="rising"), item("i1", market="NG", state="rising"),
             item("i1", market="KE", state="rising")]
    only_ng = evaluate([watch("w_1", ITEM, {"state_in": ["rising"]}, market="NG")], today, [])
    assert [a["market"] for a in only_ng] == ["NG"]
    every = evaluate([watch("w_1", ITEM, {"state_in": ["rising"]}, market="all")], today, [])
    assert sorted(a["market"] for a in every) == ["KE", "NG", "ZA"]


def test_yesterday_is_matched_per_market():
    today = [item("i1", market="ZA", state="rising"), item("i1", market="NG", state="rising")]
    yesterday = [item("i1", market="ZA", state="rising"), item("i1", market="NG", state="emerging")]
    alerts = evaluate([watch("w_1", ITEM, {"state_in": ["rising"]}, market="all")], today, yesterday)
    assert [a["market"] for a in alerts] == ["NG"]


@pytest.mark.parametrize("kind, value, key", [
    ("hashtag", "#Amapiano", "amapiano"),
    ("hashtag", "amapiano", "#amapiano"),
    ("creator", "@Some  Creator", "some creator"),
    ("sound", "  Water   Dance ", "water dance"),
])
def test_hashtag_sound_creator_resolve_by_kind_and_normalised_key(kind, value, key):
    today = [item("x1", kind=kind, key=key, state="rising"),
             item("x2", kind="topic", key=key, state="rising"),
             item("x3", kind=kind, key=key + "extra", state="rising")]
    alerts = evaluate([watch("w_1", {"kind": kind, "value": value}, {"state_in": ["rising"]})], today, [])
    assert [a["item_id"] for a in alerts] == ["x1"]


def test_brand_and_query_resolve_by_whole_word_in_label():
    today = [item("b1", label="Nando's new ad in Soweto", state="rising"),
             item("b2", label="Nandos-style chicken", state="rising"),
             item("b3", label="NANDO'S braai day", state="rising"),
             item("b4", label="Pernandoso", state="rising")]
    alerts = evaluate([watch("w_1", {"kind": "brand", "value": "nando's"}, {"state_in": ["rising"]})], today, [])
    assert [a["item_id"] for a in alerts] == ["b1", "b3"]
    alerts = evaluate([watch("w_2", {"kind": "query", "value": "braai  day"}, {"state_in": ["rising"]})],
                      today, [])
    assert [a["item_id"] for a in alerts] == ["b3"]


def test_matches_override_keyword_for_that_watch_only():
    today = [item("b1", label="Nando's new ad", state="rising"),
             item("b2", label="Chicken wars", state="rising")]
    watches = [watch("w_1", {"kind": "brand", "value": "nando's"}, {"state_in": ["rising"]}),
               watch("w_2", {"kind": "query", "value": "nando's"}, {"state_in": ["rising"]})]
    alerts = evaluate(watches, today, [], matches={"w_1": ["b2"]})
    assert [(a["watch_id"], a["item_id"]) for a in alerts] == [("w_1", "b2"), ("w_2", "b1")]


def test_target_and_rule_may_arrive_as_json_strings():
    w = watch("w_1", json.dumps(ITEM), json.dumps({"state_in": ["rising"]}))
    assert len(evaluate([w], [item("i1", state="rising")], [])) == 1


def test_label_falls_back_to_the_item_label():
    alerts = evaluate([watch("w_1", ITEM, {"state_in": ["rising"]}, label=None)],
                      [item("i1", label="Amapiano dance", state="rising")], [])
    assert alerts[0]["label"] == "Amapiano dance"


def test_unmatched_target_gives_nothing():
    assert evaluate([watch("w_1", {"kind": "item", "item_id": "nope"}, {"state_in": ["rising"]})],
                    [item("i1", state="rising")], []) == []


def test_evaluate_does_not_change_its_inputs():
    watches = [watch("w_1", ITEM, {"state_in": ["rising"]})]
    today, yesterday = [item("i1", state="rising")], [item("i1", state="spike")]
    before = json.dumps([watches, today, yesterday])
    evaluate(watches, today, yesterday, matches={})
    assert json.dumps([watches, today, yesterday]) == before


def test_no_earlier_run_gives_only_waiting_entries():
    today = [item("i1", state="rising", ratio=9, reach=900)]
    watches = [watch("w_1", ITEM, {"state_in": ["rising"]}, label="Rising watch"),
               watch("w_2", ITEM, {"ratio_over": 3}, label=None),
               watch("w_3", ITEM, {"reach_over": 50}),
               watch("w_4", ITEM, {"breakout": True}),
               watch("w_5", ITEM, {"state_in": ["rising"]}, status="paused"),
               watch("w_6", ITEM, {"tone_flip": True}),
               watch("w_7", ITEM, {"creator_surge": True})]
    compare = "Waiting for a second detect run to compare against"
    assert evaluate(watches, today, None, {"w_7": ["i1"]}) == [
        {"watch_id": "w_1", "label": "Rising watch", "waiting": compare},
        {"watch_id": "w_2", "label": None, "waiting": compare},
        {"watch_id": "w_3", "label": "A watch", "waiting": compare},
        {"watch_id": "w_4", "label": "A watch", "waiting": compare},
        {"watch_id": "w_6", "label": "A watch", "waiting": compare},
        {"watch_id": "w_7", "label": "A watch", "waiting": compare}]


def toned(item_id, today, before, **kw):
    return dict(item(item_id, **kw), tone_today=today, tone_before=before)


def test_tone_flip_fires_when_todays_tone_and_the_day_befores_have_opposite_signs():
    w = [watch("w_1", ITEM, {"tone_flip": True})]
    alerts = evaluate(w, [toned("i1", -0.4, 0.6)], [item("i1")])
    assert alerts == [{"watch_id": "w_1", "label": "A watch", "item_id": "i1", "market": "ZA",
                       "fired_because": "Tone flipped from positive to negative", "since": "2026-10-20"}]
    up = evaluate(w, [toned("i1", 0.2, -1.0)], [])
    assert [a["fired_because"] for a in up] == ["Tone flipped from negative to positive"]


def test_tone_flip_does_not_fire_without_two_tones_of_opposite_sign():
    w = [watch("w_1", ITEM, {"tone_flip": True})]
    for now, then in ((0.4, 0.6), (-0.4, -0.2), (0.0, 0.6), (-0.4, 0.0), (None, 0.6), (-0.4, None)):
        assert evaluate(w, [toned("i1", now, then)], [item("i1")]) == [], (now, then)
    assert evaluate(w, [item("i1", state="rising", ratio=9, reach=900)], []) == []


def test_creator_surge_fires_for_the_items_detect_matched_for_that_watch_today():
    w = [watch("w_1", {"kind": "creator", "value": "@dj_x"}, {"creator_surge": True}, market="all"),
         watch("w_2", {"kind": "creator", "value": "@dj_y"}, {"creator_surge": True})]
    today = [item("c1", kind="creator", key="tiktok:dj_x"), item("c1", market="NG", kind="creator", key="tiktok:dj_x"),
             item("c2", kind="creator", key="tiktok:dj_y")]
    alerts = evaluate(w, today, [], {"w_1": ["c1"], "w_other": ["c2"]})
    assert [(a["watch_id"], a["item_id"], a["market"], a["fired_because"]) for a in alerts] == [
        ("w_1", "c1", "ZA", "Creator surge: a post at three or more times their usual views"),
        ("w_1", "c1", "NG", "Creator surge: a post at three or more times their usual views")]
    assert evaluate(w, today, [], {}) == []


def test_creator_surge_fires_only_on_entering_not_when_the_run_before_matched_too():
    w = [watch("w_1", {"kind": "creator", "value": "@dj_x"}, {"creator_surge": True})]
    today = [item("c1", kind="creator", key="tiktok:dj_x")]
    assert evaluate(w, today, [], {"w_1": ["c1"]}, {"w_1": ["c1"]}) == []
    assert evaluate(w, today, [], {"w_1": ["c1"]}, {"w_other": ["c1"]})[0]["item_id"] == "c1"
    assert evaluate(w, today, [], {"w_1": ["c1"]}, {"w_1": ["c2"]})[0]["item_id"] == "c1"
    assert evaluate(w, today, [], {"w_1": ["c1"]}, None)[0]["item_id"] == "c1"


def test_breakout_fires_when_today_has_a_breakout_and_the_run_before_did_not():
    w = [watch("w_1", ITEM, {"breakout": True})]
    alerts = evaluate(w, [dict(item("i1"), breakout_creators=3)], [dict(item("i1"), breakout_creators=None)])
    assert alerts == [{"watch_id": "w_1", "label": "A watch", "item_id": "i1", "market": "ZA",
                       "fired_because": "Creator breakout: 3 creators well above their usual views",
                       "since": "2026-10-20"}]
    one = evaluate(w, [dict(item("i1"), breakout_creators=1)], [item("i1")])
    assert [a["fired_because"] for a in one] == ["Creator breakout: 1 creator well above their usual views"]
    assert len(evaluate(w, [dict(item("i1"), breakout_creators=2)], [])) == 1


def test_breakout_does_not_fire_without_a_breakout_today_or_when_the_run_before_had_one():
    w = [watch("w_1", ITEM, {"breakout": True})]
    assert evaluate(w, [dict(item("i1"), breakout_creators=4)], [dict(item("i1"), breakout_creators=2)]) == []
    assert evaluate(w, [dict(item("i1"), breakout_creators=None)], [item("i1")]) == []
    assert evaluate(w, [dict(item("i1"), breakout_creators=0)], [item("i1")]) == []
    assert evaluate(w, [item("i1", state="rising", ratio=9, reach=900)], []) == []


def test_item_missing_from_an_earlier_run_still_fires():
    today = [item("i1", state="rising", ratio=4, reach=60)]
    earlier = [item("other", state="spike", ratio=9, reach=900, date="2026-10-19")]
    for rule, because in (({"state_in": ["rising"]}, "Entered Rising"),
                          ({"ratio_over": 3}, "Growth passed 3 times its baseline"),
                          ({"reach_over": 50}, "Reach passed 50 creators in 3 days")):
        alerts = evaluate([watch("w_1", ITEM, rule)], today, earlier)
        assert [(a["item_id"], a["fired_because"]) for a in alerts] == [("i1", because)]


def test_brand_and_query_keywords_also_match_item_aliases():
    today = [dict(item("a1", label="#heritagefixture", state="rising"), aliases=["Heritage Day braai", "#braai"]),
             dict(item("a2", label="Chicken wars", state="rising"), aliases=None),
             dict(item("a3", label="Weekend", state="rising"), aliases=["braaiday"])]
    brand = evaluate([watch("w_1", {"kind": "brand", "value": "braai"}, {"state_in": ["rising"]})], today, [])
    assert [a["item_id"] for a in brand] == ["a1"]
    query = evaluate([watch("w_2", {"kind": "query", "value": "heritage  day"}, {"state_in": ["rising"]})],
                     today, [])
    assert [a["item_id"] for a in query] == ["a1"]


def test_state_alerts_read_as_plain_sentences():
    for state, because in (("new_to_42", "First spotted"), ("spike", "Spiked"), ("rising", "Entered Rising")):
        alerts = evaluate([watch("w_1", ITEM, {"state_in": [state]})], [item("i1", state=state)], [])
        assert [a["fired_because"] for a in alerts] == [because]
