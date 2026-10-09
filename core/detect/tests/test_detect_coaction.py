"""Known-answer tests for the co-action authenticity step (TRUST.md section 5, DATA.md coord_signals and
creators.coord_score, BUILD.md 2.11).

Unit tests drive coaction.analyse on plain rows, the shape coaction.sql returns: one row per post, market and
item. The DuckDB tests run coaction.run_coaction on fixture tables and then state.sql, so item_state reads the
coord_signals rows through v_coord_signals_current.

The job tests also check that detect still imports, runs and writes item_state when datasketch or networkx is
missing from the image, or when the coaction step fails.
"""

import json
import math
import os
import random
import re
import subprocess
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pytest

from .. import aggregate, coaction, job, sqlrun
from . import duck
from .fixtures import D, at, creator, day, obs, run
from .test_detect_job import FakeChain, JobClient, step_rows
from .test_detect_job import world as job_world
from .test_detect_states import World, detect

RULE = "r1"


def row(pid, acct, when, text="", tags=(), item="x", kind="hashtag", key=None, market="ZA", first_day=None):
    return {"post_id": pid, "market": market, "creator_id": acct, "published_at": when, "text": text,
            "hashtags": list(tags), "item_id": item, "kind": kind, "canonical_key": key or item,
            "first_day": first_day or when.date()}


def utc(i, hour, minute=0):
    """day(i) at hour:minute UTC. 08:00 UTC is 10:00 SAST, outside the evening."""
    return at(day(i), hour) + timedelta(minutes=minute)


def copost(accounts, items, i, hour=8, text=None, market="ZA", tag="cp"):
    """Each account posts once, one minute apart, on every item in items (one post carries them all). By default
    the post shares a link of its own for this day and hour, as a campaign pushing new content each time does."""
    text = text or f"Read this https://camp.example/{tag}-{i}-{hour}"
    out = []
    for k, a in enumerate(accounts):
        pid = f"{tag}-{i}-{hour}-{a}"
        for it in items:
            out.append(row(pid, a, utc(i, hour, k), text=text, item=it, market=market))
    return out


def organic(item, n, i=1, tag="org", start_hour=0, market="ZA"):
    """n posts on item by n distinct accounts, 11 minutes apart, no text."""
    return [row(f"{tag}-{item}-{k}", f"{tag}-{item}-a{k}", utc(i, start_hour, 11 * k), item=item, market=market)
            for k in range(n)]


BG = 20


def background(item, *days, market="ZA"):
    """BG other posts on item on each of day(i) for i in days, 00:00 to 03:29 UTC, no text. The chance check
    reshuffles post times within an item's day, so a burst or co-post can only beat chance on a day the item
    has other posts; on a day with nothing else, any reshuffle reproduces it."""
    return [r for i in days for r in organic(item, BG, i, tag=f"bg{i}", market=market)]


def signals(result):
    return sorted((s["item_id"], s["signal"]) for s in result["signals"])


NET = [f"net{k}" for k in range(6)]


# Features: masking, and the item's own key left out


def test_mask_hides_handles_numbers_and_urls():
    assert coaction.mask("Vote @Ruto_2027 now!! 50 bob each https://x.co/a?b=1 #Kura") == \
        "vote HANDLE now NUM bob each URL #kura"
    assert coaction.mask("  WE   stand\twith 3,000.5 people www.site.org/x ") == "we stand with NUM people URL"


def test_features_leave_out_the_item_key():
    text = "Friday night #Amapiano #dance #vibes with the crew"
    tags = ["#Amapiano", "#dance", "#vibes", "#joburg", "#friday", "#crew", "#fyp"]
    f = coaction.features(row("p", "a", utc(1, 8), text, tags, item="amapiano"))
    assert f["template"] == "friday night #dance #vibes with the crew"
    assert f["tags"] == ("dance", "vibes", "joburg", "friday", "crew")         # key and #fyp left out
    g = coaction.features(row("p", "a", utc(1, 8), text, tags, item="dance"))
    assert g["template"] == "friday night #amapiano #vibes with the crew"
    assert g["tags"] == ("amapiano", "vibes", "joburg", "friday", "crew")
    # a meme item's own phrase is its key: nothing is left to compare
    m = coaction.features(row("p", "a", utc(1, 8), "Doing the dance challenge #fun", item="m", kind="meme",
                              key="doing the dance challenge"))
    assert m["template"] is None and m["text"] is None and m["tags"] is None
    # a sound item's key never appears in text, so nothing is removed
    s = coaction.features(row("p", "a", utc(1, 8), text, tags, item="s", kind="sound", key="tiktok:777"))
    assert s["tags"] == ("amapiano", "dance", "vibes", "joburg", "friday", "crew")


def test_short_templates_and_single_hashtags_are_not_features():
    f = coaction.features(row("p", "a", utc(1, 8), "vote NOW 2027 @x #a", ["#a", "#b"], item="b"))
    assert f["template"] is None            # two words once handles and numbers are masked
    assert f["tags"] is None                # one hashtag left once the key is removed
    assert f["urls"] == frozenset()
    # hashtags are not template words: two words and four tags is no template
    assert coaction.features(row("p", "a", utc(1, 8), "vote now #a #b #c #d", item="z"))["template"] is None


