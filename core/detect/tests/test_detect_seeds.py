"""Seed loop, detect side (BUILD.md 2.4, DATA.md section 5): known-answer tests on the planner and on seeds.sql
through DuckDB."""

import os
import re
from collections import defaultdict
from datetime import date, timedelta

import pytest

from core.detect import seeds, sqlrun
from core.detect.items import item_id

from . import duck
from .fixtures import D, at, day, run

SEED = D + timedelta(days=1)            # D is Sunday 20 September 2026
MONDAY = date(2026, 9, 21)
FULL = 0.0                              # month spend that leaves B at collect's 800 on D


@pytest.fixture(autouse=True)
def caps_as_these_tests_were_written(monkeypatch):
    """The known answers below are worked out on the caps as they stood until 3 October 2026 (month 45,000,
    collect 800, confirm 150, reserve 50); seeds.py reads the live ones from core/config/caps.yaml."""
    for name, value in (("MONTHLY", 45000), ("COLLECT", 800), ("CONFIRM", 150), ("RESERVE", 50)):
        monkeypatch.setattr(seeds, name, value)


def cand(key, market="ZA", kind="hashtag", state="spike", worth=0.85, novelty="ongoing", accel=0.0, lang=None,
         label=None, keywords=None, local_terms=None):
    return {"market": market, "item_id": item_id(kind, key), "kind": kind, "canonical_key": key,
            "label": label or key, "state": state, "worth_pct": worth, "novelty": novelty, "accel": accel,
            "lang": lang, "keywords": keywords, "local_terms": local_terms}


def lane(rows, name, market=None):
    return [r for r in rows if r["lane"] == name and (market is None or r["market"] == market)]


def keys(rows):
    return {(r["market"], r["item_id"]) for r in rows}


def qrow(key, seed_date, lane_name="expansion", market="ZA", kind="hashtag", **extra):
    row = {"seed_date": seed_date, "market": market, "item_id": item_id(kind, key), "query": key, "kind": kind,
           "lane": lane_name, "priority": 1.0, "template": "tiktok/search/hashtag", "ttl_days": 1,
           "credits_estimate": 1.0, "yield_posts": None, "yield_new_creators": None}
    row.update(extra)
    return row


# (1) Budget

def test_budget_caps_are_read_from_caps_yaml():
    from core.collect.socialcrawl_client import load_caps

    caps = load_caps()
    assert seeds._CAPS["MONTHLY"]["total"] == caps["MONTHLY"]["total"]
    assert {k: seeds._CAPS["ENGINE_DAILY"][k] for k in ("collect", "confirm", "reserve")} == \
        {k: caps["ENGINE_DAILY"][k] for k in ("collect", "confirm", "reserve")}


def test_budget_is_what_the_month_has_left_per_day_capped_at_collects_share():
    # 21 September: 10 days left, each keeping confirm's 150 and the 50 reserve out of B
    assert seeds.budget(40000, date(2026, 9, 21)) == pytest.approx((45000 - 40000 - 10 * 200) / 10)
    assert seeds.budget(0, date(2026, 9, 21)) == 800
    assert seeds.budget(44990, date(2026, 9, 21)) == 0
    assert seeds.budget(30000, date(2026, 9, 30)) == 800
    assert seeds.budget(44700, date(2026, 9, 30)) == pytest.approx(100)


def test_shares_follow_data_md_section_5():
    s = seeds.shares(800)
    assert s == {"budget": 800, "expansion": pytest.approx(240), "exploration": pytest.approx(24),
                 "anchor": pytest.approx(36), "quota": pytest.approx(60)}


# (2) Expansion

def test_expansion_takes_worth_p80_items_in_the_five_states_with_their_priority():
    cs = [cand("a", state="new_to_42", worth=0.9, novelty="new"), cand("b", state="emerging", worth=0.8),
          cand("c", state="spike", worth=0.85), cand("d", state="rising", worth=0.85),
          cand("e", state="peaking", worth=0.8), cand("f", state="fading", worth=0.95),
          cand("g", state="mainstream", worth=0.95), cand("h", state="spike", worth=0.79),
          cand("i", state="recurring", worth=0.99), cand("j", state="on_the_boards", worth=0.99)]
    rows = lane(seeds.plan(D, cs, spent=FULL)["rows"], "expansion")
    got = {r["query"]: r["priority"] for r in rows}
    assert got == pytest.approx({"a": 0.9 * 1.3 * 1.2, "b": 0.8 * 1.3, "c": 0.85, "d": 0.85, "e": 0.4})


def test_items_expanded_in_the_last_two_days_are_skipped_unless_accelerating():
    cs = [cand("x", accel=0.0), cand("y", accel=0.5), cand("z", accel=-1.0), cand("w", accel=None)]
    queue = [qrow("x", D), qrow("y", D - timedelta(days=1)), qrow("z", D - timedelta(days=2)), qrow("w", D)]
    rows = lane(seeds.plan(D, cs, queue=queue, spent=FULL)["rows"], "expansion")
    assert {r["query"] for r in rows} == {"y", "z"}


