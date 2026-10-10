"""Tests for the morning brief's SQL evidence pack (core/brief/evidence.py), run on DuckDB fixture tables."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from core.brief import evidence
from core.trust.claims import check_answer
from core.detect.tests import duck
from core.detect.tests.fixtures import D, day, health, item_daily, rid, run, counter

UTC = timezone.utc
DETECT = rid("detect", D)
SERIES = "i1|ZA|tt_board|p1"
CONTRACT_FIELDS = {"id", "platform", "handle", "url", "posted_at", "market", "text", "engagement", "flags",
                   "thumbnail_url", "duration_s", "creator_tier", "source_market", "outlet_class"}


def utc(d, hour, minute=0):
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC)


def source_sighting(market, obs_date, *, route="tiktok/trending", protocol="tiktok/trending?feed=local"):
    return {"source_market": market, "source_region": None, "route": route, "protocol": protocol,
            "observed_at": utc(obs_date, 10), "obs_date": obs_date}


def add_source_row(con, pid, markets, sightings):
    duck.load(con, "core.source_market_fixture", [{"post_id": pid, "source_markets": markets,
                                                    "source_sightings": sightings}])


def state_row(**over):
    row = {"metric_date": D, "market": "ZA", "item_id": "i1", "kind": "hashtag", "state_raw": "emerging",
           "state": "emerging", "untested": False, "main_series_id": SERIES, "main_ratio": 2.8,
           "creators3": 31, "posts3": 84, "run_id": DETECT, "rule_version": "r1"}
    row.update(over)
    return row


def world(**state):
    con = duck.connect()
    duck.load(con, "agent.runs", [run("detect", D)])
    duck.load(con, "core.item_state", [state_row(**state)])
    return con


def current(con, market="ZA"):
    return duck.query(con, "SELECT s.* FROM {core}.v_item_state_current s WHERE s.item_id = 'i1' AND s.market = @m",
                      {"m": market})[0]


def build(con, market="ZA"):
    client = duck.Client(con)
    pack, spark, rerun = evidence.build_pack(client, current(con, market), D, market, core="core", agent="agent")
    return pack, spark, rerun, client


def add_post(con, pid, creator, published, engagement=0, *, sightings=(("sweep", "unbiased_rank"),),
             market="ZA", seen=D, item="i1", route=None, protocol=None, **fields):
    if fields.get("geo_market"):
        fields.setdefault("geo_source", "ext_region")  # a source collect treats as known, unless the test says
    post = {"post_id": pid, "platform": "tiktok", "creator_id": creator, "creator_tier_at_post": "micro",
            "published_at": published, "post_date": published.date(), "engagement": engagement,
            "text": f"caption {pid}", **fields}
    duck.load(con, "core.posts", [post])
    duck.load(con, "core.post_items", [{"post_id": pid, "item_id": item, "via": "hashtag"}])
    for lane, lane_class in sightings:
        duck.load(con, "core.post_observations", [{
            "post_id": pid, "observed_at": utc(seen, 10), "observed_date": seen, "market": market,
            "platform": "tiktok", "route": route, "protocol": protocol,
            "lane": lane, "lane_class": lane_class, "run_id": rid("collect", seen)}])


def ids(pack):
    return [e["id"] for e in pack["evidence"]]


def canonical_hash(rows):
    text = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def number(pack, unit):
    return next(n for n in pack["numbers"] if n["unit"] == unit)


# Evidence posts


def test_evidence_comes_only_from_the_last_seven_market_local_days():
    con = world()
    # ZA is UTC+2: the window is 14 September 00:00 to 20 September 23:59 SAST.
    add_post(con, "p_edge_in", "c1", utc(day(7), 22, 0))
    add_post(con, "p_edge_out", "c2", utc(day(7), 21, 59))
    add_post(con, "p_last", "c3", utc(D, 21, 59))
    # Each bound on its own: published on 21 September local though sighted on D, and sighted only after D.
    add_post(con, "p_next_day", "c4", utc(D, 22, 0))
    add_post(con, "p_seen_later", "c6", utc(day(1), 9), seen=D + timedelta(days=1))
    add_post(con, "p_old", "c5", utc(day(8), 9))
    pack, *_ = build(con)
    assert sorted(ids(pack)) == ["p_edge_in", "p_last"]
    assert next(e for e in pack["evidence"] if e["id"] == "p_edge_in")["posted_at"] == "2026-09-14T00:00:00+02:00"


def test_evidence_never_comes_from_placebo_or_agent_live_lanes():
    con = world()
    add_post(con, "p_placebo", "c1", utc(day(1), 9), 900, sightings=[("placebo", "search_presence")])
    add_post(con, "p_live", "c2", utc(day(1), 9), 900, sightings=[("agent_live", "search_presence")])
    add_post(con, "p_both", "c3", utc(day(1), 9), 5,
             sightings=[("placebo", "search_presence"), ("sweep", "unbiased_rank")])
    add_post(con, "p_legacy", "c4", utc(day(1), 9), 900, sightings=[("legacy", "legacy")])
    add_post(con, "p_ng", "c5", utc(day(1), 9), 900, market="NG")
    add_post(con, "p_other_item", "c6", utc(day(1), 9), 900, item="i2")
    pack, *_ = build(con)
    assert ids(pack) == ["p_both"]


def test_at_most_two_posts_per_creator_and_twelve_in_all():
    con = world()
    for n, eng in enumerate([1000, 900, 800, 700]):
        add_post(con, f"c1_{n}", "c1", utc(day(1), 9), eng)
    for n in range(2, 16):
        add_post(con, f"c{n}_0", f"c{n}", utc(day(2), 9), 500 - n)
    pack, *_ = build(con)
    assert ids(pack) == ["c1_0", "c1_1"] + [f"c{n}_0" for n in range(2, 12)]


def test_measured_lanes_rank_before_search_presence_then_engagement():
    con = world()
    add_post(con, "p_search", "c1", utc(day(1), 9), 10000, sightings=[("exploration", "search_presence")])
    add_post(con, "p_rank", "c2", utc(day(1), 9), 5)
    add_post(con, "p_panel", "c3", utc(day(1), 9), 50, sightings=[("panel", "panel")])
    add_post(con, "p_mixed", "c4", utc(day(1), 9), 20,
             sightings=[("exploration", "search_presence"), ("panel", "panel")])
    add_post(con, "p_search_low", "c5", utc(day(1), 9), 3, sightings=[("exploration", "search_presence")])
    pack, *_ = build(con)
    assert ids(pack) == ["p_panel", "p_mixed", "p_rank", "p_search", "p_search_low"]


def test_evidence_record_has_the_contract_fields_and_flags():
    con = world()
    transcript = "sawubona " * 50
    add_post(con, "p1", "c1", utc(day(1), 9), 500, text=None, transcript=transcript, url="https://t.example/p1",
             views=184000, likes=9100, comments=300, shares=120, thumbnail_url="https://t.example/p1.jpg",
             duration_s=21.0, creator_tier_at_post="mid", geo_market="ZA", geo_confidence=0.9,
             geo_source="ext_region")
    add_post(con, "p2", "c2", utc(day(1), 10), 400, text="caption words", transcript="spoken words")
    duck.load(con, "core.creators", [
        {"creator_id": "c1", "platform": "tiktok", "handle": "@maker_one", "coord_score": 2},
        {"creator_id": "c2", "platform": "tiktok", "handle": "@maker_two", "coord_score": 0}])
    duck.load(con, "core.post_enrichment", [{"post_id": "p1", "sponsored": True, "near_dup_size": 3},
                                            {"post_id": "p2", "sponsored": False, "near_dup_size": 2}])
    pack, *_ = build(con)
    first, second = pack["evidence"]
    assert first == {
        "id": "p1", "platform": "tiktok", "handle": "@maker_one", "url": "https://t.example/p1",
        "posted_at": "2026-09-19T11:00:00+02:00", "market": "ZA", "source_market": None,
        "text": transcript[:280],
        "quote_text": transcript,
        "engagement": {"views": 184000, "likes": 9100, "comments": 300, "shares": 120},
        "flags": ["flagged", "sponsored", "near_duplicate"], "thumbnail_url": "https://t.example/p1.jpg",
        "duration_s": 21.0, "creator_tier": "mid", "sponsor_checked": True, "outlet_class": "creator",
    }
    assert set(first) == CONTRACT_FIELDS | {"quote_text", "sponsor_checked"}
    assert len(first["text"]) == 280 and len(first["quote_text"]) == 450
    assert second["text"] == "caption words" and second["quote_text"] == "caption words"
    assert second["market"] is None and second["flags"] == ["market_assumed"]
    assert second["sponsor_checked"] is True


@pytest.mark.parametrize("market,offset", [("NG", "+01:00"), ("KE", "+03:00")])
def test_posted_at_carries_the_market_offset(market, offset):
    con = world(market=market)
    add_post(con, "p1", "c1", utc(day(1), 9), market=market, geo_market=market, geo_confidence=0.9)
    pack, *_ = build(con, market)
    hour = 9 + int(offset[1:3])
    assert pack["evidence"][0]["posted_at"] == f"2026-09-19T{hour:02d}:00:00{offset}"
    assert pack["evidence"][0]["market"] == market


# Pinned numbers


def test_numbers_are_pinned_to_the_detect_run_and_a_hash_of_their_result():
    con = world()
    pack, _, _, client = build(con)
    assert set(pack) == {"evidence", "numbers", "facts"}
    assert [(n["unit"], n["value"]) for n in pack["numbers"]] == [
        ("creators in 3 days", 31), ("posts in 3 days", 84), ("times usual", 2.8)]
    for n, name in zip(pack["numbers"], ["creators3", "posts3", "main_ratio"]):
        assert n["run_id"] == DETECT
        assert n["result_hash"] == canonical_hash([{"value": n["value"]}])
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", n["result_hash"])
        assert isinstance(n["query_id"], str) and name in n["query_id"]
    assert len({n["query_id"] for n in pack["numbers"]}) == 3
    # DATA.md 3.1: a re-run pinned to a run_id reads item_state itself, not the latest-good-run view.
    number_sql = [sql for sql in client.sql if "s.run_id = @run_id" in sql]
    assert len(number_sql) == 3
    assert all("FROM core.item_state s" in sql and "v_item_state_current" not in sql for sql in number_sql)


def test_untested_item_states_no_times_usual():
    con = world(untested=True, state="new_to_42", state_raw="new_to_42")
    pack, *_ = build(con)
    assert [n["unit"] for n in pack["numbers"]] == ["creators in 3 days", "posts in 3 days"]


def test_numbers_read_the_pinned_run_not_a_failed_later_run():
    con = world()
    duck.load(con, "agent.runs", [run("detect", D, run_id="detect-retry", status="failed", hour=18)])
    duck.load(con, "core.item_state", [state_row(run_id="detect-retry", creators3=99, posts3=99)])
    pack, *_ = build(con)
    assert number(pack, "creators in 3 days")["value"] == 31
    assert number(pack, "posts in 3 days")["value"] == 84


def test_rerun_reads_live_data_while_item_state_stays_pinned_by_run_id():
    con = world()
    pack, _, rerun, _ = build(con)
    creators = number(pack, "creators in 3 days")
    assert rerun(creators) == 31
    assert rerun(number(pack, "times usual")) == pytest.approx(2.8)
    con.execute("UPDATE core.item_state SET creators3 = 40 WHERE run_id = ?", [DETECT])
    duck.load(con, "agent.runs", [run("detect", D, run_id="detect-retry", status="failed", hour=18)])
    duck.load(con, "core.item_state", [state_row(run_id="detect-retry", creators3=99)])
    assert rerun(creators) == 40
    assert creators["value"] == 31
    assert rerun(number(pack, "posts in 3 days")) == 84
    with pytest.raises(KeyError):
        rerun({**creators, "query_id": "q_unknown"})


def test_rerun_reproduces_the_pinned_run_after_a_newer_good_detect_run():
    con = world()
    pack, _, rerun, _ = build(con)
    duck.load(con, "agent.runs", [run("detect", D, run_id="detect-newer", hour=18)])
    duck.load(con, "core.item_state", [state_row(run_id="detect-newer", creators3=99, posts3=99, main_ratio=9.9)])
    assert rerun(number(pack, "creators in 3 days")) == 31
    assert rerun(number(pack, "posts in 3 days")) == 84
    assert rerun(number(pack, "times usual")) == pytest.approx(2.8)


# Sparkline


def test_sparkline_is_the_main_series_over_fourteen_days_with_gaps():
    con = world()
    for i in range(-1, 11):
        d = day(i)
        duck.load(con, "agent.runs", [run("collect", d)])
        duck.load(con, "core.collection_health", [health("tt_board", d, valid=(i != 5))])
    duck.load(con, "core.item_counter_daily", [
        counter("i1", "tt_board", day(10), 2), counter("i1", "tt_board", day(5), 7),
        counter("i1", "tt_board", day(3), 5), counter("i1", "tt_board", day(0), 4),
        counter("i1", "tt_board", day(-1), 9), counter("i2", "tt_board", day(1), 6)])
    _, spark, *_ = build(con)
    expected = {day(10): 2, day(3): 5, day(0): 4}
    want = [None, None, None] + [None if i == 5 else expected.get(day(i), 0) for i in range(10, -1, -1)]
    assert spark["unit"] == "list appearances a day"
    assert [p["date"] for p in spark["points"]] == [day(i).isoformat() for i in range(13, -1, -1)]
    assert [p["value"] for p in spark["points"]] == want
    assert all(p["expected_low"] is None and p["expected_high"] is None for p in spark["points"])


def test_no_sparkline_without_a_main_series():
    con = world(main_series_id=None)
    _, spark, *_ = build(con)
    assert spark is None


# Facts


def test_facts_are_short_plain_lines():
    con = world()
    duck.load(con, "agent.runs", [run("aggregate", day(9), status="failed"), run("aggregate", day(6)),
                                  run("aggregate", day(2)), run("aggregate", day(-1))])
    duck.load(con, "core.item_daily", [item_daily("i1", day(9), 3), item_daily("i1", day(6), 2),
                                       item_daily("i1", day(2), 4), item_daily("i1", day(-1), 5),
                                       item_daily("i1", day(8), 1, market="NG")])
    pack, *_ = build(con)
    assert pack["facts"] == ["State: Emerging", "31 creators and 84 posts in 3 days",
                             "First seen in ZA on 2026-09-14"]


def test_first_seen_ignores_rows_after_the_brief_date():
    con = world()
    duck.load(con, "agent.runs", [run("aggregate", day(-1)), run("aggregate", day(-2))])
    duck.load(con, "core.item_daily", [item_daily("i1", day(-1), 5), item_daily("i1", day(-2), 5)])
    pack, *_ = build(con)
    assert pack["facts"] == ["State: Emerging", "31 creators and 84 posts in 3 days"]


def test_each_local_post_gets_a_day_line_in_market_local_time_and_no_other_post_does():
    """BR-2 (4 Oct): the writer may date a cause a local post names by its posted_at (law 12), so the facts carry
    each local post's weekday and market-local date. A post with no known location and no sighting in the
    market's feeds is not local and gets no line."""
    con = world()
    add_post(con, "p_za", "c1", utc(day(1), 23), 30, geo_market="ZA", geo_confidence=0.9)
    add_post(con, "p_feed", "c2", utc(day(2), 9), 20)
    add_source_row(con, "p_feed", ["ZA"], [source_sighting("ZA", day(2))])
    add_post(con, "p_none", "c3", utc(day(3), 9), 10)
    pack, *_ = build(con)
    days = [f for f in pack["facts"] if f.startswith("Post ")]
    # 23:00 UTC on Saturday 19 September is 01:00 SAST on Sunday 20 September.
    assert days == ["Post p_za: posted on Sunday 2026-09-20", "Post p_feed: posted on Friday 2026-09-18"]
    assert pack["facts"][:2] == ["State: Emerging", "31 creators and 84 posts in 3 days"]


