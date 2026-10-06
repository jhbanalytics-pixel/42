"""Watch matches, detect side (BUILD.md 2.9, core/api/contract.md sections 10.5 and 10.7): known-answer tests on
watches.sql and watches.py through DuckDB, and a staging dry run that skips until L1's tables exist."""

import json
import os
from datetime import timedelta

import pytest

from core.detect import aggregate, sqlrun, watches
from core.detect.items import item_id

from . import duck
from .fixtures import D, at, obs, post

RUN = "detect-20260920-000000000001"
STEP = "watch-20260920-000000000001"


@pytest.fixture
def con():
    c = duck.connect()
    yield c
    c.close()


def item(con, key, market="ZA", kind="hashtag", label=None, aliases=None, state="rising", main_ratio=2.5,
         creators3=40, eligible=True, authenticity="clear", geo_status="local", sponsored_share=0.0,
         map_status="active", run_id=RUN, metric_date=D, canonical_key=None):
    """One item_state row for the detect run and its open cultural_map row; returns the item_id."""
    ck = canonical_key or key
    iid = item_id(kind, ck)
    open_rows = duck.query(con, "SELECT 1 x FROM {core}.cultural_map c WHERE c.item_id = @i", {"i": iid})
    if not open_rows:
        duck.load(con, "core.cultural_map", [{"item_id": iid, "kind": kind, "canonical_key": ck,
                                              "label": label or key, "aliases": aliases, "status": map_status}])
    duck.load(con, "core.item_state", [{
        "metric_date": metric_date, "market": market, "item_id": iid, "kind": kind, "state": state,
        "main_ratio": main_ratio, "creators3": creators3, "eligible": eligible, "authenticity": authenticity,
        "geo_status": geo_status, "sponsored_share": sponsored_share, "run_id": run_id}])
    return iid


def watch(con, watch_id, target, rule, market="ZA", status="active", hour=9, created_hour=9):
    duck.load(con, "agent.watches", [{
        "watch_id": watch_id, "created_at": at(D - timedelta(days=1), created_hour),
        "status_at": at(D - timedelta(days=1), hour), "who": "passcode", "target": json.dumps(target),
        "market": market, "rule": json.dumps(rule), "label": target.get("value") or target.get("item_id"),
        "status": status}])


def run(con, run_id=STEP, d=D):
    return watches.run_watches(duck.Client(con), d, RUN, run_id, core="core", agent="agent")


def matches(con):
    rows = duck.query(con, "SELECT m.watch_id, m.match_date, m.item_id, m.market, m.method, m.run_id "
                           "FROM {agent}.watch_matches m ORDER BY m.watch_id, m.item_id, m.market")
    return [(r["watch_id"], r["item_id"], r["market"], r["method"]) for r in rows]


# (a) A test watch fires on fixture data


def test_a_hashtag_watch_on_a_rising_item_fires_and_writes_one_row(con):
    amapiano = item(con, "amapiano", label="#Amapiano")
    watch(con, "w_1", {"kind": "hashtag", "value": "#Amapiano"}, {"state_in": ["rising"]})
    counts = run(con)
    rows = duck.query(con, "SELECT * FROM {agent}.watch_matches m")
    assert rows == [{"watch_id": "w_1", "match_date": D, "item_id": amapiano, "market": "ZA",
                     "method": "canonical_key", "run_id": STEP}]
    assert counts == {"watches": 1, "waiting": 0, "matches": 1, "appended": 1}


def test_no_active_watches_reads_no_items_and_appends_nothing(con):
    item(con, "amapiano")
    client = duck.Client(con)
    counts = watches.run_watches(client, D, RUN, STEP, core="core", agent="agent")
    assert counts == {"watches": 0, "waiting": 0, "matches": 0, "appended": 0}
    assert not any("item_state" in s for s in client.sql)
    assert matches(con) == []


# (b) Item watches fire when the rule is met on today's item_state, and not otherwise