def test_templates_by_kind():
    assert seeds.template("hashtag", "amapiano", "#Amapiano") == ("tiktok/search/hashtag", "amapiano")
    assert seeds.template("sound", "tiktok:7301", "7301") == ("tiktok/song/videos", "7301")
    assert seeds.template("sound", "instagram:99", "99") == ("instagram/audio/reels", "99")
    assert seeds.template("sound", "youtube:abc", "abc") is None
    assert seeds.template("creator", "tiktok:kabza", "kabza") == ("tiktok/profile/videos", "kabza")
    assert seeds.template("creator", "twitter:mzansi", "mzansi") == ("twitter/user/tweets", "mzansi")
    assert seeds.template("topic", "load shedding", "Load shedding",
                          keywords=["loadshedding", "eskom", "stage 6", "power"], local_terms=["eish", "eskom"]) \
        == ("search/multi", "loadshedding eskom stage 6 eish")
    assert seeds.template("topic", "load shedding", "Load shedding") == ("search/multi", "Load shedding")
    assert seeds.template("brand", "castle lager", "Castle Lager") == ("search/multi", "Castle Lager")
    assert seeds.template("event", "durban july", "Durban July") == ("search/multi", "Durban July")
    assert seeds.platforms("search/multi", "brand") == ("twitter", "threads", "reddit")
    assert seeds.platforms("search/multi", "event") == ("twitter", "threads", "reddit")
    assert len(seeds.platforms("search/multi", "topic")) == 6
    assert seeds.platforms("tiktok/search/hashtag", "hashtag") == ("tiktok",)


def test_greedy_fills_each_markets_third_then_a_global_pass():
    za = [{"market": "ZA", "item_id": f"z{i:02}", "query": f"z{i:02}", "priority": 2 - i / 100,
           "credits_estimate": 1.0, "platforms": ("tiktok",), "lang": "en"} for i in range(25)]
    ng = [{"market": "NG", "item_id": f"n{i}", "query": f"n{i}", "priority": 0.5 - i / 100,
           "credits_estimate": 1.0, "platforms": ("tiktok",), "lang": "en"} for i in range(2)]
    picked = seeds.greedy(za + ng, 12, seeds.Quota(1e9))
    count = defaultdict(int)
    for r in picked:
        count[r["market"]] += 1
    assert dict(count) == {"ZA": 10, "NG": 2}
    assert {r["item_id"] for r in picked if r["market"] == "ZA"} == {f"z{i:02}" for i in range(10)}


def test_greedy_rotates_the_market_order_by_seed_day():
    rows = [{"market": m, "item_id": f"{m}{i}", "query": f"{m}{i}", "priority": 1.0, "credits_estimate": 1.0,
             "platforms": ("tiktok",), "lang": "en"} for m in seeds.MARKETS for i in range(5)]
    first = set()
    for k in range(3):
        picked = seeds.greedy(rows, 30, seeds.Quota(2), SEED + timedelta(days=k))
        assert len(picked) == 2 and len({r["market"] for r in picked}) == 1
        first.add(picked[0]["market"])
    assert first == set(seeds.MARKETS)


def test_greedy_charges_the_ledger_cost_per_call():
    cs = [cand("t1", kind="topic", worth=0.9, lang="en"), cand("h1", worth=0.85, lang="zu")]
    out = seeds.plan(D, cs, cost={"search/multi": 7.5, "tiktok/search/hashtag": 1.2}, spent=FULL)
    got = {r["query"]: r["credits_estimate"] for r in lane(out["rows"], "expansion")}
    assert got == {"t1": 7.5, "h1": 1.2}
    fallback = seeds.plan(D, cs, spent=FULL)
    assert {r["query"]: r["credits_estimate"] for r in lane(fallback["rows"], "expansion")} == {"t1": 5.0, "h1": 1.0}


def test_rising_p90_items_are_probed_in_the_other_two_markets_at_six_tenths():
    cs = [cand("r", state="rising", worth=0.95), cand("s", state="rising", worth=0.85, market="NG"),
          cand("p", state="spike", worth=0.95, market="KE")]
    rows = lane(seeds.plan(D, cs, spent=FULL)["rows"], "expansion")
    got = {(r["market"], r["query"]): r["priority"] for r in rows}
    assert got == pytest.approx({("ZA", "r"): 0.95, ("NG", "r"): 0.57, ("KE", "r"): 0.57,
                                 ("NG", "s"): 0.85, ("KE", "p"): 0.95})


def test_a_market_that_has_the_item_itself_keeps_its_own_row():
    cs = [cand("r", state="rising", worth=0.95), cand("r", state="spike", worth=0.9, market="NG")]
    rows = lane(seeds.plan(D, cs, spent=FULL)["rows"], "expansion")
    got = {(r["market"], r["query"]): r["priority"] for r in rows}
    assert got == pytest.approx({("ZA", "r"): 0.95, ("NG", "r"): 0.9, ("KE", "r"): 0.57})


# (3) Placebo