def test_a_day_line_names_a_calendar_moment_only_when_the_market_has_one_on_that_date():
    con = world()
    add_post(con, "p_today", "c1", utc(D, 8), 30, geo_market="ZA", geo_confidence=0.9)
    add_post(con, "p_before", "c2", utc(day(1), 8), 20, geo_market="ZA", geo_confidence=0.9)
    moments = [{"date": D.isoformat(), "name": "Fixture Day", "kind": "holiday", "source": "calendar",
                "item_ids": []},
               {"date": day(3).isoformat(), "name": "Not a post day", "kind": "holiday", "source": "calendar",
                "item_ids": []}]
    client = duck.Client(con)
    pack, *_ = evidence.build_pack(client, current(con), D, "ZA", core="core", agent="agent", moments=moments)
    days = [f for f in pack["facts"] if f.startswith("Post ")]
    assert days == ["Post p_today: posted on Sunday 2026-09-20, the calendar's Fixture Day",
                    "Post p_before: posted on Saturday 2026-09-19"]
    assert not any("Not a post day" in f for f in pack["facts"])


def test_the_day_lines_never_widen_what_the_why_now_may_rest_on():
    from core.brief import explain

    text = " ".join(explain.WRITER_SYSTEM.split())
    assert ("The why-now clause rests on posts its claim cites, never on the facts lines, a search line or a number "
            "alone.") in text