def test_item_watch_state_in_fires_only_when_todays_state_is_in_the_list(con):
    rising = item(con, "a", state="rising")
    peaking = item(con, "b", state="peaking")
    watch(con, "w_a", {"kind": "item", "item_id": rising}, {"state_in": ["emerging", "rising"]})
    watch(con, "w_b", {"kind": "item", "item_id": peaking}, {"state_in": ["emerging", "rising"]})
    run(con)
    assert matches(con) == [("w_a", rising, "ZA", "item_id")]


def test_item_watch_ratio_over_fires_at_or_above_and_never_on_a_null_ratio(con):
    at_bar = item(con, "a", main_ratio=3.0)
    under = item(con, "b", main_ratio=2.99)
    warm_up = item(con, "c", main_ratio=None)
    for key, iid in (("a", at_bar), ("b", under), ("c", warm_up)):
        watch(con, f"w_{key}", {"kind": "item", "item_id": iid}, {"ratio_over": 3})
    run(con)
    assert matches(con) == [("w_a", at_bar, "ZA", "item_id")]


def test_item_watch_reach_over_fires_at_or_above(con):
    wide = item(con, "a", creators3=50)
    narrow = item(con, "b", creators3=49)
    watch(con, "w_a", {"kind": "item", "item_id": wide}, {"reach_over": 50})
    watch(con, "w_b", {"kind": "item", "item_id": narrow}, {"reach_over": 50})
    run(con)
    assert matches(con) == [("w_a", wide, "ZA", "item_id")]


def test_unknown_and_empty_rules_never_fire_and_no_rule_is_waiting_any_more(con):
    iid = item(con, "a")
    watch(con, "w_unknown", {"kind": "item", "item_id": iid}, {"sparkle": True})
    watch(con, "w_empty", {"kind": "item", "item_id": iid}, {})
    counts = run(con)
    assert matches(con) == []
    assert counts == {"watches": 2, "waiting": 0, "matches": 0, "appended": 0}
    assert watches.WAITING == ()


def signal(con, iid, creators, market="ZA", run_id="detect-20260920-000000000001", metric_date=D):
    duck.load(con, "core.breakout_signals", [{
        "metric_date": metric_date, "market": market, "item_id": iid, "run_id": run_id, "creators": creators,
        "posts": creators, "evidence_post_ids": [], "top_ratio": 4.0, "held_flagged": 0,
        "rule_version": "breakout-v1"}])


def test_breakout_fires_on_a_breakout_signal_for_the_day_in_that_market_and_is_not_waiting(con):
    broke = item(con, "a")
    quiet = item(con, "b")
    elsewhere = item(con, "c")
    yesterday = item(con, "d")
    signal(con, broke, 1)
    signal(con, broke, 3, run_id="detect-20260920-000000000002")
    signal(con, elsewhere, 2, market="NG")
    signal(con, yesterday, 2, metric_date=D - timedelta(days=1))
    for key, iid in (("a", broke), ("b", quiet), ("c", elsewhere), ("d", yesterday)):
        watch(con, f"w_{key}", {"kind": "item", "item_id": iid}, {"breakout": True})
    counts = run(con)
    assert matches(con) == [("w_a", broke, "ZA", "item_id")]
    assert counts == {"watches": 4, "waiting": 0, "matches": 1, "appended": 1}


def test_the_items_statement_carries_the_days_largest_breakout_count(con):
    broke = item(con, "a")
    quiet = item(con, "b")
    signal(con, broke, 2)
    signal(con, broke, 5, run_id="detect-20260920-000000000009")
    rows = watches.query(duck.Client(con), watches.statements()["items"], {"d": D, "run_id": RUN}, "core",
                         "agent")
    assert {r["item_id"]: r["breakout_creators"] for r in rows} == {broke: 5, quiet: None}


def test_breakout_holds_only_on_a_count_of_one_or_more():
    rule = {"breakout": True}
    assert watches.fires(rule, {"breakout_creators": 1})
    assert watches.fires(rule, {"breakout_creators": 4})
    assert not watches.fires(rule, {"breakout_creators": 0})
    assert not watches.fires(rule, {"breakout_creators": None})
    assert not watches.fires(rule, {"breakout_creators": True})
    assert not watches.fires(rule, {})
    assert watches.fires({"breakout": True, "state_in": ["rising"]}, {"breakout_creators": 2, "state": "rising"})
    assert not watches.fires({"breakout": True, "state_in": ["rising"]},
                             {"breakout_creators": 2, "state": "peaking"})


