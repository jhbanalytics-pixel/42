"""Known-answer tests for item_state's share flags and worth-attention score (DATA.md section 3.7, BUILD.md 2.3),
driven through the real sql/state.sql on DuckDB.

Share flags: each label lands in item_state.share_flags when its 7-day share reaches DATA.md's threshold
(near_duplicates .30, young_accounts .40, burst .35 and concentrated .60, the last two only with 10 or more
posts), and not below it. Worth: series_test rows and window posts are written directly, and worth_raw and
worth_pct are computed here from those inputs by the wr CTE's formula, independently of the SQL.
"""

import math
from datetime import timedelta

import pytest

from . import duck
from .fixtures import D, at, counter, creator, day, item_daily, obs, post, rid
from .test_detect_states import LEGACY, RANK, RULE, World, detect, sql_statements


@pytest.fixture
def con():
    c = duck.connect()
    for stmt in sql_statements("waves.sql"):
        c.execute(duck.create_statement(stmt))
    yield c
    c.close()


def add_posts(w, item, n, market="ZA", creators=None, tier=None, near_dup=(), young=(), bucket=(), old=()):
    """n posts of item in its measured feed lane, post j first seen on day(j % 3). creators[j] names post j's
    creator (default one creator per post); tier maps a post index to its creator tier (default micro).
    Post j is published j hours after midnight of day(10), each in its own 10-minute bucket, except the
    posts in bucket, which share one minute. near_dup posts have near_dup_size 3. The creators of young posts
    opened their accounts 29 days before the post's first sighting, those of old posts exactly 30 days before;
    every other account is 400 days old."""
    w.items.add(item)
    known = {r["creator_id"] for r in w.t["core.creators"]}
    for j in range(n):
        pid, d = f"{item}-p{j}", day(j % 3)
        cid = creators[j] if creators else f"{item}-c{j}"
        p = post(pid, cid, d, tier=(tier or {}).get(j, "micro"))
        p["published_at"] = at(day(0), 6) if j in bucket else at(day(10), 0) + timedelta(hours=j)
        w.t["core.posts"].append(p)
        w.t["core.post_observations"].append(obs(pid, d, "unbiased_rank", "sweep", market=market))
        w.t["core.post_items"].append({"post_id": pid, "item_id": item, "via": "hashtag"})
        if j in near_dup:
            w.t["core.post_enrichment"].append({"post_id": pid, "sponsored": False, "near_dup_size": 3})
        if cid not in known:
            c = creator(cid)
            if j in young or j in old:
                c["account_created_at"] = at(d - timedelta(days=29 if j in young else 30))
            w.t["core.creators"].append(c)
            known.add(cid)


# share_flags


def flag_item(w, item, n, **kw):
    """A New to 42 item: local feed reads from day(2), and n posts as add_posts writes them."""
    for i in (2, 1, 0):
        w.counter(item, "feed_tiktok", i, 1.0)
    add_posts(w, item, n, **kw)


FLAG_ITEMS = {
    "nd30": (dict(n=10, near_dup=range(3)), ["near_duplicates"]),               # 3 of 10: .30
    "nd20": (dict(n=10, near_dup=range(2)), []),
    "yg40": (dict(n=10, young=range(4)), ["young_accounts"]),                    # 4 of 10: .40
    "yg30": (dict(n=10, young=range(3), old=(3,)), []),                          # a 30-day account is not young
    "bu35": (dict(n=20, bucket=range(7)), ["burst"]),                            # 7 of 20 in one bucket: .35
    "bu30": (dict(n=20, bucket=range(6)), []),
    "bu9": (dict(n=9, bucket=range(9)), []),                                     # all in one bucket, under 10 posts
    "co60": (dict(n=10, creators=["a", "a", "b", "b", "c", "c", "d", "e", "f", "g"]), ["concentrated"]),
    "co50": (dict(n=10, creators=["a", "a", "b", "b", "c", "d", "e", "f", "g", "h"]), []),
    "co9": (dict(n=9, creators=["a", "a", "a", "b", "b", "b", "c", "c", "c"]), []),   # top 3 all, under 10 posts
    "all4": (dict(n=10, creators=["a", "a", "a", "b", "b", "c", "c", "d", "e", "f"], near_dup=range(3),
                  young=(3, 4, 7, 8, 9), bucket=(5, 6, 7, 8)),
             ["near_duplicates", "young_accounts", "burst", "concentrated"]),
    "cp": (dict(n=30, near_dup=range(9)), ["near_duplicates"]),                  # 9 of 30, and 30 posts seen
}