# The SQL file


def test_evidence_sql_is_read_only_bigquery_with_dataset_placeholders():
    text = evidence.SQL.read_text(encoding="utf-8")
    assert set(evidence.QUERIES) >= {"evidence", "creators3", "posts3", "main_ratio", "sparkline", "first_seen"}
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|TRUNCATE)\b", text, re.IGNORECASE)
    for sql in evidence.QUERIES.values():
        assert "{core}." in sql and "intelligence_42" not in sql


def k3_verdicts(pack, market, cites, text="Creators post about the dance."):
    claim = {"text": text, "label": "single_source", "kind": "observation", "quotes": [], "numbers": []}
    answer = {"status": "complete", "short_answer": "", "evidence": pack["evidence"], "so_what": [],
              "watch_next": [], "gaps": [], "context": "",
              "claims": [dict(claim, id=cid, evidence_ids=ids) for cid, ids in cites.items()]}
    _, rows = check_answer(answer, window_start="2026-09-14T00:00:00+02:00",
                           window_end="2026-09-20T23:59:59+02:00", market=market)
    return {r["claim_id"]: r["verdict"] for r in rows if r["rule"] == "K3"}


def test_a_post_carries_only_its_located_market_so_k3_cuts_a_claim_citing_one_placed_elsewhere():
    con = world()
    add_post(con, "p_ng", "c1", utc(day(1), 9), 50, geo_market="NG", geo_confidence=0.9)
    add_post(con, "p_weak", "c2", utc(day(1), 9), 40, geo_market="NG", geo_confidence=0.5)
    add_post(con, "p_edge", "c3", utc(day(1), 9), 30, geo_market="KE", geo_confidence=0.7)
    add_post(con, "p_local", "c4", utc(day(1), 9), 20)
    add_post(con, "p_home", "c5", utc(day(1), 9), 10, geo_market="ZA", geo_confidence=0.8)
    pack, *_ = build(con)
    assert {e["id"]: e["market"] for e in pack["evidence"]} == {
        "p_ng": "NG", "p_weak": None, "p_edge": "KE", "p_local": None, "p_home": "ZA"}
    assert k3_verdicts(pack, "ZA", {"c_ng": ["p_ng"], "c_local": ["p_local"], "c_weak": ["p_weak"],
                                    "c_home": ["p_home"]}) == {
        "c_ng": "cut", "c_local": "pass", "c_weak": "pass", "c_home": "pass"}