def _placebo_world():
    cs = [cand(f"e{i:02}", worth=0.85) for i in range(40)]
    cs += [cand(f"low{i}", worth=0.05 + i * 0.03) for i in range(10)]          # 0.05 to 0.32
    cs += [cand(f"mid{i}", worth=0.5) for i in range(3)]
    cs += [cand(f"nglow{i}", worth=0.1, market="NG") for i in range(5)]
    return cs


def test_placebo_is_about_five_percent_of_expansion_calls_on_items_below_p40_in_the_same_market():
    cs = _placebo_world()
    rows = seeds.plan(D, cs, spent=FULL)["rows"]
    assert len(lane(rows, "expansion", "ZA")) == 40
    placebo = lane(rows, "placebo")
    assert len(placebo) == 2 and {r["market"] for r in placebo} == {"ZA"}
    low = {c["item_id"] for c in cs if c["worth_pct"] < 0.4 and c["market"] == "ZA"}
    assert {r["item_id"] for r in placebo} <= low
    assert all(r["template"] == "tiktok/search/hashtag" for r in placebo)
    assert not keys(placebo) & keys(lane(rows, "expansion"))


def test_placebo_is_at_most_one_call_below_twenty_expansion_calls():
    days = [SEED + timedelta(days=k) for k in range(400)]
    two = [seeds.placebo_calls(2, s, "ZA") for s in days]
    assert set(two) <= {0, 1} and 0.05 <= sum(two) / len(two) <= 0.15      # about 5% of 2 calls
    assert max(seeds.placebo_calls(19, s, "NG") for s in days) == 1
    assert seeds.placebo_calls(0, SEED, "ZA") == 0
    assert seeds.placebo_calls(20, SEED, "ZA") == 1
    assert seeds.placebo_calls(40, SEED, "ZA") == 2
    assert seeds.placebo_calls(2, SEED, "ZA") == seeds.placebo_calls(2, SEED, "ZA")


def test_placebo_mix_follows_the_pool_even_when_a_platform_quota_is_full():
    cs = [cand(f"t{i:02}", kind="topic", worth=0.85) for i in range(40)]
    cs += [cand(f"lowh{i:02}", worth=0.1) for i in range(20)] + [cand(f"lowt{i:02}", kind="topic", worth=0.1)
                                                                 for i in range(20)]
    kinds = []
    for k in range(30):
        d = date(2026, 9, 1) + timedelta(days=k)
        full = [qrow("gdelt", d + timedelta(days=1), credits_estimate=60.0)]         # the whole tiktok quota
        rows = lane(seeds.plan(d, cs, queue=full, cost={"search/multi": 1.0}, spent=FULL)["rows"], "placebo")
        assert len(rows) == 2
        kinds += [r["kind"] for r in rows]
    assert 0.35 <= kinds.count("hashtag") / len(kinds) <= 0.65


def test_placebo_draw_is_reproducible_by_date():
    cs = _placebo_world()
    first = {r["item_id"] for r in lane(seeds.plan(D, cs, spent=FULL)["rows"], "placebo")}
    again = {r["item_id"] for r in lane(seeds.plan(D, cs, spent=FULL)["rows"], "placebo")}
    assert first == again
    others = [frozenset(r["item_id"] for r in lane(seeds.plan(D + timedelta(days=k), cs, spent=FULL)["rows"],
                                                   "placebo")) for k in range(1, 6)]
    assert any(o != frozenset(first) for o in others)


# (4) Exploration

def test_exploration_gets_ten_percent_of_the_expansion_share_from_p40_to_p80():
    cs = [cand(f"m{i:02}", worth=0.4 + i * 0.01) for i in range(40)]         # 0.40 to 0.79
    cs += [cand("top", worth=0.95), cand("low", worth=0.2)]
    out = seeds.plan(D, cs, spent=FULL)
    assert out["exploration"] == pytest.approx(0.1 * out["expansion"])
    rows = lane(out["rows"], "exploration")
    assert sum(r["credits_estimate"] for r in rows) == pytest.approx(24)
    assert {r["query"] for r in rows} <= {f"m{i:02}" for i in range(40)}


def test_thompson_posterior_is_the_kinds_record_of_rising_within_seven_days():
    trials = [{"market": "ZA", "item_id": "a", "kind": "hashtag", "success": True},
              {"market": "ZA", "item_id": "b", "kind": "hashtag", "success": False},
              {"market": "NG", "item_id": "c", "kind": "hashtag", "success": False},
              {"market": "ZA", "item_id": "d", "kind": "topic", "success": True}]
    post = seeds.posterior(trials)
    assert post["kind"] == {"hashtag": (2, 3), "topic": (2, 1)}
    assert post["item"] == {("ZA", "a"): (1, 0), ("ZA", "b"): (0, 1), ("NG", "c"): (0, 1), ("ZA", "d"): (1, 0)}