@pytest.mark.parametrize("tags,expected", [
    (["#fyp", "#foryou", "#viral"], None),                                     # generic only
    (["#fyp", "#foryou", "#viral", "#a", "#b", "#c", "#d"], None),             # four once generic tags go
    (["#fyp", "#foryou", "#viral", "#a", "#b", "#c", "#d", "#e"], ("a", "b", "c", "d", "e")),
])
def test_hashtag_sequences_leave_out_generic_tags_and_need_five(tags, expected):
    assert coaction.features(row("p", "a", utc(1, 8), "", tags, item="z"))["tags"] == expected


def test_a_generic_tag_sequence_links_no_accounts():
    rows = []
    for i in (2, 1):
        for k in range(8):
            rows.append(row(f"g-{i}-{k}", f"gen{k}", utc(i, 8, k), "#fyp #foryou #viral lol",
                            ["#fyp", "#foryou", "#viral", "#x"]))
    assert coaction.coaction_events(rows) == []
    assert coaction.analyse(rows, D)["networks"] == []


def feats(text="", tags=(), emb=None, item="x"):
    return coaction.features(row("p", "a", utc(1, 8), text, tags, item=item), emb)


LONG = "the minister promised free data bundles for every student in the province before the vote"


def test_match_by_feature():
    assert coaction.match(feats("see https://a.co/Z now"), feats("look https://a.co/Z")) == ["url"]
    assert coaction.match(feats(LONG), feats(LONG)) == ["template", "text"]
    near = LONG.replace("bundles", "bundle")
    assert coaction.match(feats(LONG), feats(near)) == ["text"]
    assert coaction.match(feats(LONG), feats("a completely different sentence about football tonight")) == []
    five = ["#a", "#b", "#c", "#d", "#e"]
    assert coaction.match(feats("", ["#x"] + five), feats("", five)) == ["hashtags"]
    assert coaction.match(feats("", five), feats("", five[::-1])) == []


def test_embedding_cosine_only_when_both_posts_have_an_embedding():
    e1 = [1.0, 0.0, 0.2]
    e2 = [0.98, 0.05, 0.21]                  # cosine about 0.998
    far = [0.0, 1.0, 0.0]
    assert coaction.match(feats("alpha one", emb=e1), feats("beta two", emb=e2)) == ["embedding"]
    assert coaction.match(feats("alpha one", emb=e1), feats("beta two", emb=far)) == []
    assert coaction.match(feats("alpha one", emb=e1), feats("beta two")) == []
    assert coaction.match(feats("alpha one", emb=[]), feats("beta two", emb=[])) == []


def test_embeddings_passed_to_analyse_link_posts_whose_text_differs():
    rows, emb = [], {}
    for i in (2, 1):
        for k, a in enumerate(NET[:5]):
            pid = f"e-{i}-{a}"
            rows.append(row(pid, a, utc(i, 8, k), text=a))
            emb[pid] = [1.0, 0.01 * k, 0.0]
    rows += background("x", 2, 1)
    assert coaction.analyse(rows, D)["networks"] == []
    net = coaction.analyse(rows, D, emb)["networks"]
    assert [n["accounts"] for n in net] == [5]


# Co-action edges and the repeat rule


def test_pairs_count_only_within_ten_minutes():
    ten = [row("a1", "a", utc(1, 8), "https://u.co/1"), row("b1", "b", utc(1, 8, 10), "https://u.co/1")]
    eleven = [row("a1", "a", utc(1, 8), "https://u.co/1"), row("b1", "b", utc(1, 8, 11), "https://u.co/1")]
    assert len(coaction.coaction_events(ten)) == 1
    assert coaction.coaction_events(eleven) == []


def test_the_same_account_never_pairs_with_itself():
    rows = [row("a1", "a", utc(1, 8), "https://u.co/1"), row("a2", "a", utc(1, 8, 1), "https://u.co/1")]
    assert coaction.coaction_events(rows) == []


def pair_posts(spec):
    """Two accounts co-posting a shared link: spec lists (day index, hour, items, link) per co-post."""
    out = []
    for n, (i, hour, items, link) in enumerate(spec):
        for a, minute in (("a", 0), ("b", 2)):
            for it in items:
                out.append(row(f"{a}{n}", a, utc(i, hour, minute), f"https://u.co/{link}", item=it))
    return out


@pytest.mark.parametrize("spec,qualifies", [
    ([(1, 8, ["x"], 1)], False),                            # once
    ([(1, 8, ["x"], 1), (1, 14, ["x"], 2)], True),          # same day and item, two new links: the bot branch
    ([(1, 8, ["x"], 1), (1, 14, ["x"], 1)], False),         # same day and item, the same link
    ([(2, 8, ["x"], 1), (1, 8, ["x"], 2)], True),           # two days, new content each time
    ([(2, 8, ["x"], 1), (1, 8, ["x"], 1)], False),          # two days, the same item and link: fans reposting
    ([(1, 8, ["x"], 1), (1, 14, ["y"], 2)], True),          # two items
    ([(1, 8, ["x"], 1), (1, 14, ["y"], 1)], True),          # the same link pushed on two items
    ([(1, 8, ["x", "y"], 1)], False),                       # one post pair carrying two items is one event
    ([(1, 8, ["x", "y"], 1), (1, 14, ["y", "z"], 2)], True),   # share item y, but two new links in a day
    ([(1, 8, ["x", "y"], 1), (1, 14, ["y", "z"], 1)], False),  # share item y and the link
])
def test_a_pair_counts_only_when_it_repeats_across_items_or_days(spec, qualifies):
    events = coaction.coaction_events(pair_posts(spec))
    assert (coaction.qualifying_pairs(events) == {("ZA", ("a", "b"))}) is qualifies