def test_a_post_sighted_in_kenya_with_no_known_location_has_no_market_and_is_flagged_assumed():
    con = world(market="KE")
    add_post(con, "p_none", "c1", utc(day(1), 9), 50, market="KE")
    add_post(con, "p_weak", "c2", utc(day(1), 9), 40, market="KE", geo_market="KE", geo_confidence=0.69)
    add_post(con, "p_lang", "c3", utc(day(1), 9), 30, market="KE", geo_market="KE", geo_confidence=0.8,
             geo_source="language")
    add_post(con, "p_ke", "c4", utc(day(1), 9), 20, market="KE", geo_market="KE", geo_confidence=0.8,
             geo_source="ext_region")
    pack, *_ = build(con, "KE")
    by_id = {e["id"]: e for e in pack["evidence"]}
    for pid in ("p_none", "p_weak", "p_lang"):
        assert by_id[pid]["market"] is None, pid
        assert "market_assumed" in by_id[pid]["flags"], pid
    assert by_id["p_ke"]["market"] == "KE" and "market_assumed" not in by_id["p_ke"]["flags"]


def test_creators_in_kenya_citing_only_unlocated_posts_is_cut_and_passes_on_a_post_located_in_kenya():
    con = world(market="KE")
    add_post(con, "p_yt", "c1", utc(day(1), 9), 50, market="KE")
    add_post(con, "p_ke", "c2", utc(day(1), 9), 40, market="KE", geo_market="KE", geo_confidence=0.8)
    pack, *_ = build(con, "KE")
    text = "The hashtag spread among sports creators and commentary accounts in Kenya."
    assert k3_verdicts(pack, "KE", {"c_unlocated": ["p_yt"], "c_located": ["p_ke"]}, text) == {
        "c_unlocated": "cut", "c_located": "pass"}