def test_thompson_sampling_favours_kinds_that_rose():
    trials = [{"market": "ZA", "item_id": f"h{i}", "kind": "hashtag", "success": True} for i in range(30)]
    trials += [{"market": "ZA", "item_id": f"t{i}", "kind": "topic", "success": False} for i in range(30)]
    cs = [cand(f"hx{i}", worth=0.6) for i in range(5)] + [cand(f"tx{i}", kind="topic", worth=0.6) for i in range(5)]
    theta = seeds.thompson(cs, seeds.posterior(trials), SEED)
    assert min(theta[("ZA", c["item_id"])] for c in cs[:5]) > max(theta[("ZA", c["item_id"])] for c in cs[5:])
    assert theta == seeds.thompson(cs, seeds.posterior(trials), SEED)


# (5) Diversity

def test_no_item_platform_or_language_takes_more_than_a_quarter_of_expansion_credits():
    # E = 240, so each item, platform and language may take 60 credits
    cs = [cand("big", kind="topic", state="rising", worth=0.99, lang="sw")]           # search/multi at 25
    cs += [cand(f"zu{i:03}", worth=0.9, lang="zu") for i in range(90)]                  # tiktok hashtags in ZA
    for m, site in (("NG", "twitter"), ("KE", "facebook")):     # creators on two sites, so only the item cap stops big
        cs += [cand(f"{m}en{i:02}", market=m, worth=0.85, lang="en") for i in range(30)]
        cs += [cand(f"{site}:{m}en{i:02}", market=m, kind="creator", worth=0.85, lang="en") for i in range(30)]
    out = seeds.plan(D, cs, cost={"search/multi": 25.0}, spent=FULL)
    limit = 0.25 * out["expansion"]
    used = defaultdict(float)
    for r in out["rows"]:
        used[("item", r["item_id"])] += r["credits_estimate"]
        used[("language", r["lang"])] += r["credits_estimate"]
        for p in r["platforms"]:
            used[("platform", p)] += r["credits_estimate"] / len(r["platforms"])
    assert max(used.values()) <= limit + 1e-9
    assert used[("platform", "tiktok")] == pytest.approx(limit)
    assert used[("language", "en")] == pytest.approx(limit)
    assert len([r for r in out["rows"] if r["query"] == "big"]) == 2


def test_unknown_language_does_not_cap_seeding():
    cs = [cand(f"{m}t{i:02}", market=m, kind="topic", worth=0.85) for m in seeds.MARKETS for i in range(30)]
    cs += [cand(f"{m}low{i}", market=m, worth=0.1) for m in seeds.MARKETS for i in range(5)]
    rows = seeds.plan(D, cs, cost={"search/multi": 3.0}, spent=FULL)["rows"]
    assert all(r["lang"] == "und" for r in rows)
    assert {r["market"] for r in lane(rows, "expansion")} == set(seeds.MARKETS)
    assert sum(r["credits_estimate"] for r in lane(rows, "expansion")) > 0.25 * 240
    assert len(lane(rows, "placebo")) >= 1


def test_language_clusters_fold_sheng_into_swahili_and_unknown_into_und():
    assert seeds.lang_cluster("sheng") == "sw"
    assert seeds.lang_cluster("SW") == "sw"
    assert seeds.lang_cluster(None) == "und"
    assert seeds.lang_cluster("") == "und"
    assert seeds.lang_cluster("pcm") == "pcm"


# (6) No Google Trends, no age terms

def test_the_queue_never_names_google_trends_or_age_terms():
    cs = [cand("genz", worth=0.95), cand("genzfashion", worth=0.95), cand("teens", worth=0.9),
          cand("fashion", kind="topic", worth=0.9, keywords=["google trends", "fashion week"]),
          cand("amapiano", worth=0.9)]
    rows = seeds.plan(D, cs, spent=FULL)["rows"]
    assert {r["query"] for r in rows} == {"amapiano"}
    assert not seeds.allowed({"template": "google_trends/interest", "query": "amapiano", "kind": "hashtag"})
    assert not seeds.allowed({"template": "search/multi", "query": "Gen Z style", "kind": "topic"})
    assert not seeds.allowed({"template": "search/multi", "query": "gen_z", "kind": "topic"})
    assert seeds.allowed({"template": "search/multi", "query": "Durban July", "kind": "event"})


# Anchors

def test_yielding_anchors_carry_forward_within_fifteen_percent_of_expansion():
    queue = []
    for i in range(50):
        queue.append(qrow(f"anc{i:02}", D, "anchor", item_id=None))
        queue.append(qrow(f"anc{i:02}", D, "anchor", item_id=None, priority=None, credits_estimate=None,
                          yield_posts=3))
    queue.append(qrow("a-dry", D, "anchor", item_id=None))           # sorts first, so only its yield keeps it out
    queue.append(qrow("a-dry", D, "anchor", item_id=None, credits_estimate=None, yield_posts=0))
    queue.append(qrow("a-kids", D, "anchor", item_id=None))
    queue.append(qrow("a-kids", D, "anchor", item_id=None, credits_estimate=None, yield_posts=9))
    rows = lane(seeds.plan(D, [], queue=queue, spent=FULL)["rows"], "anchor")
    assert len(rows) == 36
    assert {r["query"] for r in rows} <= {f"anc{i:02}" for i in range(50)}
    assert all(r["item_id"] is None and r["seed_date"] == SEED for r in rows)


# (7) and (1) through seeds.sql on DuckDB