def tone_view(con):
    (stmt,) = [s for s in sqlrun.agent_statements("core", "agent")
               if sqlrun.object_name(s) == "agent.v_item_tone_daily"]
    con.execute(duck.create_statement(stmt))


def toned(con, iid, d, tones, market="ZA", prefix=None):
    """Posts dated d linked to iid and sighted in market, one per tone label."""
    pids = [f"{prefix or iid[:6]}-{d:%m%d}-{market}-{n}" for n in range(len(tones))]
    duck.load(con, "core.posts", [post(pid, "cr1", d) for pid in pids])
    duck.load(con, "core.post_items", [{"post_id": pid, "item_id": iid, "via": "hashtag"} for pid in pids])
    duck.load(con, "core.post_observations", [obs(pid, d, "unbiased_rank", "sweep", market=market) for pid in pids])
    duck.load(con, "core.post_enrichment", [{"post_id": pid, "tone": t} for pid, t in zip(pids, tones)])


GLAD, CROSS, FLAT = ["celebratory"] * 4 + ["neutral"], ["angry"] * 3 + ["sad", "mixed"], ["neutral"] * 5
BEFORE = D - timedelta(days=1)


def test_tone_flip_fires_when_the_days_tone_changes_sign_from_the_day_before_in_that_market(con):
    tone_view(con)
    soured, cheered, steady, flat, thin = (item(con, k) for k in ("a", "b", "c", "d", "e"))
    elsewhere = item(con, "f", market="NG")
    toned(con, soured, BEFORE, GLAD)
    toned(con, soured, D, CROSS)
    toned(con, cheered, BEFORE, CROSS)
    toned(con, cheered, D, GLAD)
    toned(con, steady, BEFORE, GLAD)
    toned(con, steady, D, GLAD)
    toned(con, flat, BEFORE, GLAD)
    toned(con, flat, D, FLAT)                                   # zero is no sign, so not a flip
    toned(con, thin, BEFORE, GLAD)
    toned(con, thin, D, CROSS[:4])                              # four enriched posts: too few for a tone
    toned(con, elsewhere, BEFORE, GLAD, market="NG")
    toned(con, elsewhere, D, CROSS, market="ZA")                # the flip is in another market
    for key, iid in (("a", soured), ("b", cheered), ("c", steady), ("d", flat), ("e", thin)):
        watch(con, f"w_{key}", {"kind": "item", "item_id": iid}, {"tone_flip": True})
    watch(con, "w_f", {"kind": "item", "item_id": elsewhere}, {"tone_flip": True}, market="all")
    counts = run(con)
    assert matches(con) == sorted([("w_a", soured, "ZA", "item_id"), ("w_b", cheered, "ZA", "item_id")])
    assert counts == {"watches": 6, "waiting": 0, "matches": 2, "appended": 2}


def test_the_tones_statement_gives_each_items_tone_today_and_the_day_before(con):
    tone_view(con)
    soured, thin = item(con, "a"), item(con, "b")
    toned(con, soured, BEFORE, GLAD)
    toned(con, soured, D, CROSS)
    toned(con, thin, D, CROSS[:4])
    rows = watches.query(duck.Client(con), watches.statements()["tones"], {"d": D}, "core", "agent")
    got = {r["item_id"]: (r["tone_today"], r["tone_before"]) for r in rows}
    assert got == {soured: (-0.8, 0.8)}


def test_without_the_tone_view_tone_flip_waits_quietly_and_the_other_watches_still_match(con):
    iid = item(con, "a")
    watch(con, "w_tone", {"kind": "item", "item_id": iid}, {"tone_flip": True})
    watch(con, "w_rise", {"kind": "item", "item_id": iid}, {"state_in": ["rising"]})
    counts = run(con)
    assert matches(con) == [("w_rise", iid, "ZA", "item_id")]
    assert counts["matches"] == 1 and counts["unread"] == ["tone_flip"]


