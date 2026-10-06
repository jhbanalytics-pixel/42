"""Seeds for the collect job's expansion phase (task 2.4, collect side). No network: BigQuery is faked."""
import random
import re
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.collect import job, seeds
from core.collect.tests.test_gdelt import COHORT_FORMS, ROUND_FORMS, UNICODE_AGE_LENS

RUN = date(2026, 9, 30)
COST = {"slots14": 15, "slots16": 7, "cost14": 1, "cost16": 10}


def seed(query, lane="expansion", day=RUN, ttl=3, template="search/multi", market="ZA", item_id=None,
         kind="topic", priority=2.0):
    return {"seed_date": day, "market": market, "item_id": item_id or f"{kind}|{query.casefold()}", "query": query,
            "kind": kind, "lane": lane, "priority": priority, "template": template, "ttl_days": ttl,
            "credits_estimate": 10.0, "yield_posts": None, "yield_new_creators": None}


def used(query, posts, creators, day, lane="exploration", market="ZA"):
    return dict(seed(query, lane=lane, day=day, ttl=0, market=market, template="tiktok/search/top"),
                yield_posts=posts, yield_new_creators=creators, credits_estimate=None)


def tags(n, origin=None):
    ranked = {"hashtag": [f"tag{i:02d}" for i in range(n)], "sound": []}
    if origin:
        ranked["origin"] = {t: origin(i) for i, t in enumerate(ranked["hashtag"])}
    return ranked


def credits(out, **cost):
    cost = {"14": cost.get("cost14", 1), "16": cost.get("cost16", 10)}
    return [(p, cost[row]) for row in ("14", "16") for p in out[row]]


def share(out, key):
    paid = credits(out)
    total = sum(c for _, c in paid)
    groups = {}
    for p, c in paid:
        if key(p) is not None:
            groups[key(p)] = groups.get(key(p), 0) + c
    return {k: v / total for k, v in groups.items()}


# Reading seed_queue

class FakeBQ:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, {p.name: p for p in job_config.query_parameters}))
        return SimpleNamespace(result=lambda: self.rows)


def test_read_is_one_parameterised_select_over_the_lookback():
    bq = FakeBQ([seed("amapiano")])
    assert seeds.read(bq, RUN, ("ZA", "NG", "KE")) == [seed("amapiano")]
    [(sql, params)] = bq.queries
    assert "`ogilvy-trends-v2.intelligence_42_core.seed_queue`" in sql
    assert sql.lstrip().upper().startswith("SELECT")
    assert "@d" in sql and "UNNEST(@markets)" in sql and f"INTERVAL {seeds.LOOKBACK_DAYS} DAY" in sql
    assert params["d"].value == RUN and params["markets"].values == ["ZA", "NG", "KE"]
    assert not re.search(r"\b(MERGE|INSERT|UPDATE|DELETE|DROP|TRUNCATE|CREATE|REPLACE)\b", sql, re.I)


def test_live_keeps_seeds_inside_their_ttl_only():
    rows = [
        seed("today"), seed("two days old", day=RUN - timedelta(days=2)),
        seed("three days old", day=RUN - timedelta(days=3)),                  # ttl 3: expired
        seed("tomorrow", day=RUN + timedelta(days=1)),                        # not yet
        seed("no ttl", ttl=0), seed("null ttl", ttl=None),
        used("a yield row", 5, 2, RUN),                                        # a record, not a seed
        seed("placebo pick", lane="placebo"),
        dict(seed("x"), query=None), dict(seed("y"), query="  "),
        seed("lagos", market="NG"),
    ]
    live = seeds.live(rows, RUN)
    assert [s["query"] for s in live["ZA"]] == ["placebo pick", "today", "two days old"]
    assert [s["query"] for s in live["NG"]] == ["lagos"]
    assert live["KE"] == []


def test_live_uses_null_yields_and_present_estimate_to_identify_queued_rows():
    rows = [seed("queued"),
            dict(seed("posts yielded"), yield_posts=0, credits_estimate=None),
            dict(seed("creator yielded"), yield_new_creators=1, credits_estimate=None),
            dict(seed("estimate missing"), credits_estimate=None)]
    assert [s["query"] for s in seeds.live(rows, RUN)["ZA"]] == ["queued"]