LEDGER = """CREATE TABLE IF NOT EXISTS core.credit_ledger (
  trend_date DATE NOT NULL, logged_at TIMESTAMPTZ, run_id VARCHAR, job VARCHAR, lane VARCHAR, agent VARCHAR,
  market VARCHAR, platform VARCHAR, route VARCHAR, endpoint VARCHAR, params_hash VARCHAR, item_id VARCHAR,
  calls BIGINT, credits_quoted DOUBLE, credits_charged DOUBLE, cache_hit BOOLEAN, posts_new BIGINT,
  balance_after DOUBLE)"""
EXTRA = [("seed_queue", "priority", "DOUBLE"), ("seed_queue", "template", "VARCHAR"),
         ("seed_queue", "ttl_days", "BIGINT"), ("seed_queue", "credits_estimate", "DOUBLE"),
         ("seed_queue", "yield_posts", "BIGINT"), ("seed_queue", "yield_new_creators", "BIGINT"),
         ("post_enrichment", "langs", "VARCHAR[]"), ("clusters", "keywords", "VARCHAR[]"),
         ("clusters", "local_terms", "VARCHAR[]")]


def cmap(key, kind="hashtag", status="active"):
    return {"item_id": item_id(kind, key), "kind": kind, "canonical_key": key, "label": key, "status": status,
            "valid_from": at(day(400)), "valid_to": None}


def state_row(key, d=D, market="ZA", kind="hashtag", state="spike", worth=0.85, eligible=True, novelty="ongoing",
              main_series_id=None, run_id=None):
    return {"metric_date": d, "market": market, "item_id": item_id(kind, key), "kind": kind, "state_raw": state,
            "state": state, "eligible": eligible, "worth_pct": worth if eligible else None, "novelty": novelty,
            "main_series_id": main_series_id, "run_id": run_id or f"detect-{d:%Y%m%d}"}


def ledger(d, charged, route="search/multi", calls=1, quoted=None, lane_name="expansion"):
    return {"trend_date": d, "route": route, "calls": calls, "credits_charged": charged,
            "credits_quoted": charged if quoted is None else quoted, "lane": lane_name}


class World:
    def __init__(self):
        self.con = duck.connect()
        self.con.execute(LEDGER)
        for table, col, typ in EXTRA:
            self.con.execute(f"ALTER TABLE core.{table} ADD COLUMN IF NOT EXISTS {col} {typ}")
        self.detect_days = set()

    def load(self, table, rows):
        duck.load(self.con, table, rows)

    def states(self, rows):
        self.load("core.item_state", rows)
        for d in {r["metric_date"] for r in rows} - self.detect_days:
            self.load("agent.runs", [run("detect", d)])
            self.detect_days.add(d)

    def detect_runs(self, days):
        for d in set(days) - self.detect_days:
            self.load("agent.runs", [run("detect", d)])
            self.detect_days.add(d)

    def queue(self):
        cur = self.con.execute("SELECT * FROM core.seed_queue ORDER BY seed_date, market, lane, query")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def client(self):
        return duck.Client(self.con)

    def run(self, d=D):
        client = self.client()
        counts = seeds.run_seeds(client, d, f"detect-{d:%Y%m%d}", core="core", agent="agent")
        return counts, client.sql


def test_budget_reads_the_month_spend_from_credit_ledger():
    w = World()
    w.load("core.credit_ledger", [ledger(date(2026, 8, 31), 5000), ledger(date(2026, 9, 1), 20000),
                                  ledger(date(2026, 9, 10), 19000), ledger(date(2026, 9, 12), None, quoted=600),
                                  ledger(date(2026, 9, 20), 400, route="tiktok/search/top", lane_name="confirm")])
    counts, _ = w.run()
    assert counts["budget"] == pytest.approx(300)
    assert counts["expansion"] == pytest.approx(90)
    assert counts["exploration"] == pytest.approx(9)


def test_confirm_share_is_not_paid_from_b_and_a_new_month_starts_from_zero():
    w = World()
    w.load("core.credit_ledger", [ledger(date(2026, 9, 29), 44900)])
    counts, _ = w.run(date(2026, 9, 30))
    assert counts["budget"] == 800
    counts, _ = w.run(date(2026, 9, 29))
    assert counts["budget"] == 0


def test_cost_per_call_is_the_28_day_ledger_average_by_route():
    w = World()
    w.load("core.credit_ledger", [ledger(day(3), 12.0, calls=2), ledger(day(10), 6.0, calls=2),
                                  ledger(day(40), 100.0, calls=1), ledger(day(1), 2.0, route="tiktok/song/videos")])
    cost = seeds.read_cost(w.client(), D, core="core", agent="agent")
    assert cost == pytest.approx({"search/multi": 4.5, "tiktok/song/videos": 2.0})