def test_share_flags_land_in_item_state_at_their_thresholds(con):
    w = World()
    w.protocol("feed_tiktok", 9)
    for item, (kw, _) in FLAG_ITEMS.items():
        kw = dict(kw)
        n = kw.pop("n")
        if "creators" in kw:
            kw["creators"] = [f"{item}-{c}" for c in kw["creators"]]
        flag_item(w, item, n, **kw)
    w.build(con)
    rows = detect(con)
    got = {item: list(rows[item]["share_flags"]) for item in FLAG_ITEMS}
    assert got == {item: flags for item, (_, flags) in FLAG_ITEMS.items()}
    assert set(FLAG_ITEMS) <= set(rows)
    # a share flag never holds an item; with 30 or more posts seen it reads Check pattern
    assert rows["cp"]["authenticity"] == "check_pattern" and rows["cp"]["eligible"] is True
    assert rows["all4"]["authenticity"] == "not_assessed"                         # under 30 posts


# worth_raw and worth_pct


def st_row(item, d, *, tested, y, p=None, mu=None, med=0.0, vel=None, accel=None, first=None, market="ZA",
           significant=False):
    return {"metric_date": d, "series_id": f"{item}|{market}|feed_tiktok|p1", "item_id": item, "market": market,
            "platform": "tiktok", "series": "feed_tiktok", "protocol": "p1", "lane_class": "unbiased_rank",
            "kind": "hashtag", "y": y, "trials": 3, "obs_prior": 40 if tested else 2, "obs28": 28 if tested else 2,
            "first_measured": first or (day(60) if tested else day(5)),
            "baseline_state": "ok" if tested else "warmup", "med": med, "v3": y, "peak28": max(y, 1.0),
            "vel": vel, "accel": accel, "test": "nb" if tested else "none", "mu": mu, "p_mid": p, "q": p,
            "significant": significant, "run_id": rid("stats", d), "rule_version": RULE}


# creators in the last 3 days for e00 to e20: 3 + (8k mod 21), a shuffle of 3 to 23, with e09 at 30 instead
CREATORS = [3 + (8 * k) % 21 for k in range(21)]
CREATORS[9] = 30