def test_live_keeps_producer_placebo_rows_as_queued_seeds():
    placebo = seed("low topic", lane="placebo", template="tiktok/search/hashtag")
    assert [(s["query"], s["lane"], s["template"]) for s in seeds.live([placebo], RUN)["ZA"]] == [
        ("low topic", "placebo", "tiktok/search/hashtag")]


def test_live_drops_rule_one_words():
    word = "".join(["you", "th"])
    live = seeds.live([seed(f"{word} month"), seed("amapiano")], RUN)
    assert [s["query"] for s in live["ZA"]] == ["amapiano"]


def test_one_seed_per_cluster_newest_then_highest_priority():
    rows = [seed("Cyril Ramaphosa", day=RUN - timedelta(days=1), priority=9.0),
            seed("cyril ramaphosa", priority=1.0), seed("#CyrilRamaphosa", priority=3.0),
            seed("amapiano", priority=1.0), seed("gqom", priority=5.0)]
    live = seeds.live(rows, RUN)["ZA"]
    assert [(s["query"], s["priority"]) for s in live] == [("gqom", 5.0), ("#CyrilRamaphosa", 3.0), ("amapiano", 1.0)]
    assert seeds.cluster("Cyril Ramaphosa") == seeds.cluster("#cyrilramaphosa") == "cyrilramaphosa"


def test_an_anchor_expires_after_three_uses_that_found_nothing():
    anchors = [seed("braai", lane="anchor"), seed("kota", lane="anchor"), seed("sasa", lane="anchor")]
    history = [used("braai", 0, 0, RUN - timedelta(days=d), lane="anchor") for d in (1, 2, 3)]
    history += [used("kota", 0, 0, RUN - timedelta(days=1), lane="anchor"),
                used("kota", 0, 0, RUN - timedelta(days=2), lane="anchor"),
                used("kota", 4, 1, RUN - timedelta(days=3), lane="anchor")]
    history += [used("sasa", 0, 0, RUN - timedelta(days=d), lane="anchor") for d in (1, 2)]
    live = seeds.live(anchors + history, RUN)["ZA"]
    assert sorted(s["query"] for s in live) == ["kota", "sasa"]


def test_record_lists_each_clusters_uses_newest_first():
    rows = [used("gqom", 3, 0, RUN - timedelta(days=2)), used("gqom", 5, 2, RUN - timedelta(days=1)),
            used("gqom", 1, 1, RUN - timedelta(days=1), market="NG"), seed("gqom")]
    record = seeds.record(rows)
    assert [u["yield_posts"] for u in record[("ZA", "gqom")]] == [5, 3]
    assert [u["yield_posts"] for u in record[("NG", "gqom")]] == [1]


def test_record_keeps_yield_rows_with_unknown_post_count():
    unknown = dict(seed("gqom"), credits_estimate=None, yield_posts=None, yield_new_creators=1)
    assert seeds.record([unknown]) == {("ZA", "gqom"): [unknown]}


def test_unknown_post_count_does_not_expire_an_anchor():
    anchor = seed("braai", lane="anchor")
    unknown = dict(seed("braai", lane="anchor", day=RUN - timedelta(days=1)),
                   credits_estimate=None, yield_posts=None, yield_new_creators=1)
    misses = [used("braai", 0, 0, RUN - timedelta(days=day), lane="anchor") for day in (2, 3)]
    assert [s["query"] for s in seeds.live([anchor, unknown, *misses], RUN)["ZA"]] == ["braai"]


# The mix: slice, exploration and the caps

def test_seeds_share_the_expansion_slots_and_never_add_any():
    queue = [seed(f"news {i}", priority=10 - i) for i in range(6)] + \
            [seed(f"topic {i}", template="tiktok/search/top") for i in range(4)]
    out = seeds.mix(tags(30), queue, {}, random.Random(1), **COST)
    assert len(out["14"]) == 15 and len(out["16"]) == 7
    assert sum(c for _, c in credits(out)) == 85
    assert {p.family for p in out["16"]} >= {"seed_queue"}
    assert [p.query for p in out["16"] if p.family == "seed_queue"][:2] == ["news 0", "news 1"]
    assert any(p.family == "seed_queue" for p in out["14"])
    queued = [p for p in out["14"] + out["16"] if p.family == "seed_queue"]
    assert all(p.item_id and p.seed_date == RUN and p.kind == "topic" for p in queued)