@pytest.mark.parametrize("n,networks", [(4, 0), (5, 1), (6, 1)])
def test_components_of_five_or_more_accounts_are_networks(n, networks):
    rows = copost(NET[:n], ["x"], 2) + copost(NET[:n], ["x"], 1) + background("x", 2, 1)
    assert len(coaction.analyse(rows, D)["networks"]) == networks


def test_a_network_alone_on_its_items_falls_back_to_the_trust_thresholds():
    # with nothing else posted on those item-days a reshuffle has nothing to shuffle against, so the chance
    # check cannot be computed and the pair is judged on TRUST.md 5.1's literal rule alone (an owned tag)
    rows = copost(NET, ["x"], 2) + copost(NET, ["x"], 1)
    assert [n["accounts"] for n in coaction.analyse(rows, D)["networks"]] == [6]
    # the same co-posts spread over more than 30 minutes of the day are not an owned item-day
    rows = copost(NET, ["x"], 2) + copost(NET, ["x"], 1) + organic("x", 3, 2, start_hour=12)
    rows += organic("x", 3, 1, start_hour=12)
    assert coaction.analyse(rows, D)["networks"] == []


def test_poisson_tail_stays_finite_for_heavy_counts():
    assert coaction._poisson_tail(2, 0.5) == pytest.approx(1 - math.exp(-0.5) * 1.5)
    assert coaction._poisson_tail(0, 3.0) == 1.0
    for k, lam in ((500, 2.0), (400, 150.0), (172, 1.0)):
        p = coaction._poisson_tail(k, lam)
        assert 0.0 <= p <= 1e-12, (k, lam, p)
    assert 0.9999 < coaction._poisson_tail(100, 150.0) < 1.0             # about 0.999994


def test_a_heavy_bot_network_completes_and_is_flagged():
    """5 bots co-post a new link 6 times a day for 30 days among 30 other accounts posting their own words."""
    rng = random.Random(7)
    bots = [f"bot{k}" for k in range(5)]
    rows = []
    for i in range(30):
        for n in range(6):
            start = at(day(i), 6) + timedelta(hours=2.5 * n, seconds=rng.uniform(0, 600))
            rows += [row(f"{b}-{i}-{n}", b, start + timedelta(seconds=90 * j),
                         f"great deal https://promo.example/{i}-{n} today", ["#promo"], item="promo")
                     for j, b in enumerate(bots)]
        rows += [row(f"pbg-{i}-{b}", f"pbg{b}", at(day(i), 4) + timedelta(hours=rng.uniform(0, 17.9)),
                     " ".join(rng.choice(VOCAB) for _ in range(7)), ["#promo"], item="promo")
                 for b in range(30) if rng.random() < 0.5]
    res = coaction.analyse(rows, D)
    assert [n["accounts"] for n in res["networks"]] == [5]
    assert verdict(res) == {"promo": "likely"}


# coord_signals rows


def test_a_network_writes_a_coaction_row_per_item_with_its_share():
    rows = copost(NET, ["x"], 1) + copost(NET, ["y"], 1, hour=14) + organic("x", 24) + organic("y", 54)
    res = coaction.analyse(rows, D)
    assert signals(res) == [("x", "coaction"), ("y", "coaction")]
    by = {s["item_id"]: s for s in res["signals"]}
    assert by["x"]["accounts"] == 6 and by["x"]["item_posts_share"] == pytest.approx(0.2)
    assert by["y"]["accounts"] == 6 and by["y"]["item_posts_share"] == pytest.approx(0.1)
    assert by["x"]["component_id"] == by["y"]["component_id"]
    assert all(s["market"] == "ZA" for s in res["signals"])
    assert res["scores"] == {a: 1 for a in NET}


def test_signal_rows_name_no_account():
    rows = copost(NET, ["x"], 1) + copost(NET, ["y"], 1, hour=14) + background("x", 1) + background("y", 1)
    res = coaction.analyse(rows, D)
    assert signals(res) == [("x", "coaction"), ("y", "coaction")]
    for s in res["signals"]:
        assert set(s) == {"item_id", "market", "signal", "component_id", "accounts", "item_posts_share"}
        text = json.dumps(s)
        assert not any(a in text for a in NET)


def test_a_network_active_only_before_the_seven_days_scores_but_writes_no_row():
    rows = copost(NET, ["x"], 12) + copost(NET, ["y"], 12, hour=14) + organic("x", 10) + background("x", 12)
    rows += background("y", 12)
    res = coaction.analyse(rows, D)
    assert res["signals"] == []
    assert res["scores"] == {a: 1 for a in NET}


def test_networks_are_found_per_market():
    rows = copost(NET[:3], ["x"], 2) + copost(NET[:3], ["x"], 1)
    rows += copost(NET[3:], ["x"], 2, market="KE") + copost(NET[3:], ["x"], 1, market="KE")
    assert coaction.analyse(rows, D)["networks"] == []


def pool_posts(item, i, accounts, gap, also=()):
    """Each account posts once on item (and any items in also) on day(i) from 10:00 UTC, gap minutes apart,
    with no text, so no two posts share content."""
    return [row(f"{item}-{a}", a, utc(i, 10, gap * k), item=j) for k, a in enumerate(accounts) for j in (item,) + also]