def test_tone_flip_holds_only_on_two_tones_of_opposite_sign():
    rule = {"tone_flip": True}
    assert watches.fires(rule, {"tone_today": -0.2, "tone_before": 0.6})
    assert watches.fires(rule, {"tone_today": 0.2, "tone_before": -1})
    assert not watches.fires(rule, {"tone_today": 0.2, "tone_before": 0.6})
    assert not watches.fires(rule, {"tone_today": 0.0, "tone_before": 0.6})
    assert not watches.fires(rule, {"tone_today": -0.4, "tone_before": None})
    assert not watches.fires(rule, {"tone_today": None, "tone_before": 0.4})
    assert not watches.fires(rule, {})
    assert not watches.fires({"tone_flip": False}, {"tone_today": -0.2, "tone_before": 0.6})


def surge_world(w):
    """Creator a has a post at four times usual on D, b one at twice usual, c only four earlier posts."""
    w.breaker("a", 2000, d=D)
    w.breaker("b", 1000, d=D)
    w.history("c", [400, 500, 600, 500])
    w.add("tiktok-c-w", "c", D, 9000)
    return w


def test_creator_surge_fires_for_a_watched_creator_with_a_post_at_three_times_usual_today(con):
    from .test_detect_breakout import World

    surge_world(World()).load(con)
    surged = item(con, "a", kind="creator", canonical_key="tiktok:a")
    usual = item(con, "b", kind="creator", canonical_key="tiktok:b")
    unjudged = item(con, "c", kind="creator", canonical_key="tiktok:c")
    tag = item(con, "a")
    watch(con, "w_a", {"kind": "creator", "value": "@a"}, {"creator_surge": True})
    watch(con, "w_b", {"kind": "creator", "value": "b"}, {"creator_surge": True})
    watch(con, "w_c", {"kind": "creator", "value": "c"}, {"creator_surge": True})
    watch(con, "w_tag", {"kind": "item", "item_id": tag}, {"creator_surge": True})
    counts = run(con)
    assert matches(con) == [("w_a", surged, "ZA", "canonical_key")]
    assert counts == {"watches": 4, "waiting": 0, "matches": 1, "appended": 1}
    assert usual and unjudged


def test_creator_rows_carry_the_days_surge_count_and_unknown_stays_null(con, monkeypatch):
    from .test_detect_breakout import World

    surge_world(World()).load(con)
    rows = [{"item_id": k, "kind": "creator", "canonical_key": f"tiktok:{k}"} for k in ("a", "b", "c")]
    rows.append({"item_id": "t", "kind": "hashtag", "canonical_key": "a"})
    watches.add_surges(duck.Client(con), D, rows, "core", "agent")
    assert {r["item_id"]: r["creator_surges"] for r in rows} == {"a": 1, "b": 0, "c": None, "t": None}


def test_without_the_creator_views_creator_surge_waits_quietly(con, monkeypatch):
    iid = item(con, "a", kind="creator", canonical_key="tiktok:a")
    watch(con, "w_surge", {"kind": "creator", "value": "a"}, {"creator_surge": True})
    watch(con, "w_rise", {"kind": "item", "item_id": iid}, {"state_in": ["rising"]})
    con.execute("DROP TABLE core.creators")
    counts = run(con)
    assert matches(con) == [("w_rise", iid, "ZA", "item_id")]
    assert counts["unread"] == ["creator_surge"]


def test_creator_surge_holds_only_on_a_count_of_one_or_more():
    rule = {"creator_surge": True}
    assert watches.fires(rule, {"creator_surges": 1})
    assert watches.fires(rule, {"creator_surges": 3})
    assert not watches.fires(rule, {"creator_surges": 0})
    assert not watches.fires(rule, {"creator_surges": None})
    assert not watches.fires(rule, {})
    assert not watches.fires({"creator_surge": "yes"}, {"creator_surges": 2})