def test_anchor_slice_is_at_most_15_percent_of_expansion_credits():
    anchors = [seed(f"anchor {i}", lane="anchor", template="search/multi" if i % 2 else "tiktok/search/top",
                    priority=1.0) for i in range(20)]
    out = seeds.mix(tags(30), anchors, {}, random.Random(1), **COST)
    paid = credits(out)
    anchor = sum(c for p, c in paid if p.lane == "anchor")
    assert 0 < anchor <= 0.15 * sum(c for _, c in paid)
    assert all(p.family == "anchor" for p, _ in paid if p.lane == "anchor")


def test_exploration_takes_10_to_15_percent_of_row_14_from_below_the_cut():
    for n in (8, 15, 30):
        out = seeds.mix(tags(n), [], {}, random.Random(n), **COST)
        row14 = out["14"]
        explore = [p for p in row14 if p.lane == "exploration"]
        assert 0.10 <= len(explore) / len(row14) <= 0.15, n
        main = [p.query for p in row14 if p.lane == "expansion"]
        assert main == tags(n)["hashtag"][:len(main)]
        assert not {p.query for p in explore} & set(main)
    again = seeds.mix(tags(30), [], {}, random.Random(30), **COST)
    first = seeds.mix(tags(30), [], {}, random.Random(30), **COST)
    assert [p.query for p in again["14"]] == [p.query for p in first["14"]]


def test_thompson_sampling_favours_exploration_seeds_that_found_new_creators():
    queue = [seed("winner", lane="exploration", template="tiktok/search/top")] + \
            [seed(f"loser {i}", lane="exploration", template="tiktok/search/top") for i in range(5)]
    record = {"winner": [{"yield_posts": 9, "yield_new_creators": 3}] * 10,
              **{f"loser{i}": [{"yield_posts": 2, "yield_new_creators": 0}] * 10 for i in range(5)}}
    picked = {}
    for n in range(200):
        out = seeds.mix(tags(16), queue, record, random.Random(n), **COST)
        explore = [p for p in out["14"] if p.lane == "exploration"]
        assert len(explore) == 2
        for p in explore:
            picked[p.query] = picked.get(p.query, 0) + 1
    assert picked["winner"] >= 180
    assert all(picked.get(f"loser {i}", 0) <= 20 for i in range(5))
    assert sum(v for k, v in picked.items() if k.startswith("tag")) > 100  # untried harvest picks still get a go


def test_no_platform_or_source_family_above_a_quarter_when_the_pool_allows():
    rows = ("1", "2", "3", "7", "8")
    ranked = tags(40, origin=lambda i: (f"p{i % 8}", rows[i % 5]))
    queue = [seed(f"news {i}") for i in range(10)]
    out = seeds.mix(ranked, queue, {}, random.Random(3), **COST)
    assert sum(c for _, c in credits(out)) == 85
    assert max(share(out, lambda p: p.platform).values()) <= 0.25
    families = share(out, lambda p: p.family)
    assert {"feed", "board", "chart", "forum", "panel", "seed_queue"} <= set(families)
    assert max(families.values()) <= 0.25


def test_a_one_platform_pool_still_fills_the_slots():
    out = seeds.mix(tags(30, origin=lambda i: ("tiktok", "1")), [], {}, random.Random(1), **COST)
    assert len(out["14"]) == 15 and len(out["16"]) == 7


def test_no_cluster_above_a_quarter_of_expansion_credits_in_the_job():
    for n in range(0, 12):
        for k in range(0, 4):
            queue = [seed(f"news {i}") for i in range(k)] + [seed("tag00", template="tiktok/search/top")]
            calls = [c for c in job.expansion_calls("ZA", RUN, tags(n), random.Random(n), set(), queue=queue)
                     if c.row in ("14", "16")]
            total = sum(c.hold() for c in calls)
            by = {}
            for c in calls:
                by[c.seed.cluster] = by.get(c.seed.cluster, 0) + c.hold()
            assert all(v <= 0.25 * total for v in by.values()), (n, k, by)