def test_pool_reuse_across_three_unrelated_items():
    rows = copost(NET, ["x"], 1) + copost(NET, ["y"], 1, hour=14) + background("x", 1) + background("y", 1)
    for i, it in ((3, "u"), (4, "v"), (5, "w")):                  # six pool accounts inside 30 minutes
        rows += pool_posts(it, i, NET, 5)
    res = coaction.analyse(rows, D)
    assert signals(res) == [("u", "pool_reuse"), ("v", "pool_reuse"), ("w", "pool_reuse"),
                            ("x", "coaction"), ("x", "pool_reuse"), ("y", "coaction"), ("y", "pool_reuse")]
    by = {(s["item_id"], s["signal"]): s for s in res["signals"]}
    assert by[("u", "pool_reuse")]["item_posts_share"] == 1.0 and by[("u", "pool_reuse")]["accounts"] == 6


def test_pool_reuse_counts_an_item_only_when_five_pool_accounts_post_inside_30_minutes():
    rows = copost(NET, ["x"], 1) + copost(NET, ["y"], 1, hour=14) + background("x", 1) + background("y", 1)
    for i, it in ((3, "u"), (4, "v"), (5, "w")):                  # the same six accounts, 2 hours apart
        rows += pool_posts(it, i, NET, 120)
    assert signals(coaction.analyse(rows, D)) == [("x", "coaction"), ("y", "coaction")]


def test_items_carried_on_the_same_posts_are_related_so_no_pool_reuse():
    rows = copost(NET, ["x"], 1) + copost(NET, ["y"], 1, hour=14) + background("x", 1) + background("y", 1)
    for i, it in ((3, "u"), (4, "v"), (5, "w")):                  # each post also carries x
        rows += pool_posts(it, i, NET, 5, also=("x",))
    assert signals(coaction.analyse(rows, D)) == [("x", "coaction"), ("y", "coaction")]


def test_pools_merge_only_when_they_overlap_by_half_the_smaller():
    a, b, c = frozenset("abcdef"), frozenset("defghi"), frozenset("fjklmn")
    assert sorted(len(p) for _, p in coaction._merge_pools([("ZA", a), ("ZA", b)])) == [9]          # 3 of 6
    assert sorted(len(p) for _, p in coaction._merge_pools([("ZA", a), ("ZA", c)])) == [6, 6]       # 1 of 6
    assert sorted(len(p) for _, p in coaction._merge_pools([("ZA", a), ("KE", b)])) == [6, 6]


EVE = [f"eve{k}" for k in range(6)]
T1 = "we stand with the people tonight and always"
T2 = "show up and be counted this weekend friends"


def evening(accounts, item, i, hour, text, market="ZA", gap=7):
    """Each account posts text on item on day(i) from hour UTC, gap minutes apart. With gap 7 five posts fall
    inside 30 minutes and only neighbours are within 10 minutes, so reordering the accounts on another evening
    repeats no pair and forms no co-action network."""
    return [row(f"{item}-{i}-{a}", a, utc(i, hour, gap * k), text=text, item=item, market=market)
            for k, a in enumerate(accounts)]


def reorder(accounts):
    return accounts[::2] + accounts[1::2]


@pytest.mark.parametrize("n,hour,market,second,expected", [
    (5, 17, "ZA", "other", 2),          # 19:00 SAST, then another item and template on another evening
    (4, 17, "ZA", "other", 0),          # four accounts
    (5, 15, "ZA", "other", 0),          # 17:00 SAST: before the evening
    (5, 15, "KE", "other", 2),          # 18:00 EAT: evening in Kenya
    (5, 16, "NG", "other", 0),          # 17:00 WAT
    (5, 17, "NG", "other", 2),          # 18:00 WAT
    (5, 17, "ZA", None, 0),             # one evening only: no shared-account structure
    (5, 17, "ZA", "same_item", 0),      # the repeat is on the same item, as a meme's fans do
    (5, 17, "ZA", "same_template", 0),  # the repeat carries the same template
    (5, 17, "ZA", "spread", 0),         # 11 minutes apart: five posts never fall inside 30 minutes
])
def test_same_evening_template(n, hour, market, second, expected):
    gap = 11 if second == "spread" else 7
    rows = evening(EVE[:n], "e1", 2, hour, T1, market, gap) + background("e1", 2, market=market)
    if second:
        item = "e1" if second == "same_item" else "e2"
        text = T1 if second == "same_template" else T2
        rows += evening(reorder(EVE[:n]), item, 1, hour, text, market, gap) + background(item, 1, market=market)
    res = coaction.analyse(rows, D)
    assert [s["signal"] for s in res["signals"]] == ["same_evening_template"] * expected
    for s in res["signals"]:                                          # n burst posts of n + BG on the item
        assert (s["accounts"], s["item_posts_share"], s["market"]) == (n, pytest.approx(n / (n + BG)), market)
    assert len({s["component_id"] for s in res["signals"]}) <= 1      # one pool, one component id
    assert res["networks"] == [] and res["scores"] == {}


def test_a_pool_bursting_twice_on_one_item_is_one_signal():
    rows = evening(EVE[:5], "e1", 3, 17, T1) + evening(reorder(EVE[:5]), "e1", 2, 17, T1)
    # a third burst whose only pairs within 10 minutes are eve0 with eve3 and eve1 with eve2
    rows += [row(f"e2-{a}", a, utc(1, 17, m), text=T2, item="e2")
             for a, m in zip(["eve0", "eve3", "eve4", "eve1", "eve2"], [0, 1, 12, 23, 29])]
    rows += background("e1", 3, 2) + background("e2", 1)
    res = coaction.analyse(rows, D)
    assert signals(res) == [("e1", "same_evening_template"), ("e2", "same_evening_template")]
    by = {s["item_id"]: s for s in res["signals"]}
    assert (by["e1"]["item_posts_share"], by["e1"]["accounts"]) == (pytest.approx(10 / (10 + 2 * BG)), 5)
    assert res["networks"] == []