def test_a_malformed_watch_is_skipped_and_counted_and_the_good_one_still_matches(con, monkeypatch):
    iid = item(con, "a")
    good = {"kind": "item", "item_id": iid}
    watch(con, "w_good", good, {"state_in": ["rising"]})
    duck.load(con, "agent.watches", [
        {"watch_id": "w_array", "created_at": at(D), "status_at": at(D), "who": "passcode",
         "target": json.dumps([good]), "market": "ZA", "rule": json.dumps({"state_in": ["rising"]}),
         "label": "array", "status": "active"},
        {"watch_id": "w_string", "created_at": at(D), "status_at": at(D), "who": "passcode",
         "target": json.dumps(good), "market": "ZA", "rule": json.dumps("rising"), "label": "string",
         "status": "active"}])
    real = watches.query

    def with_unparsable(client, sql, params, core, agent):
        rows = real(client, sql, params, core, agent)
        if sql == watches.statements()["watches"]:
            # BigQuery gives a JSON string scalar already parsed, so the rule arrives as bare text
            rows = sorted(rows + [{"watch_id": "w_broken", "target": good, "market": "ZA", "rule": "rising"}],
                          key=lambda w: w["watch_id"])
        return rows

    monkeypatch.setattr(watches, "query", with_unparsable)
    counts = run(con)
    assert matches(con) == [("w_good", iid, "ZA", "item_id")]
    assert counts == {"watches": 4, "waiting": 0, "matches": 1, "appended": 1,
                      "bad_watches": ["w_array", "w_broken", "w_string"]}


def test_only_the_detect_runs_item_state_for_the_day_is_read(con):
    item(con, "a", run_id="detect-20260920-older0000000")
    item(con, "b", metric_date=D - timedelta(days=1))
    for key in ("a", "b"):
        watch(con, f"w_{key}", {"kind": "item", "item_id": item_id("hashtag", key)}, {"state_in": ["rising"]})
    run(con)
    assert matches(con) == []


# Hashtag, sound and creator watches match cultural_map kind and canonical_key exactly


def test_key_watches_match_kind_and_normalised_key_exactly(con):
    tag = item(con, "amapiano", label="#Amapiano")
    same_key_sound = item(con, "amapiano", kind="sound", canonical_key="tiktok:amapiano")
    longer_tag = item(con, "amapianoza")
    creator = item(con, "dj_x", kind="creator", canonical_key="tiktok:dj_x")
    sound = item(con, "s", kind="sound", canonical_key="tiktok:7301AbC")
    watch(con, "w_tag", {"kind": "hashtag", "value": " #AMAPIANO "}, {"state_in": ["rising"]})
    watch(con, "w_creator", {"kind": "creator", "value": "@DJ_X"}, {"state_in": ["rising"]})
    watch(con, "w_creator_full", {"kind": "creator", "value": "tiktok:dj_x"}, {"state_in": ["rising"]})
    watch(con, "w_sound", {"kind": "sound", "value": "7301AbC"}, {"state_in": ["rising"]})
    watch(con, "w_sound_case", {"kind": "sound", "value": "7301abc"}, {"state_in": ["rising"]})
    run(con)
    assert matches(con) == sorted([
        ("w_tag", tag, "ZA", "canonical_key"),
        ("w_creator", creator, "ZA", "canonical_key"),
        ("w_creator_full", creator, "ZA", "canonical_key"),
        ("w_sound", sound, "ZA", "canonical_key")])
    assert same_key_sound and longer_tag


# (c) Brand and query watches match label, key or alias on word boundaries, never inside another word


def test_brand_watch_matches_label_key_and_alias_on_word_boundaries(con):
    by_label = item(con, "i1", kind="topic", label="Nando's Friday Special", canonical_key="friday special")
    by_key = item(con, "i2", kind="topic", label="Peri peri", canonical_key="nando's peri peri")
    by_alias = item(con, "i3", kind="topic", label="Chicken night", canonical_key="chicken night",
                    aliases=["#Nando's", "chicken"])
    inside = item(con, "i4", kind="topic", label="Supernando's club", canonical_key="supernando's club")
    after = item(con, "i5", kind="topic", label="Nando'ss", canonical_key="nandoss")
    watch(con, "w_brand", {"kind": "brand", "value": "NANDO'S"}, {"state_in": ["rising"]})
    run(con)
    assert matches(con) == sorted([
        ("w_brand", by_label, "ZA", "label_words"),
        ("w_brand", by_key, "ZA", "key_words"),
        ("w_brand", by_alias, "ZA", "alias_words")])
    assert inside and after