def test_duplicate_enrichment_rows_never_yield_two_evidence_records():
    con = world()
    add_post(con, "p_dup", "c1", utc(day(1), 9), 50)
    duck.load(con, "core.post_enrichment", [{"post_id": "p_dup", "sponsored": False, "near_dup_size": 1},
                                            {"post_id": "p_dup", "sponsored": True, "near_dup_size": 4}])
    pack, *_ = build(con)
    assert ids(pack) == ["p_dup"]
    assert set(pack["evidence"][0]["flags"]) == {"sponsored", "near_duplicate", "market_assumed"}


def test_a_location_counts_only_from_the_sources_collect_treats_as_known():
    con = world(market="KE")
    for pid, source in (("p_region", "ext_region"), ("p_home", "home_market"), ("p_place", "place_mention"),
                        ("p_none", None), ("p_lang", "language"), ("p_other", "vendor_guess")):
        add_post(con, pid, f"c_{pid}", utc(day(1), 9), 10, market="KE", geo_market="KE", geo_confidence=0.9,
                 geo_source=source)
    pack, *_ = build(con, "KE")
    assert {e["id"]: e["market"] for e in pack["evidence"]} == {
        "p_region": "KE", "p_home": "KE", "p_place": "KE", "p_none": None, "p_lang": None, "p_other": None}


def source_market_values(pack):
    return {e["id"]: e["source_market"] for e in pack["evidence"]}


def test_source_market_requires_a_sighting_inside_the_brief_window():
    con = world(market="KE")
    add_post(con, "p_inside", "c1", utc(day(1), 9), market="KE")
    add_post(con, "p_stale", "c2", utc(day(1), 9), market="KE")
    add_source_row(con, "p_inside", ["KE"], [source_sighting("KE", day(1))])
    add_source_row(con, "p_stale", ["NG"], [source_sighting("NG", day(8))])

    pack, *_ = build(con, "KE")

    assert source_market_values(pack) == {"p_inside": "KE", "p_stale": None}