def meme_rows(n=40, i=1):
    """A meme: n accounts post the same caption on one hashtag and one sound through one evening,
    9 minutes apart, each once."""
    text = "Doing the dance challenge with my crew tonight #skibidance"
    out = []
    for k in range(n):
        when = utc(i, 16, 9 * k)                  # 18:00 SAST onwards
        pid, a = f"meme-{k}", f"fan{k}"
        out.append(row(pid, a, when, text, ["#skibidance"], item="skibidance"))
        out.append(row(pid, a, when, text, ["#skibidance"], item="snd", kind="sound", key="tiktok:777"))
    return out


VOCAB = ("sun rain bok try loss win power grid dark night stage four ref fans cheer boo kick scrum lineout "
         "ball coach team braai fire meat beer wait long queue eskom candle cold slow city town street bus "
         "taxi train noise quiet music drum bass song dance crew home late early").split()
MEME = "doing the dance challenge with my crew tonight #amapiano"


def fans_week(seed, item, text=None, n=40, days=7, p=0.9, evenings=False):
    """n fans each post on item on about p of the last days, at a random minute of the day, or of the
    18:00 to 24:00 SAST evening when evenings is set. With text None every post has its own random words;
    otherwise every post carries text."""
    rng = random.Random(f"{seed}-{item}-{evenings}")
    hour, span = (16, 360) if evenings else (0, 1440)
    out = []
    for i in range(days):
        for k in range(n):
            if rng.random() < p:
                words = text or " ".join(rng.choice(VOCAB) for _ in range(7))
                out.append(row(f"{item}-{i}-{k}", f"fan{k}", utc(i, hour, rng.randrange(span)), words, item=item))
    return out


@pytest.mark.parametrize("evenings", [False, True])
def test_a_meme_on_one_sound_and_one_caption_is_never_flagged(evenings):
    rows = meme_rows()
    assert coaction.coaction_events(rows)                        # the caption links neighbours in time
    res = coaction.analyse(rows, D)
    assert res["signals"] == [] and res["networks"] == [] and res["scores"] == {}
    for seed in (1, 2, 3):                                        # the same fans on most days for a week
        res = coaction.analyse(fans_week(seed, "amapiano", MEME, evenings=evenings), D)
        assert res["signals"] == [] and res["networks"] == [], seed


@pytest.mark.parametrize("evenings", [False, True])
def test_the_same_fans_on_unrelated_items_are_never_flagged(evenings):
    for seed in (1, 2, 3):
        rows = (fans_week(seed, "amapiano", MEME, evenings=evenings) + fans_week(seed, "springbok", evenings=evenings)
                + fans_week(seed, "load_shedding", evenings=evenings))
        res = coaction.analyse(rows, D)
        assert res["signals"] == [] and res["networks"] == [], seed


# Fan communities (the reviewer's scenarios): the same fans on two or three memes and a few unrelated topics in
# one week, posting around a 20:30 SAST peak. Every meme has its own caption, so fans who post two memes at
# the peak share content across unrelated items by chance.

MEMES = ("amapiano", "tshwala", "pantsula")
TOPICS = ("springbok", "load_shedding", "braai")
WORDS = ("sun road bread river blue lamp city song jump rain dog tree phone green chair wave kota taxi mall "
         "braai").split()


def fandom(n, memes, p, sd, seed, days=7, p_topic=0.3):
    rng = random.Random(seed)
    out = []
    for i in range(days):
        for m in memes:
            caption = f"when the {m} beat drops and you cannot stop dancing #{m}"
            for a in range(n):
                if rng.random() < p:
                    hour = min(max(rng.gauss(18.5, sd), 6), 21.99)          # UTC; 20:30 SAST peak
                    out.append(row(f"{m}-{i}-{a}", f"fan{a}", at(day(i), 0) + timedelta(hours=hour), caption,
                                   [f"#{m}"], item=m))
        for t in TOPICS:
            for a in range(n):
                if rng.random() < p_topic / days * 3:
                    words = " ".join(rng.choice(WORDS) for _ in range(7))
                    out.append(row(f"{t}-{i}-{a}", f"fan{a}", at(day(i), 0) + timedelta(hours=rng.uniform(4, 21.9)),
                                   f"{words} #{t}", [f"#{t}"], item=t))
    return out


COMMUNITIES = {150: (.6, 1.0), 80: (.8, 1.5), 40: (.5, 2.0), 30: (.5, 2.0), 25: (.6, 1.5), 20: (.5, 2.0)}


@pytest.mark.parametrize("memes", [2, 3])
@pytest.mark.parametrize("n", sorted(COMMUNITIES))
def test_a_fan_community_on_several_memes_is_never_flagged(n, memes):
    p, sd = COMMUNITIES[n]
    for seed in (0, 1, 2):
        res = coaction.analyse(fandom(n, MEMES[:memes], p, sd, seed), D)
        assert res["signals"] == [] and res["networks"] == [], (n, memes, seed)


def verdict(res):
    """Per item, as state.sql reads coord_signals: likely (a network share of 0.2, or two signals) or check."""
    by = defaultdict(list)
    for s in res["signals"]:
        by[s["item_id"]].append(s)
    return {item: "likely" if max([s["item_posts_share"] for s in ss if s["signal"] == "coaction"] or [0]) >= .2
            or len({(s["signal"], s["component_id"]) for s in ss}) >= 2 else "check" for item, ss in by.items()}