def test_candidates_query_reads_state_acceleration_language_and_cluster_terms():
    w = World()
    w.load("core.cultural_map", [cmap("amapiano"), cmap("load shedding", "topic"), cmap("fyp"),
                                 cmap("viralthing", status="generic"), cmap("hidden"), cmap("old")])
    w.states([state_row("amapiano", main_series_id="s1", worth=0.9),
              state_row("load shedding", kind="topic", worth=0.5),
              state_row("fyp", worth=0.95), state_row("viralthing", worth=0.95),
              state_row("hidden", eligible=False),
              state_row("old", run_id="detect-20260920-superseded")])
    w.load("core.series_test", [{"metric_date": D, "series_id": "s1", "item_id": item_id("hashtag", "amapiano"),
                                 "market": "ZA", "accel": 0.4, "run_id": "stats-20260920"}])
    w.load("agent.runs", [run("stats", D)])
    w.load("core.post_observations", [
        {"post_id": p, "observed_at": at(day(1)), "observed_date": day(1), "market": "ZA", "platform": "tiktok",
         "lane": "sweep", "lane_class": "unbiased_rank"} for p in ("p1", "p2", "p3")])
    w.load("core.post_items", [{"post_id": p, "item_id": item_id("hashtag", "amapiano"), "via": "hashtag"}
                               for p in ("p1", "p2", "p3")])
    w.load("core.post_enrichment", [{"post_id": "p1", "langs": ["zu", "en"]}, {"post_id": "p2", "langs": ["zu"]},
                                    {"post_id": "p3", "langs": ["en"]}])
    w.load("core.clusters", [
        {"cluster_date": day(1), "cluster_id": "c1", "market": "pan", "item_id": item_id("topic", "load shedding"),
         "keywords": ["pan1"], "local_terms": []},
        {"cluster_date": day(2), "cluster_id": "c2", "market": "ZA", "item_id": item_id("topic", "load shedding"),
         "keywords": ["eskom", "stage 6"], "local_terms": ["eish"]}])
    got = {r["canonical_key"]: r for r in seeds.read_candidates(w.client(), D, "detect-20260920", core="core",
                                                                 agent="agent")}
    assert set(got) == {"amapiano", "load shedding", "fyp"}
    assert got["amapiano"]["accel"] == pytest.approx(0.4) and got["amapiano"]["lang"] == "zu"
    assert got["load shedding"]["keywords"] == ["eskom", "stage 6"]
    assert got["load shedding"]["local_terms"] == ["eish"]
    assert got["load shedding"]["lang"] is None


def test_candidates_read_single_market_clusters_written_in_lowercase():
    """L3 writes clusters.market as 'za', 'ng', 'ke' or 'pan'. The ZA item's 'za' cluster is read before a newer
    'pan' one, and an 'ng' cluster of the same item is never read for ZA."""
    w = World()
    w.load("core.cultural_map", [cmap("load shedding", "topic"), cmap("braai", "topic")])
    w.states([state_row("load shedding", kind="topic"), state_row("braai", kind="topic")])
    shed, braai = item_id("topic", "load shedding"), item_id("topic", "braai")
    w.load("core.clusters", [
        {"cluster_date": day(2), "cluster_id": "c1", "market": "za", "item_id": shed,
         "keywords": ["eskom"], "local_terms": ["eish"]},
        {"cluster_date": day(1), "cluster_id": "c2", "market": "pan", "item_id": shed,
         "keywords": ["pan1"], "local_terms": []},
        {"cluster_date": day(1), "cluster_id": "c3", "market": "ng", "item_id": braai,
         "keywords": ["suya"], "local_terms": []}])
    got = {r["canonical_key"]: r for r in seeds.read_candidates(w.client(), D, "detect-20260920", core="core",
                                                                 agent="agent")}
    assert (got["load shedding"]["keywords"], got["load shedding"]["local_terms"]) == (["eskom"], ["eish"])
    assert (got["braai"]["keywords"], got["braai"]["local_terms"]) == (None, None)


def test_candidates_read_todays_states_of_the_detect_run_while_it_is_still_running():
    w = World()
    w.load("core.cultural_map", [cmap("amapiano"), cmap("older")])
    w.load("core.item_state", [state_row("amapiano", run_id="detect-20260920-running"),
                               state_row("older", run_id="detect-20260920-earlier")])
    w.load("agent.runs", [run("detect", D, "detect-20260920-running", "running"),
                          run("detect", D, "detect-20260920-earlier")])
    got = seeds.read_candidates(w.client(), D, "detect-20260920-running", core="core", agent="agent")
    assert [r["canonical_key"] for r in got] == ["amapiano"]


def test_generic_and_ineligible_items_are_never_seeded():
    w = World()
    w.load("core.cultural_map", [cmap("amapiano"), cmap("fyp"), cmap("viralthing", status="generic"),
                                 cmap("hidden"), cmap("fyplow")])
    w.states([state_row("amapiano", worth=0.9), state_row("fyp", worth=0.95),
              state_row("viralthing", worth=0.95), state_row("hidden", eligible=False)])
    counts, _ = w.run()
    assert {r["query"] for r in w.queue()} == {"amapiano"}
    assert counts["appended"] == 1