def worth_spec():
    """Per item: its market, its series_test inputs, creators3 and the factors the wr CTE reads. e00 to e13 and
    the twins are New to 42 (untested, first measured day(5)); e14 to e20 are On the boards (tested,
    first measured day(60), p_mid 10^-(k-13)). nl is New to 42 but not_local, so not eligible; ns is tested
    with no state, so it has no item_state row but still takes a place in the percent ranks."""
    spec = {}
    for k in range(21):
        tested = k >= 14
        spec[f"e{k:02d}"] = dict(
            market="ZA", tested=tested, y=float(k), p=10.0 ** -(k - 13) if tested else None,
            mu=2.0 if tested else None, med=0.0, vel=(20 - k) / 10, accel=-0.3 if k == 3 else 0.0,
            creators=CREATORS[k], auth=.8, spread=.25, novelty=.35 if tested else 1.0, diffusion=.7, eligible=True)
    spec["e09"]["auth"] = 1.0                                         # 30 posts seen, co-action ran: Clear
    spec["e02"]["novelty"] = .8                                       # a legacy wave 200 days ago: recurrence
    spec["e17"]["novelty"] = .6                                       # a variant cluster
    spec["e04"]["diffusion"] = 1.0                                    # a macro post after the first micro one
    spec["e16"]["diffusion"] = .5                                     # a macro post first
    spec["e18"]["spread"] = .6                                        # significant in ZA and NG 5 days ago
    for twin in ("t1", "t2"):
        spec[twin] = dict(market="ZA", tested=False, y=.5, p=None, mu=None, med=0.0, vel=2.5, accel=0.0,
                          creators=25, auth=.8, spread=.25, novelty=1.0, diffusion=.7, eligible=True)
    spec["nl"] = dict(market="ZA", tested=False, y=5.5, p=None, mu=None, med=0.0, vel=1.05, accel=None,
                      creators=3, auth=.8, spread=.25, novelty=1.0, diffusion=.7, eligible=False)
    spec["ns"] = dict(market="ZA", tested=True, y=1.0, p=1e-9, mu=1.0, med=1.0, vel=None, accel=None,
                      creators=0, eligible=None)
    spec["g0"] = dict(market="NG", tested=False, y=1.0, p=None, mu=None, med=0.0, vel=.5, accel=0.0,
                      creators=3, auth=.8, spread=.25, novelty=1.0, diffusion=.7, eligible=True)
    spec["g1"] = dict(market="NG", tested=False, y=2.0, p=None, mu=None, med=0.0, vel=.1, accel=0.0,
                      creators=4, auth=.8, spread=.25, novelty=1.0, diffusion=.7, eligible=True)
    return spec


def worth_world(con, spec):
    w = World()
    w.days["stats"] |= {D, day(5)}
    w.days["collect"].add(D)
    w.days["coaction"].add(D)
    for item, s in spec.items():
        w.items.add(item)
        w.t["core.series_test"].append(st_row(item, D, tested=s["tested"], y=s["y"], p=s["p"], mu=s["mu"],
                                              med=s["med"], vel=s["vel"], accel=s["accel"], market=s["market"]))
        if s["creators"]:
            tier = {0: "macro"} if item == "e16" else {s["creators"] - 1: "macro"} if item == "e04" else None
            add_posts(w, item, s["creators"], market=s["market"], tier=tier)
        if s["tested"] and item != "ns":
            w.t["core.item_counter_daily"].append(
                counter(item, "board_tiktok_hashtag", D, 1.0, is_board=True))
    for d in (day(201), day(200), day(2), day(1), day(0)):
        w.days["aggregate"].add(d)
    w.t["core.item_daily"] += [item_daily("e02", day(201), 3, **LEGACY), item_daily("e02", day(200), 9, **LEGACY)]
    w.t["core.item_daily"] += [item_daily("e02", day(i), 1, **RANK) for i in (2, 1, 0)]
    w.t["core.item_daily"].append(item_daily("nl", D, 10, platform="_all", lane_class="_any", series=None,
                                             protocol=None, geo_known_posts=10, local_posts=0))
    w.t["core.clusters"].append({"cluster_date": D, "cluster_id": "k1", "market": "ZA", "item_id": "e17",
                                 "match_kind": "variant"})
    for market in ("ZA", "NG"):
        w.t["core.series_test"].append(st_row("e18", day(5), tested=True, y=9.0, p=1e-4, mu=1.0, market=market,
                                              significant=True))
    w.load(con)


def percent_rank(keys):
    n = len(keys)
    return [0.0 if n == 1 else sum(o < k for o in keys) / (n - 1) for k in keys]


def cume_dist(keys):
    return [sum(o <= k for o in keys) / len(keys) for k in keys]


def hand(auth, surge, momentum, breadth, spread, novelty, diffusion):
    """worth_raw as DATA.md 3.7's wr CTE writes it."""
    return auth * math.exp((math.log(.05 + surge) + math.log(.05 + momentum) + math.log(.05 + breadth)
                            + math.log(.05 + spread) + math.log(novelty) + math.log(diffusion)) / 6)