AGE_TAGS = ["".join(["tee", "ns"]), "".join(["ge", "nzhumor"]), "".join(["boo", "merhumour"])]


def test_a_rule_one_harvest_tag_takes_no_slot_in_any_row_or_lane():
    for n in (1, 5, 30):
        ranked = {"hashtag": AGE_TAGS + [f"tag{i:02d}" for i in range(n)], "sound": []}
        out = seeds.mix(ranked, [], {}, random.Random(n), **COST)
        picked = {p.query for row in ("14", "16") for p in out[row]}
        assert not picked & set(AGE_TAGS), (n, picked)
        assert len(out["14"]) == min(15, n) and len(out["16"]) == min(7, n)


@pytest.mark.parametrize("tag", UNICODE_AGE_LENS + COHORT_FORMS)
def test_mix_gives_no_slot_to_a_disguised_or_end_position_rule_one_tag(tag):
    ranked = {"hashtag": [tag] + [f"tag{i:02d}" for i in range(30)], "sound": [], "origin": {tag: ("tiktok", "2")}}
    for n in range(5):
        out = seeds.mix(ranked, [], {}, random.Random(n), **COST)
        assert [p.query for row in ("14", "16") for p in out[row] if p.query == tag] == []
        assert len(out["14"]) == 15 and len(out["16"]) == 7


@pytest.mark.parametrize("query", ROUND_FORMS + ["\u0430mapiano", "Cyril \u0420amaphosa"])
def test_every_path_refuses_a_disguised_fused_or_mixed_script_form(query):
    assert job._tag(query) is None and job._tag("#" + query) is None
    for lane in seeds.LANES:
        live = seeds.live([seed(query, lane=lane), seed("amapiano", lane=lane)], RUN)
        assert [s["query"] for s in live["ZA"]] == ["amapiano"], lane
    queue = [seed(query), seed(query, lane="anchor", template="tiktok/search/top"),
             seed(query, lane="exploration", template="tiktok/search/top")]
    ranked = {"hashtag": [query] + [f"tag{i:02d}" for i in range(30)], "sound": [],
              "origin": {query: ("tiktok", "2")}}
    for n in range(3):
        out = seeds.mix(ranked, queue, {}, random.Random(n), **COST)
        assert [p.query for row in ("14", "16") for p in out[row] if p.query == query] == []


def test_a_rule_one_harvest_tag_gets_no_call_and_no_yield_row():
    ranked = {"hashtag": AGE_TAGS + [f"tag{i:02d}" for i in range(30)], "sound": []}
    calls = job.expansion_calls("ZA", RUN, ranked, random.Random("x"), set())
    searches = [c for c in calls if c.row in seeds.EXPANSION_ROWS]
    assert searches and not {c.params["query"] for c in searches} & set(AGE_TAGS)
    done = [(c, SimpleNamespace(status="ok", credits_charged=1), {"posts": [{"post_id": c.params["query"],
                                                                              "creator_id": "c"}]})
            for c in searches]
    rows = seeds.yield_rows(RUN, "ZA", done)
    assert rows and not {r["query"] for r in rows} & set(AGE_TAGS)


# Yield rows

def call(row, query, lane="expansion", pick_lane=None, family="seed_queue", item_id="topic|x"):
    pick = seeds.Pick(query, pick_lane or lane, family, seeds.cluster(query), item_id=item_id, kind="topic",
                      seed_date=RUN - timedelta(days=1), priority=4.5, row=row)
    route = "search/multi" if row == "16" else "tiktok/search/top"
    return SimpleNamespace(row=row, route=route, lane=lane, seed=pick)


def done_entry(c, status, charged, posts):
    return (c, SimpleNamespace(status=status, credits_charged=charged),
            {"posts": [{"post_id": p, "creator_id": cr} for p, cr in posts]} if status == "ok" else None)