def test_source_market_selects_the_brief_market_only_when_sighted_and_otherwise_needs_one_market():
    con = world(market="KE")
    add_post(con, "p_ke_ng", "c1", utc(day(1), 9), market="KE", geo_market="ZA", geo_confidence=0.9,
             geo_source="ext_region")
    add_post(con, "p_ng", "c2", utc(day(1), 9), market="KE")
    add_post(con, "p_za_ng", "c3", utc(day(1), 9), market="KE")
    add_source_row(con, "p_ke_ng", ["KE", "NG"], [source_sighting("KE", day(1)),
                                                      source_sighting("NG", day(1))])
    add_source_row(con, "p_ng", ["NG"], [source_sighting("NG", day(1))])
    add_source_row(con, "p_za_ng", ["NG", "ZA"], [source_sighting("NG", day(1)),
                                                    source_sighting("ZA", day(1))])

    pack, *_ = build(con, "KE")

    assert source_market_values(pack) == {"p_ke_ng": "KE", "p_ng": "NG", "p_za_ng": None}
    assert {e["id"]: e["market"] for e in pack["evidence"]} == {
        "p_ke_ng": "ZA", "p_ng": None, "p_za_ng": None}


def test_generic_route_and_all_history_market_index_do_not_prove_a_current_source_market():
    con = world(market="KE")
    add_post(con, "p_generic", "c1", utc(day(1), 9), market="KE", route="tiktok/search/top",
             protocol="tiktok/search/top")
    add_post(con, "p_unindexed", "c2", utc(day(1), 9), market="KE", route="search/multi",
             protocol="search/multi?platforms=instagram,youtube")
    add_source_row(con, "p_generic", ["KE"], [source_sighting("KE", day(8), route="tiktok/search/top",
                                                                  protocol="tiktok/search/top")])

    pack, *_ = build(con, "KE")

    assert source_market_values(pack) == {"p_generic": None, "p_unindexed": None}


def test_null_or_foreign_source_market_sightings_never_become_a_brief_market():
    con = world(market="KE")
    add_post(con, "p_null", "c1", utc(day(1), 9), market="KE")
    add_post(con, "p_foreign", "c2", utc(day(1), 9), market="KE")
    add_source_row(con, "p_null", [], [source_sighting(None, day(1))])
    add_source_row(con, "p_foreign", ["GLOBAL"], [source_sighting("GLOBAL", day(1))])

    pack, *_ = build(con, "KE")

    assert source_market_values(pack) == {"p_null": None, "p_foreign": None}


def test_creator_profile_join_stays_platform_specific_and_coordination_hold_stays_global():
    con = world()
    add_post(con, "p_shared", "shared", utc(day(1), 9), 50)
    duck.load(con, "core.creators", [
        {"creator_id": "shared", "platform": "tiktok", "handle": "@tiktok", "coord_score": 0},
        {"creator_id": "shared", "platform": "instagram", "handle": "@instagram", "coord_score": 1},
    ])

    pack, *_ = build(con)

    assert ids(pack) == ["p_shared"]
    assert pack["evidence"][0]["handle"] == "@tiktok"
    assert "flagged" in pack["evidence"][0]["flags"]


# The suppression list (SETUP.md data protection): a suppressed creator's posts never enter the pack


def suppress(con, *creator_ids):
    duck.load(con, "core.suppressed_fixture", [{"creator_id": c} for c in creator_ids])


def test_a_suppressed_creators_posts_leave_the_pack_and_other_posts_fill_it():
    con = world()
    add_post(con, "p_hidden_0", "c_hidden", utc(day(1), 9), 1000)
    add_post(con, "p_hidden_1", "c_hidden", utc(day(1), 9), 990)
    for n in range(2, 14):
        add_post(con, f"c{n}_0", f"c{n}", utc(day(2), 9), 500 - n)
    suppress(con, "c_hidden")
    pack, *_ = build(con)
    assert ids(pack) == [f"c{n}_0" for n in range(2, 14)]