def test_rows_are_appended_for_tomorrow_and_nothing_is_replaced():
    w = World()
    hot = [f"e{i:02}" for i in range(20)]                  # 20 expansion calls give exactly one placebo
    w.load("core.cultural_map", [cmap(k) for k in hot + ["lowish"]])
    w.states([state_row(k, worth=0.9) for k in hot] + [state_row("lowish", worth=0.1)])
    before = [qrow("older", day(3)), qrow("gdelt", SEED, market="NG", kind="topic", template="search/multi")]
    w.load("core.seed_queue", before)
    counts, sql = w.run()
    after = w.queue()
    for row in before:
        assert row in after
    new = [r for r in after if r not in before]
    assert {(r["lane"], r["query"]) for r in new} == {("expansion", k) for k in hot} | {("placebo", "lowish")}
    assert all(r["seed_date"] == SEED and r["ttl_days"] == 1 and r["credits_estimate"] == 1.0 for r in new)
    assert all(r["yield_posts"] is None and r["priority"] > 0 for r in new)
    writes = [s for s in sql if WRITE.search(s)]
    assert len(writes) == 1 and "INSERT INTO core.seed_queue" in writes[0]
    assert counts["appended"] == 21 and counts["credits"] == {"expansion": 20.0, "placebo": 1.0}


def test_a_rerun_appends_nothing_new():
    w = World()
    hot = [f"e{i:02}" for i in range(20)]
    w.load("core.cultural_map", [cmap(k) for k in hot + ["lowish", "midish"]])
    w.states([state_row(k, worth=0.9) for k in hot] + [state_row("lowish", worth=0.1), state_row("midish", worth=0.6)])
    first, _ = w.run()
    rows = w.queue()
    second, _ = w.run()
    assert first["appended"] == 22 and first["credits"]["placebo"] == 1.0
    assert second["appended"] == 0 and w.queue() == rows


def test_exploration_trials_come_from_seeds_that_ran_joined_to_later_rising_states():
    w = World()
    w.load("core.seed_queue", [
        qrow("a", day(10), "exploration"), qrow("a", day(10), "exploration", yield_posts=5),
        qrow("b", day(10), "exploration"), qrow("b", day(10), "exploration", yield_posts=3),
        qrow("c", day(10), "exploration"),
        qrow("t", day(3), "exploration", kind="topic", yield_posts=4),
        qrow("e", day(9), "exploration", yield_posts=0, yield_new_creators=0),
        qrow("f", day(10), "expansion", yield_posts=8)])
    w.states([state_row("a", d=day(7), state="rising"), state_row("b", d=day(2), state="rising"),
              state_row("e", d=day(8), state="rising"), state_row("f", d=day(8), state="rising"),
              state_row("t", d=day(1), state="rising")])
    w.detect_runs([day(i) for i in range(3, 11)])          # every window day had a good detect run
    trials = seeds.read_trials(w.client(), D, core="core", agent="agent")
    got = {(t["item_id"], t["success"]) for t in trials}
    assert got == {(item_id("hashtag", "a"), True), (item_id("hashtag", "b"), False),
                   (item_id("hashtag", "e"), False)}
    assert seeds.posterior(trials)["kind"] == {"hashtag": (2, 3)}


def test_a_trial_fails_only_when_every_day_of_its_window_had_a_good_detect_run():
    """Only full weeks: a seed that found posts but saw no Rising counts as a failure only when all 7 days of
    its window had a good detect run. A missing day leaves it out (it counts once the day's detect run is in);
    a Rising seen on a good day is still a success."""
    w = World()
    w.load("core.seed_queue", [
        qrow("full", day(20), "exploration", yield_posts=5),
        qrow("gap", day(10), "exploration", yield_posts=5),
        qrow("rose", day(10), "exploration", yield_posts=2),
        qrow("none", day(10), "exploration", yield_posts=0),
        qrow("edge", day(6), "exploration", yield_posts=4)])
    w.detect_runs([day(i) for i in range(1, 21) if i != 6])
    w.states([state_row("rose", d=day(8), state="rising")])
    w.load("agent.runs", [run("detect", D, "detect-20260920", "running")])
    got = {(t["item_id"], t["success"]) for t in seeds.read_trials(w.client(), D, core="core", agent="agent")}
    assert got == {(item_id("hashtag", "full"), False), (item_id("hashtag", "rose"), True)}
    w.detect_runs([day(6)])
    got = {(t["item_id"], t["success"]) for t in seeds.read_trials(w.client(), D, core="core", agent="agent")}
    assert got == {(item_id("hashtag", "full"), False), (item_id("hashtag", "rose"), True),
                   (item_id("hashtag", "gap"), False), (item_id("hashtag", "none"), False)}


# (8) Weekly drift report