def test_yield_rows_append_one_row_per_seed_used_with_its_yield():
    feed = SimpleNamespace(row="1", route="tiktok/trending", lane="sweep", seed=None)
    done = [
        done_entry(feed, "ok", 5, [("f1", "c-feed"), ("f2", "c-feed2")]),
        done_entry(call("16", "Cyril Ramaphosa"), "ok", 10, [("p1", "c-feed"), ("p2", "c-new"), ("p3", None)]),
        done_entry(call("14", "cyril ramaphosa"), "ok", 1, [("p2", "c-new"), ("p4", "c-new2")]),
        done_entry(call("14", "braai", pick_lane="anchor", family="anchor", item_id="topic|braai"), "ok", 1, []),
        done_entry(call("14", "failed seed", item_id="topic|failed"), "error", 0, []),
        done_entry(call("14", "amapiano", lane="exploration", family="harvest", item_id=None), "empty", 0, []),
    ]
    rows = seeds.yield_rows(RUN, "ZA", done)
    got = {(r["query"], r["lane"]): r for r in rows}
    assert set(got) == {("Cyril Ramaphosa", "expansion"), ("braai", "anchor"), ("amapiano", "exploration")}
    cyril = got[("Cyril Ramaphosa", "expansion")]
    assert cyril == {"seed_date": "2026-09-30", "market": "ZA", "item_id": "topic|x", "query": "Cyril Ramaphosa",
                     "kind": "topic", "lane": "expansion", "priority": 4.5,
                     "template": "search/multi,tiktok/search/top", "ttl_days": 0, "credits_estimate": None,
                     "yield_posts": 4, "yield_new_creators": 2}
    assert got[("braai", "anchor")]["yield_posts"] == 0
    assert got[("amapiano", "exploration")]["item_id"] is None
    assert all(r["ttl_days"] == 0 for r in rows)  # a record is never read back as a live seed
    assert seeds.live(rows, RUN)["ZA"] == []


def test_new_seed_creators_excludes_creators_seen_in_any_nonseed_call():
    harvest = SimpleNamespace(row="14", route="tiktok/search/top", lane="expansion", seed=None)
    queued = call("16", "Cyril Ramaphosa")
    done = [done_entry(harvest, "ok", 1, [("h1", "c-seen")]),
            done_entry(queued, "ok", 1, [("s1", "c-seen"), ("s2", "c-new")])]
    [(row,)] = [(r,) for r in seeds.yield_rows(RUN, "ZA", done)]
    assert row["yield_new_creators"] == 1


def test_placeholders_stand_for_every_lane_the_mix_reads():
    fake = seeds.placeholders("KE", RUN)
    assert {s["lane"] for s in fake} == {"expansion", "exploration", "anchor"}
    assert all(s["market"] == "KE" and s["query"].startswith("<KE ") for s in fake)
    assert seeds.live(fake, RUN)["KE"]


# Hygiene

BANNED = ["".join(p) for p in (["gen", "z"], ["gen", " z"], ["gen", "-z"], ["you", "th"], ["stu", "dent"],
                                ["te", "en"], ["mill", "ennial"], ["gener", "ation"], ["boo", "mer"],
                                ["you", "ng"], ["var", "sity"], ["adoles", "cent"], ["google", "trends"],
                                ["google", "_trends"], ["google", " trends"])]
FORBIDDEN_SQL = re.compile(r"\b(DELETE|DROP|TRUNCATE|CREATE|REPLACE|UPDATE|WRITE_TRUNCATE)\b|expiration", re.I)


@pytest.mark.parametrize("name", ["seeds.py", "drift.py"])
def test_sources_hold_no_banned_literals_dashes_or_destructive_sql(name):
    text = (Path(seeds.__file__).parent / name).read_text(encoding="utf-8")
    assert not [w for w in BANNED if w in text.casefold()]
    assert not [d for d in (chr(0x2014), chr(0x2013)) if d in text]
    assert not re.search(r"-{2}(?![a-z])", text), "double hyphen outside a CLI flag"
    assert not FORBIDDEN_SQL.search(text)