def expected_worth(spec):
    """worth_raw for every item with a state and worth_pct for every eligible cohort of 20 or more, by market."""
    raw, pct = {}, {}
    for market in {s["market"] for s in spec.values()}:
        items = [i for i, s in spec.items() if s["market"] == market]
        ss = [spec[i] for i in items]
        surge = percent_rank([(0.0 if s["p"] is None else -math.log10(max(s["p"], 1e-12)),
                               (s["y"] + 1) / ((s["mu"] if s["mu"] is not None else s["med"]) + 1)) for s in ss])
        momentum = percent_rank([(s["vel"] or 0.0) + .5 * (s["accel"] or 0.0) for s in ss])
        breadth = percent_rank([s["creators"] for s in ss])
        for i, s, su, mo, br in zip(items, ss, surge, momentum, breadth):
            if s["eligible"] is not None:
                raw[i] = hand(s["auth"], su, mo, br, s["spread"], s["novelty"], s["diffusion"])
        for eligible in (True, False):
            cohort = [i for i in items if spec[i]["eligible"] is eligible]
            n, ws = len(cohort), [raw[i] for i in cohort]
            for i, pr, cd in zip(cohort, percent_rank(ws), cume_dist(ws)):
                pct[i] = (pr * (n - 1) + cd * n - 1) / 2 / (n - 1) if eligible and n >= 20 else None
    return raw, pct


def test_worth_raw_and_worth_pct_match_the_hand_computed_formula(con):
    spec = worth_spec()
    worth_world(con, spec)
    rows = detect(con)
    assert set(rows) == set(spec) - {"ns"}
    assert rows["e09"]["authenticity"] == "clear" and rows["e02"]["novelty"] == "recurrence"
    assert rows["e17"]["novelty"] == "variant" and rows["e14"]["novelty"] == "ongoing"
    assert (rows["e04"]["diffusion"], rows["e16"]["diffusion"], rows["e05"]["diffusion"]) == (
        "bottom_up", "top_down", "small_only")
    assert rows["e18"]["markets_hot"] == 2 and rows["nl"]["eligible"] is False

    # Three items by hand, ranks counted over the 25 ZA hashtag rows (ns included, so n - 1 = 24).
    # e09: surge place 12 of 0 to 24, momentum 13, breadth 24 (30 creators, the most); Clear, new, small_only.
    assert rows["e09"]["worth_raw"] == pytest.approx(hand(1, 12 / 24, 13 / 24, 24 / 24, .25, 1, .7), rel=1e-12)
    # e18: surge 21 (p_mid 1e-5), momentum 3, breadth 19 (21 creators); two hot markets, ongoing, small_only.
    assert rows["e18"]["worth_raw"] == pytest.approx(hand(.8, 21 / 24, 3 / 24, 19 / 24, .6, .35, .7), rel=1e-12)
    # e02: surge 4, momentum 20 (vel 1.8), breadth 17 (19 creators); a recurrence, small_only.
    assert rows["e02"]["worth_raw"] == pytest.approx(hand(.8, 4 / 24, 20 / 24, 17 / 24, .25, .8, .7), rel=1e-12)

    raw, pct = expected_worth(spec)
    assert {i: rows[i]["worth_raw"] for i in rows} == pytest.approx(raw, rel=1e-12)
    got_pct = {i: rows[i]["worth_pct"] for i in rows}
    assert {i for i, v in got_pct.items() if v is None} == {"nl", "g0", "g1"}   # not eligible; NG cohort of 2
    assert {i: v for i, v in got_pct.items() if v is not None} == pytest.approx(
        {i: v for i, v in pct.items() if v is not None}, rel=1e-12)
    # the twins tie, so each takes the midrank of the two places they share
    assert rows["t1"]["worth_raw"] == rows["t2"]["worth_raw"]
    below = sum(raw[i] < raw["t1"] for i in raw if spec[i]["market"] == "ZA" and spec[i]["eligible"])
    assert got_pct["t1"] == got_pct["t2"] == pytest.approx((below + .5) / 22)