def hired(seed):
    """8 hired accounts push three unrelated tags on three evenings, each with a new template, 6 of the 8
    posting inside 25 minutes, amid 40 fans posting their own words on each tag all day."""
    rng = random.Random(f"hired-{seed}")
    out = []
    for i, (tag, text) in enumerate([("kura", "vote for change this time lets go all of us"),
                                     ("haki", "justice now we demand answers from them all"),
                                     ("tuko", "we are together tonight and every night after")]):
        for k in rng.sample(range(8), 6):
            out.append(row(f"h-{tag}-{k}", f"hire{k}", utc(i + 1, 16, rng.randrange(25)), text, item=tag))
        out += fans_week(seed, tag, days=5)          # fans on every day a hired burst falls on
    return out


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_hired_pool_on_three_unrelated_tags_reads_likely_coordinated(seed):
    assert verdict(coaction.analyse(hired(seed), D)) == {"kura": "likely", "haki": "likely", "tuko": "likely"}


def owned_tags(seed):
    """The reviewer's owned-tag case (TRUST.md 5.2, hashtag for hire): 6 of 8 hired accounts post a new
    template on each of three tags nobody else uses, inside 25 minutes of an evening, amid a 40-fan community
    posting two memes."""
    rng = random.Random(100 + seed)
    out = []
    for n, tag in enumerate(("kura", "vote2027", "hustler")):
        text = f"we the people of {tag} stand together " + " ".join(rng.choice(WORDS) for _ in range(3))
        for a in rng.sample(range(8), 6):
            out.append(row(f"h-{tag}-{a}", f"hired{a}", at(day(1 + n), 19) + timedelta(minutes=rng.uniform(0, 25)),
                           f"{text} #{tag}", [f"#{tag}"], item=tag))
    return out + fandom(40, MEMES[:2], .5, 2.0, seed, p_topic=0)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_a_hired_pool_on_owned_tags_reads_likely_coordinated(seed):
    v = verdict(coaction.analyse(owned_tags(seed), D))
    assert v == {"kura": "likely", "vote2027": "likely", "hustler": "likely"}, v


def show(seed, others, viewers=12, active=7, spread_min=20):
    """A daily show: each day 7 of 12 viewers share that day's new official episode link with the same caption
    within spread_min minutes of airtime (20:30 SAST) for 7 days, amid others posting their own words."""
    rng = random.Random(seed)
    out = []
    for i in range(7):
        link = f"https://tv.example/ep{i}"
        for a in rng.sample(range(viewers), active):
            when = at(day(i), 18) + timedelta(minutes=30 + rng.uniform(0, spread_min))
            out.append(row(f"s-{i}-{a}", f"viewer{a}", when, f"new episode is up {link}", ["#showname"],
                           item="showname"))
        for b in range(others):
            words = " ".join(rng.choice(WORDS) for _ in range(7))
            out.append(row(f"o-{i}-{b}", f"other{i}-{b}", at(day(i), 8) + timedelta(hours=rng.uniform(0, 13.9)),
                           f"{words} #showname", ["#showname"], item="showname"))
    return out


@pytest.mark.parametrize("others", [15, 20, 30])
def test_viewers_sharing_the_days_episode_link_are_never_flagged(others):
    for seed in (0, 1, 2):
        res = coaction.analyse(show(seed, others), D)
        assert res["signals"] == [] and res["networks"] == [], (others, seed)


@pytest.mark.parametrize("spec,qualifies", [
    ([(1, 8, 1), (1, 14, 2)], True),                # the same day, two different links: a bot's pattern
    ([(1, 8, 1), (1, 14, 1)], False),               # the same day, the same link
])
def test_two_links_on_one_day_repeat(spec, qualifies):
    rows = []
    for n, (i, hour, link) in enumerate(spec):
        rows += [row(f"{a}{n}", a, utc(i, hour, m), f"great deal https://promo.example/{link} today")
                 for a, m in (("a", 0), ("b", 2))]
    events = coaction.coaction_events(rows)
    assert (coaction.qualifying_pairs(events) == {("ZA", ("a", "b"))}) is qualifies


def test_a_friend_group_on_its_own_niche_tag_is_never_flagged():
    friends = [f"friend{k}" for k in range(6)]
    captions = [["braai at mine later", "who is bringing ice", "lekker day for it", "fire is going now",
                 "come through guys", "the meat is on"],
                ["hike was amazing", "legs are finished", "best view in town", "next week again",
                 "who took the photos", "coffee after that"]]
    rows = [row(f"f-{i}-{k}", a, at(day(i), 17) + timedelta(minutes=3 * k), f"{captions[i - 1][k]} #ourcrew",
                ["#ourcrew"], item="ourcrew") for i in (1, 2) for k, a in enumerate(friends)]
    res = coaction.analyse(rows, D)
    assert res["signals"] == [] and res["networks"] == []


# DuckDB: run_coaction, coord_score and item_state


@pytest.fixture
def con():
    c = duck.connect()
    for stmt in [duck.strip_leading_comments(s) for s in sqlrun.split(sqlrun.render(
            (coaction.SQL.parent / "waves.sql").read_text(encoding="utf-8"), "core", "agent"))]:
        c.execute(duck.create_statement(stmt))
    yield c
    c.close()