def test_a_post_by_another_account_with_a_suppressed_handle_leaves_and_the_handle_is_masked_in_other_text():
    con = world()
    add_post(con, "p_hidden", "c_hidden", utc(day(1), 9), 900)
    add_post(con, "p_same_handle", "c_twin", utc(day(1), 9), 800)
    add_post(con, "p_mention", "c3", utc(day(1), 9), 700, text="Learnt this from @Kay_Dance last night")
    add_post(con, "p_other", "c4", utc(day(1), 9), 600)
    duck.load(con, "core.creators", [
        {"creator_id": "c_hidden", "platform": "tiktok", "handle": "@Kay_Dance", "coord_score": 0},
        {"creator_id": "c_twin", "platform": "tiktok", "handle": "kay_dance", "coord_score": 0},
        {"creator_id": "c3", "platform": "tiktok", "handle": "@third", "coord_score": 0},
        {"creator_id": "c4", "platform": "tiktok", "handle": "@fourth", "coord_score": 0}])
    suppress(con, "c_hidden")
    pack, *_ = build(con)
    assert ids(pack) == ["p_mention", "p_other"]
    mention = pack["evidence"][0]
    assert "kay_dance" not in json.dumps(pack).lower()
    assert mention["text"] == mention["quote_text"] == "Learnt this from @*** last night"


def test_a_pack_built_with_the_list_the_job_read_uses_that_list():
    con = world()
    add_post(con, "p_hidden", "c_hidden", utc(day(1), 9), 900)
    add_post(con, "p_other", "c2", utc(day(1), 9), 600)
    duck.load(con, "core.creators", [
        {"creator_id": "c_hidden", "platform": "tiktok", "handle": "@kay", "coord_score": 0}])
    suppress(con, "c_hidden")
    client = duck.Client(con)
    hidden = evidence.read_hidden(client, core="core", agent="agent")
    assert hidden == ({"tiktok:kay"}, {"c_hidden"}, set())
    pack, *_ = evidence.build_pack(client, current(con), D, "ZA", core="core", agent="agent", hidden=hidden)
    assert ids(pack) == ["p_other"]


def test_an_unreadable_suppression_list_stops_the_pack_with_a_clear_error():
    con = world()
    add_post(con, "p1", "c1", utc(day(1), 9), 900)
    con.execute("DROP VIEW core.v_suppressed_creators")
    with pytest.raises(evidence.SuppressionUnreadable, match="suppression list"):
        build(con)


# Confirm search finds enter the pack only when local by the market scope rule (market_scope.sql creator_ranked)


CONFIRM = (("confirm", "search_presence"),)


def test_a_confirm_only_post_enters_the_pack_only_when_located_in_or_sighted_in_the_markets_feeds():
    con = world()
    add_post(con, "p_measured_unlocated", "c1", utc(day(1), 9), 10)
    # Seen by the sweep and found again by confirm: it enters through its sweep sighting, as before.
    add_post(con, "p_swept_and_confirmed", "c2", utc(day(1), 9), 10,
             sightings=[("sweep", "unbiased_rank"), ("confirm", "search_presence")])
    add_post(con, "p_confirm_located", "c3", utc(day(1), 9), 900, sightings=CONFIRM, geo_market="ZA",
             geo_confidence=0.9, geo_source="place_mention")
    add_post(con, "p_confirm_sourced", "c4", utc(day(1), 9), 800, sightings=CONFIRM)
    add_source_row(con, "p_confirm_sourced", ["ZA"], [source_sighting("ZA", day(1))])
    # Confirm finds not local by the same rule stay out, however engaged.
    add_post(con, "p_confirm_unlocated", "c5", utc(day(1), 9), 70_000, sightings=CONFIRM)
    add_post(con, "p_confirm_other_feed", "c6", utc(day(1), 9), 60_000, sightings=CONFIRM)
    add_source_row(con, "p_confirm_other_feed", ["NG"], [source_sighting("NG", day(1))])
    add_post(con, "p_confirm_stale_feed", "c7", utc(day(1), 9), 50_000, sightings=CONFIRM)
    add_source_row(con, "p_confirm_stale_feed", ["ZA"], [source_sighting("ZA", day(7))])
    add_post(con, "p_confirm_language", "c8", utc(day(1), 9), 40_000, sightings=CONFIRM, geo_market="ZA",
             geo_confidence=1.0, geo_source="language")
    add_post(con, "p_confirm_weak", "c9", utc(day(1), 9), 30_000, sightings=CONFIRM, geo_market="ZA",
             geo_confidence=0.6)
    add_post(con, "p_confirm_elsewhere", "c10", utc(day(1), 9), 20_000, sightings=CONFIRM, geo_market="NG",
             geo_confidence=0.9)

    pack, *_ = build(con)

    assert ids(pack) == ["p_measured_unlocated", "p_swept_and_confirmed", "p_confirm_located",
                         "p_confirm_sourced"]


def test_confirm_finds_left_out_free_their_creators_slots_for_local_posts():
    con = world()
    # The creator's unlocated confirm finds outrank its local post on engagement but never take its two slots.
    add_post(con, "p_confirm_0", "c1", utc(day(1), 9), 9_000, sightings=CONFIRM)
    add_post(con, "p_confirm_1", "c1", utc(day(1), 9), 8_000, sightings=CONFIRM)
    add_post(con, "p_local", "c1", utc(day(1), 9), 5, sightings=CONFIRM, geo_market="ZA", geo_confidence=0.9)

    pack, *_ = build(con)

    assert ids(pack) == ["p_local"]