def test_drift_report_gives_herfindahl_concentration_of_seeds_against_the_unseeded_feeds():
    seed_rows = [{"kind": "hashtag", "template": "tiktok/search/hashtag", "credits_estimate": 3.0, "lang": "en"},
                 {"kind": "topic", "template": "search/multi", "credits_estimate": 6.0, "lang": "zu"}]
    feed_rows = [{"dim": "platform", "cat": "tiktok", "n": 2}, {"dim": "platform", "cat": "youtube", "n": 2},
                 {"dim": "kind", "cat": "hashtag", "n": 4}, {"dim": "language", "cat": "sheng", "n": 1},
                 {"dim": "language", "cat": "sw", "n": 1}, {"dim": "language", "cat": "en", "n": 2}]
    got = seeds.drift(seed_rows, feed_rows)
    assert got["kind"] == pytest.approx({"seeds": 5 / 9, "feeds": 1.0, "gap": 5 / 9 - 1})
    assert got["platform"] == pytest.approx({"seeds": 15 / 81, "feeds": 0.5, "gap": 15 / 81 - 0.5})
    assert got["language"] == pytest.approx({"seeds": 5 / 9, "feeds": 0.5, "gap": 5 / 9 - 0.5})
    assert got["und_share"] == 0.0
    seed_rows.append({"kind": "hashtag", "template": "tiktok/search/hashtag", "credits_estimate": 3.0, "lang": None})
    assert seeds.drift(seed_rows, feed_rows)["und_share"] == pytest.approx(0.25)
    assert seeds.hhi({}) is None


def _drift_world():
    w = World()
    w.load("core.cultural_map", [cmap("amapiano")])
    w.load("core.seed_queue", [qrow("amapiano", MONDAY - timedelta(days=1), credits_estimate=2.0)])
    w.load("core.post_observations", [
        {"post_id": "p1", "observed_at": at(MONDAY), "observed_date": MONDAY, "market": "ZA", "platform": "tiktok",
         "lane": "sweep", "lane_class": "unbiased_rank"}])
    w.load("core.post_items", [{"post_id": "p1", "item_id": item_id("hashtag", "amapiano"), "via": "hashtag"}])
    return w


def test_drift_report_runs_only_on_mondays():
    drift_sql = {sqlrun.render(seeds.statements()[n], core="core", agent="agent") for n in ("drift_seeds", "drift_feeds")}
    counts, sql = _drift_world().run(MONDAY)
    assert counts["drift"]["kind"] == pytest.approx({"seeds": 1.0, "feeds": 1.0, "gap": 0.0})
    assert counts["drift"]["platform"]["seeds"] == pytest.approx(1.0)
    assert counts["drift"]["language"] == pytest.approx({"seeds": 1.0, "feeds": 1.0, "gap": 0.0})
    assert counts["drift"]["und_share"] == 1.0
    assert drift_sql <= set(sql)
    counts, sql = _drift_world().run(MONDAY - timedelta(days=1))
    assert "drift" not in counts
    assert not drift_sql & set(sql)


WRITE = re.compile(r"\b(INSERT|MERGE|UPDATE|DELETE|TRUNCATE|DROP|REPLACE)\b", re.I)


def test_seeds_sql_has_no_statement_that_replaces_or_removes_rows():
    writes = [n for n, s in seeds.statements().items() if WRITE.search(s)]
    assert writes == ["append"]
    assert WRITE.findall(seeds.statements()["append"]) == ["INSERT"]
    assert set(seeds.statements()) == {"spend", "cost", "candidates", "queue", "trials", "drift_seeds",
                                       "drift_feeds", "append"}


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
def test_seeds_sql_dry_runs_on_bigquery():
    from google.cloud import bigquery

    from core.detect import aggregate

    client = bigquery.Client(project="ogilvy-trends-v2")
    params = {"d": D, "month_start": date(2026, 9, 1), "run_id": "detect-20260920-000000000001"}
    for name, sql in seeds.statements().items():
        extra = []
        if name == "append":
            extra = [aggregate._struct_array("rows", [dict(qrow("x", SEED), item_id="i")], seeds.SEED_FIELDS)]
        used = {k: v for k, v in params.items() if f"@{k}" in sql}
        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False,
                                         query_parameters=[sqlrun._param(k, v) for k, v in used.items()] + extra)
        assert client.query(sqlrun.render(sql), job_config=config).dry_run


def _locality_world(statuses):
    w = World()
    keys = [f"tag{i}" for i in range(len(statuses))]
    w.load("core.cultural_map", [cmap(k) for k in keys])
    w.states([{**state_row(k), "locality_status": status} for k, status in zip(keys, statuses)])
    return w, dict(zip(statuses, keys))


def test_under_v2_seeds_skip_an_item_whose_locality_row_is_unreadable_or_missing(monkeypatch):
    """Review N3: state carries these rows as eligible so the brief can hold them; nothing spends credits on them."""
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v2")
    w, key = _locality_world(["local", "unreadable", "missing", "not_local", None, "market_unconfirmed"])
    got = {r["canonical_key"] for r in seeds.read_candidates(w.client(), D, "detect-20260920", core="core", agent="agent")}
    assert got == {key["local"], key["not_local"], key[None], key["market_unconfirmed"]}


def test_under_v1_seeds_read_no_locality_status(monkeypatch):
    from core.conftest import set_locality_authority

    set_locality_authority(monkeypatch, "v1")
    w, key = _locality_world(["local", "unreadable", "missing"])
    got = {r["canonical_key"] for r in seeds.read_candidates(w.client(), D, "detect-20260920", core="core", agent="agent")}
    assert got == set(key.values())