def test_query_watch_matches_a_phrase_with_any_spacing_but_not_a_longer_word(con):
    phrase = item(con, "i1", kind="topic", label="Cape   Town jazz", canonical_key="cape town jazz")
    longer = item(con, "i2", kind="topic", label="Cape Townships", canonical_key="cape townships")
    split = item(con, "i3", kind="topic", label="Cape and Town", canonical_key="cape and town")
    watch(con, "w_q", {"kind": "query", "value": "cape  town"}, {"reach_over": 10})
    run(con)
    assert matches(con) == [("w_q", phrase, "ZA", "label_words")]
    assert longer and split


def test_a_word_watch_never_matches_a_regex_like_value_as_a_pattern(con):
    item(con, "i1", kind="topic", label="abc", canonical_key="abc")
    dotted = item(con, "i2", kind="topic", label="a.c news", canonical_key="a.c news")
    watch(con, "w_q", {"kind": "query", "value": "a.c"}, {"state_in": ["rising"]})
    run(con)
    assert matches(con) == [("w_q", dotted, "ZA", "label_words")]


# (d) Paused watches and watches in other markets never match


def test_paused_watches_never_match_and_the_latest_row_decides(con):
    iid = item(con, "a")
    target, rule = {"kind": "item", "item_id": iid}, {"state_in": ["rising"]}
    watch(con, "w_paused", target, rule, status="active", hour=9)
    watch(con, "w_paused", target, rule, status="paused", hour=10)
    watch(con, "w_resumed", target, rule, status="paused", hour=9)
    watch(con, "w_resumed", target, rule, status="active", hour=10)
    run(con)
    assert matches(con) == [("w_resumed", iid, "ZA", "item_id")]


def test_market_watches_match_their_market_only_and_all_matches_every_market(con):
    za = item(con, "amapiano", market="ZA")
    item(con, "amapiano", market="NG")
    watch(con, "w_za", {"kind": "hashtag", "value": "amapiano"}, {"state_in": ["rising"]}, market="ZA")
    watch(con, "w_ke", {"kind": "hashtag", "value": "amapiano"}, {"state_in": ["rising"]}, market="KE")
    watch(con, "w_all", {"kind": "hashtag", "value": "amapiano"}, {"state_in": ["rising"]}, market="all")
    run(con)
    assert matches(con) == [("w_all", za, "NG", "canonical_key"), ("w_all", za, "ZA", "canonical_key"),
                            ("w_za", za, "ZA", "canonical_key")]


# (e) A rerun on the same day adds no duplicate rows


def test_a_rerun_on_the_same_day_appends_nothing_new(con):
    iid = item(con, "a")
    watch(con, "w_1", {"kind": "item", "item_id": iid}, {"state_in": ["rising"]})
    watch(con, "w_2", {"kind": "brand", "value": "a"}, {"state_in": ["rising"]})
    first = run(con, run_id=STEP)
    second = run(con, run_id="watch-20260920-000000000002")
    assert first["appended"] == 2 and second == {"watches": 2, "waiting": 0, "matches": 2, "appended": 0}
    rows = duck.query(con, "SELECT m.run_id FROM {agent}.watch_matches m")
    assert [r["run_id"] for r in rows] == [STEP, STEP]


def test_a_new_match_on_a_rerun_is_appended_beside_the_old_ones(con):
    a = item(con, "a")
    watch(con, "w_1", {"kind": "hashtag", "value": "a"}, {"state_in": ["rising"]}, market="all")
    run(con, run_id=STEP)
    item(con, "a", market="KE")
    counts = run(con, run_id="watch-20260920-000000000002")
    assert counts["appended"] == 1
    assert matches(con) == [("w_1", a, "KE", "canonical_key"), ("w_1", a, "ZA", "canonical_key")]