def add_posts(w, rows):
    """Load analyse-shaped rows into a World: posts, one sighting each, post_items, creators, map items."""
    seen = set()
    for r in rows:
        w.items.add(r["item_id"])
        w.t["core.post_items"].append({"post_id": r["post_id"], "item_id": r["item_id"], "via": "hashtag"})
        if r["post_id"] in seen:
            continue
        seen.add(r["post_id"])
        w.t["core.posts"].append({"post_id": r["post_id"], "platform": "tiktok", "creator_id": r["creator_id"],
                                  "creator_tier_at_post": "micro", "published_at": r["published_at"],
                                  "post_date": r["first_day"], "text": r["text"], "hashtags": r["hashtags"]})
        w.t["core.post_observations"].append(obs(r["post_id"], r["first_day"], "unbiased_rank", "sweep",
                                                 market=r["market"]))
        if r["creator_id"] not in {c["creator_id"] for c in w.t["core.creators"]}:
            w.t["core.creators"].append(creator(r["creator_id"]))


def run_step(con, client=None, run_id="coaction-test"):
    counts = coaction.run_coaction(client or duck.Client(con), D, run_id, RULE, core="core", agent="agent")
    duck.load(con, "agent.runs", [run("coaction", D, run_id, hour=13)])
    return counts


def clean_rows():
    """30 posts by 10 accounts over 7 days, each in its own 10-minute bucket, nothing shared."""
    return [row(f"clean-{j}", f"clean-a{j % 10}", utc(j % 7, 1, 11 * j), item="clean") for j in range(30)]


def test_end_to_end_state_reads_the_coaction_run(con):
    w = World()
    w.protocol("feed_tiktok", 9)
    for it in ("camp", "clean", "skibidance"):
        for i in (2, 1, 0):
            w.counter(it, "feed_tiktok", i, 1.0)
    camp = copost(NET, ["camp"], 1) + copost(NET, ["camp2"], 1, hour=14) + organic("camp", 24)
    camp += background("camp2", 1)
    add_posts(w, camp + clean_rows() + meme_rows())
    w.build(con)

    counts = run_step(con)
    assert counts["coord_signals"] == 2 and counts["networks"] == 1

    cur = duck.query(con, "SELECT * FROM {core}.v_coord_signals_current c WHERE c.metric_date = @d", {"d": D})
    assert sorted((r["item_id"], r["signal"]) for r in cur) == [("camp", "coaction"), ("camp2", "coaction")]
    assert {r["run_id"] for r in cur} == {"coaction-test"} and {r["rule_version"] for r in cur} == {RULE}
    assert [r["item_posts_share"] for r in cur if r["item_id"] == "camp"] == [pytest.approx(0.2)]

    st = detect(con)
    assert st["camp"]["authenticity"] == "likely_coordinated" and st["camp"]["eligible"] is False
    assert st["clean"]["authenticity"] == "clear"
    assert st["skibidance"]["authenticity"] == "clear"


def test_one_network_under_a_fifth_of_posts_reads_check_pattern(con):
    w = World()
    w.protocol("feed_tiktok", 9)
    for i in (2, 1, 0):
        w.counter("camp", "feed_tiktok", i, 1.0)
    add_posts(w, copost(NET, ["camp"], 1) + copost(NET, ["camp2"], 1, hour=14) + organic("camp", 25)
              + background("camp2", 1))
    w.build(con)
    run_step(con)
    assert detect(con)["camp"]["authenticity"] == "check_pattern"   # 6 of 31 posts, one network signal


def test_coord_score_is_merged_on_existing_creators_only(con):
    w = World()
    add_posts(w, copost(NET, ["x"], 1) + copost(NET, ["y"], 1, hour=14) + background("x", 1) + background("y", 1))
    w.t["core.creators"] = [c for c in w.t["core.creators"] if c["creator_id"] in NET[:2]]
    w.t["core.creators"] += [creator("old", 2), creator("keep", 0)]
    w.load(con)
    before = duck.query(con, "SELECT c.creator_id, c.handle, c.account_created_at FROM {core}.creators c "
                             "ORDER BY c.creator_id")
    client = duck.Client(con)
    counts = run_step(con, client)
    after = duck.query(con, "SELECT c.creator_id, c.handle, c.account_created_at, c.coord_score "
                            "FROM {core}.creators c ORDER BY c.creator_id")
    assert [{k: v for k, v in r.items() if k != "coord_score"} for r in after] == before
    assert {r["creator_id"]: r["coord_score"] for r in after} == {"net0": 1, "net1": 1, "old": 0, "keep": 0}
    assert counts["coord_scores"] == 7                            # six members and old; keep is already 0
    merges = [s for s in client.sql if "MERGE" in s.upper()]
    assert merges and all("INSERT" not in s.upper() for s in merges)


def test_embeddings_are_read_from_post_enrichment(con):
    w = World()
    rows = []
    for i in (2, 1):
        for k, a in enumerate(NET[:5]):
            rows.append(row(f"e-{i}-{a}", a, utc(i, 8, k), text=f"words {k} {i}", item="x"))
    add_posts(w, rows + background("x", 2, 1))
    w.t["core.post_enrichment"] = [{"post_id": r["post_id"], "embedding": [1.0, 0.01 * n, 0.0]}
                                   for n, r in enumerate(rows)]
    w.load(con)
    assert run_step(con)["networks"] == 1