def test_the_evidence_pack_holds_the_posts_the_market_scope_pack_counts():
    from core.brief.market_scope import read_market_scope

    con = world()
    add_post(con, "p_local", "c1", utc(day(1), 9), 10, geo_market="ZA", geo_confidence=0.9)
    add_post(con, "p_unlocated", "c2", utc(day(1), 9), 10)
    add_post(con, "p_confirm_sourced", "c3", utc(day(1), 9), 900, sightings=CONFIRM)
    add_source_row(con, "p_confirm_sourced", ["ZA"], [source_sighting("ZA", day(1))])
    for n in range(4):
        add_post(con, f"p_confirm_global_{n}", f"g{n}", utc(day(1), 9), 50_000 + n, sightings=CONFIRM)

    pack, *_ = build(con)
    scope = read_market_scope(duck.Client(con), {"item_id": "i1"}, D, "ZA", core="core", agent="agent")

    assert ids(pack) == ["p_local", "p_unlocated", "p_confirm_sourced"]
    assert scope["total_posts7"] == len(pack["evidence"])
    assert scope["market_posts7"] == 2


# The paid-label signal: the enrich model's marker or the vendor's label, and whether either was read


def sponsor_read(pack):
    return {e["id"]: ("sponsored" in e["flags"], e["sponsor_checked"]) for e in pack["evidence"]}


def test_a_post_is_sponsored_on_the_enrich_marker_or_the_vendor_label_and_checked_when_either_was_read():
    con = world()
    vendor = {
        "p_vendor_true": {"sponsored": True},
        "p_vendor_false": {"relevance": 0.9, "sponsored": False},
        "p_labels_true": {"labels": {"sponsored": True, "intent": "sell"}},
        "p_labels_false": {"labels": {"sponsored": False, "intent": "entertain"}},
        "p_disclosed": {"labels": {"sponsored": {"brand": "Acme", "disclosed": True, "p": 0.9, "undisclosed": False}}},
        "p_undisclosed": {"labels": {"sponsored": {"brand": None, "disclosed": False, "p": 0.7, "undisclosed": True}}},
        "p_neither": {"labels": {"sponsored": {"brand": None, "disclosed": False, "p": 0.1, "undisclosed": False}}},
        "p_other_labels": {"relevance": 0.4, "labels": {"intent": "entertain"}, "date_basis": "published"},
    }
    for n, (pid, labels) in enumerate(vendor.items()):
        add_post(con, pid, f"c{n}", utc(day(1), 9), 100 - n, vendor_labels=json.dumps(labels))
    add_post(con, "p_enrich_false_vendor_true", "c20", utc(day(1), 9), 50,
             vendor_labels=json.dumps({"labels": {"sponsored": True}}))
    add_post(con, "p_nothing", "c21", utc(day(1), 9), 40)
    duck.load(con, "core.post_enrichment", [{"post_id": "p_enrich_false_vendor_true", "sponsored": False}])

    pack, *_ = build(con)

    assert sponsor_read(pack) == {
        "p_vendor_true": (True, True), "p_vendor_false": (False, True),
        "p_labels_true": (True, True), "p_labels_false": (False, True),
        "p_disclosed": (True, True), "p_undisclosed": (True, True), "p_neither": (False, True),
        "p_other_labels": (False, False),
        "p_enrich_false_vendor_true": (True, True), "p_nothing": (False, False),
    }


def test_only_an_enrich_row_with_a_sponsored_reading_counts_as_checked():
    con = world()
    add_post(con, "p_enrich_false", "c1", utc(day(1), 9), 30)
    add_post(con, "p_enrich_true", "c2", utc(day(1), 9), 20)
    # embed.sql and video_insert.sql write post_enrichment rows that leave sponsored null.
    add_post(con, "p_embed_only", "c3", utc(day(1), 9), 10)
    add_post(con, "p_embed_and_enrich", "c4", utc(day(1), 9), 5)
    duck.load(con, "core.post_enrichment", [
        {"post_id": "p_enrich_false", "sponsored": False},
        {"post_id": "p_enrich_true", "sponsored": True},
        {"post_id": "p_embed_only", "near_dup_size": 1},
        {"post_id": "p_embed_and_enrich", "near_dup_size": 1},
        {"post_id": "p_embed_and_enrich", "sponsored": False}])

    pack, *_ = build(con)

    assert sponsor_read(pack) == {"p_enrich_false": (False, True), "p_enrich_true": (True, True),
                                  "p_embed_only": (False, False), "p_embed_and_enrich": (False, True)}