def test_the_next_day_matches_again(con):
    iid = item(con, "a")
    item(con, "a", metric_date=D + timedelta(days=1), run_id="detect-20260921-000000000001")
    watch(con, "w_1", {"kind": "item", "item_id": iid}, {"state_in": ["rising"]})
    run(con)
    watches.run_watches(duck.Client(con), D + timedelta(days=1), "detect-20260921-000000000001",
                        "watch-20260921-000000000001", core="core", agent="agent")
    rows = duck.query(con, "SELECT m.match_date FROM {agent}.watch_matches m ORDER BY m.match_date")
    assert [r["match_date"] for r in rows] == [D, D + timedelta(days=1)]


# (f) Ineligible, likely coordinated, generic, paid-led and not-local items never produce a match


@pytest.mark.parametrize("held", [
    {"eligible": False},
    {"eligible": None},
    {"authenticity": "likely_coordinated"},
    {"map_status": "generic"},
    {"map_status": "rejected"},
    {"sponsored_share": 0.5},
    {"geo_status": "not_local"},
], ids=["ineligible", "eligible_null", "likely_coordinated", "generic", "rejected", "paid_led", "not_local"])
def test_items_the_gate_holds_never_match_any_watch_kind(con, held):
    iid = item(con, "amapiano", label="Amapiano", **held)
    watch(con, "w_item", {"kind": "item", "item_id": iid}, {"state_in": ["rising"]})
    watch(con, "w_tag", {"kind": "hashtag", "value": "amapiano"}, {"reach_over": 1})
    watch(con, "w_brand", {"kind": "brand", "value": "amapiano"}, {"ratio_over": 1})
    counts = run(con)
    assert matches(con) == [] and counts["matches"] == 0


def test_a_clear_item_beside_a_held_one_still_matches(con):
    ok = item(con, "amapiano", market="ZA")
    item(con, "amapiano", market="NG", authenticity="likely_coordinated")
    watch(con, "w_all", {"kind": "brand", "value": "amapiano"}, {"state_in": ["rising"]}, market="all")
    run(con)
    assert matches(con) == [("w_all", ok, "ZA", "label_words")]


# Pure helpers


def test_word_method_prefers_label_then_key_then_alias():
    assert watches.word_method("amapiano", "Amapiano", "amapiano", ["amapiano"]) == "label_words"
    assert watches.word_method("amapiano", "x", "amapiano", ["amapiano"]) == "key_words"
    assert watches.word_method("amapiano", "x", "y", ["#AMAPIANO"]) == "alias_words"
    assert watches.word_method("amapiano", "amapianos", "y", None) is None
    assert watches.word_method("", "anything", "y", None) is None


def test_json_columns_are_read_whether_the_client_gives_text_or_objects():
    assert watches.as_obj('{"kind": "item"}') == {"kind": "item"}
    assert watches.as_obj({"kind": "item"}) == {"kind": "item"}
    assert watches.as_obj(None) == {}


# Staging dry runs: BigQuery parses and plans each statement without running it, so nothing is written or billed.

staging = pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")


def dry_run(client, sql, arrays=()):
    from google.cloud import bigquery

    config = bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False,
        query_parameters=[sqlrun._param(k, v) for k, v in {"d": D, "run_id": RUN}.items()] + list(arrays))
    assert client.query(sqlrun.render(sql), job_config=config).dry_run


@staging
def test_the_items_statement_dry_runs_on_staging():
    from google.cloud import bigquery

    dry_run(bigquery.Client(project="ogilvy-trends-v2"), watches.statements()["items"])


@staging
def test_the_watch_table_statements_dry_run_on_staging_once_l1s_tables_exist():
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery

    client = bigquery.Client(project="ogilvy-trends-v2")
    for table in ("watches", "watch_matches", "v_watches_current"):
        try:
            client.get_table(f"ogilvy-trends-v2.{sqlrun.AGENT}.{table}")
        except NotFound:
            pytest.skip(f"{sqlrun.AGENT}.{table} does not exist on staging yet (lane L1)")
    st = watches.statements()
    rows = [{"watch_id": "w_1", "match_date": D, "item_id": "i1", "market": "ZA", "method": "item_id",
             "run_id": STEP}]
    dry_run(client, st["watches"])
    dry_run(client, st["appended"])
    dry_run(client, st["append"], [aggregate._struct_array("rows", rows, watches.MATCH_FIELDS)])