def test_run_coaction_only_appends_and_merges(con):
    w = World()
    add_posts(w, copost(NET, ["x"], 1) + copost(NET, ["y"], 1, hour=14) + background("x", 1) + background("y", 1))
    w.load(con)
    client = duck.Client(con)
    run_step(con, client)
    run_step(con, client, "coaction-second")
    for sql in client.sql:
        upper = sql.upper()
        for word in ("DELETE", "DROP", "TRUNCATE", "ALTER", "REPLACE", "CREATE"):
            assert word not in upper
        assert re.match(r"\s*(SELECT|WITH|INSERT|MERGE)\b", re.sub(r"--[^\n]*", "", upper))
    rows = duck.query(con, "SELECT c.run_id, COUNT(*) n FROM {core}.coord_signals c GROUP BY c.run_id ORDER BY 1")
    assert rows == [{"run_id": "coaction-second", "n": 2}, {"run_id": "coaction-test", "n": 2}]


def test_inserts_are_chunked(con, monkeypatch):
    monkeypatch.setattr(coaction, "CHUNK", 1)
    w = World()
    add_posts(w, copost(NET, ["x"], 1) + copost(NET, ["y"], 1, hour=14) + background("x", 1) + background("y", 1))
    w.load(con)
    client = duck.Client(con)
    run_step(con, client)
    assert len([s for s in client.sql if "INSERT INTO" in s.upper()]) == 2
    assert len([s for s in client.sql if "MERGE" in s.upper()]) == 6


# The detect job runs coaction for d before state


def test_job_runs_coaction_before_state_under_its_own_run(con):
    job_world(con)
    client, chain = JobClient(con), FakeChain(con)
    counts = job.run(client, D, chain=chain, core="core", agent="agent")
    rows = step_rows(con, "coaction")
    assert len(rows) == 1 and rows[0]["status"] == "ok"
    assert re.match(r"^coaction-20260920-[0-9a-f]{12}$", rows[0]["run_id"])
    assert json.loads(rows[0]["counts"]) == {**counts["coaction"], "model_usd": 0.0}
    # the runs rows only: the locality step also streams its verification rows into item_locality_verified
    stages = [r["stage"] for table, rs in client.inserted if table.endswith(".runs") for r in rs]
    assert stages == ["aggregate"] * 4 + ["stats", "coaction", "locality", "breakout", "watch", "seeds", "forecast"]  # 3 catch-up days, then d
    posts_at = client.sql.index(sqlrun.render(coaction.posts_sql(), "core", "agent"))
    state_at = next(i for i, s in enumerate(client.sql) if "INSERT INTO" in s and "item_state" in s)
    assert posts_at < state_at


def _without_libraries(monkeypatch):
    """Make datasketch and networkx unimportable, as on an image built before they were added."""
    for name in ("datasketch", "networkx"):
        for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            monkeypatch.delitem(sys.modules, mod)
        monkeypatch.setitem(sys.modules, name, None)
    coaction._minhash.cache_clear()


def test_job_skips_coaction_when_its_libraries_are_missing_and_still_writes_state(con, monkeypatch):
    job_world(con)
    _without_libraries(monkeypatch)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    rows = step_rows(con, "coaction")
    assert [r["status"] for r in rows] == ["skipped"]
    assert "ModuleNotFoundError" in rows[0]["error"] and rows[0]["finished_at"] is not None
    assert counts["coaction"]["status"] == "skipped"
    assert chain.of("finish")[0][2] == "ok" and chain.of("start_next") == [("start_next", "detect", D)]
    state = duck.query(con, "SELECT * FROM {core}.v_item_state_current s WHERE s.metric_date = @d", {"d": D})
    assert {r["item_id"] for r in state} == {"new", "two"}
    assert {r["authenticity"] for r in state} == {"not_assessed"}


def test_a_failing_coaction_is_recorded_failed_and_state_still_runs(con, monkeypatch):
    job_world(con)

    def boom(*a, **k):
        raise ValueError("coord_signals insert failed")

    monkeypatch.setattr(coaction, "run_coaction", boom)
    chain = FakeChain(con)
    counts = job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    rows = step_rows(con, "coaction")
    assert [r["status"] for r in rows] == ["failed"]
    assert rows[0]["error"] == "ValueError: coord_signals insert failed"
    assert counts["coaction"] == {"status": "failed", "error": rows[0]["error"]}
    assert chain.of("finish")[0][2] == "ok"
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.v_item_state_current s WHERE s.metric_date = @d",
                      {"d": D}) == [{"n": 2}]


def test_detect_job_imports_without_datasketch_or_networkx():
    code = ("import sys\n"
            "sys.modules['datasketch'] = None\n"
            "sys.modules['networkx'] = None\n"
            "import core.detect.job\n"
            "print('ok')\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[3],
                         capture_output=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


# BigQuery dry runs (skipped unless F42_BQ=1)


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
def test_bigquery_dry_runs():
    from google.cloud import bigquery

    client = bigquery.Client(project=job.PROJECT)

    def dry(sql, params):
        result = client.query(sqlrun.render(sql), job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, query_parameters=params))
        assert result.dry_run

    dry(coaction.posts_sql(), [sqlrun._param("d", D)])
    dry(coaction.ENRICHMENT_SQL, [aggregate._struct_array("ids", [{"post_id": "p1"}], coaction.ID_FIELDS)])
    dry(coaction.FLAGGED_SQL, [])
    dry(coaction.INSERT_SQL, [aggregate._struct_array("rows", [{
        "metric_date": D, "item_id": "i", "market": "ZA", "run_id": "r", "signal": "coaction",
        "component_id": "coaction-abc", "accounts": 5, "item_posts_share": 0.2, "rule_version": RULE}],
        coaction.SIGNAL_FIELDS)])
    dry(coaction.MERGE_SQL, [aggregate._struct_array("rows", [{"creator_id": "c", "coord_score": 1}],
                                                     coaction.SCORE_FIELDS)])
