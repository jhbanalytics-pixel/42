"""Tests for core/collect/job.py and core/collect/writers.py: the 02:00 collect job and its BigQuery writes.

No network and no cloud: a scripted fake client (or the real client over a fake HTTP callable and memory
stores), a fake BigQuery that records statements and load jobs and plays the posts MERGE on a dict, and
injected item and geo functions that follow lane L2's rules.
"""

import copy
import functools
import json
import random
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import yaml

from core.collect import chain, ids, job, writers
from core.collect import local_sources as ls
from core.collect.gdelt import SEED_FIELDS, blocked
from core.collect.parse import parse
from core.collect.socialcrawl_client import (
    KEY_ENV, PRICED, Result, SocialCrawlClient, quote_for, split_vendor_labels)
from core.collect.stores import MemoryLedgerStore, MemoryRawStore
from core.collect.tests.test_gdelt import (COHORT_FORMS, NAME_NEIGHBOURS, NAME_TAGS, ORDINARY_SWEEP, ROUND_FORMS,
                                           UNICODE_AGE_LENS)
from core.collect.tests.test_local_sources import HOSTS, PROFILES, SCRAPE, FakeGet, envelope
from core.collect.tests.test_local_sources import FakeClient as LocalClient

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads((Path(__file__).resolve().parent / "fixtures" / "job_responses.json").read_text(encoding="utf-8"))
TUESDAY = date(2026, 9, 29)
MONDAY = date(2026, 10, 5)
NOW = datetime(2026, 9, 29, 0, 5, tzinfo=timezone.utc)  # 02:05 SAST on Tuesday 29 September
STAGE_1A = {"1", "2", "3", "4", "5", "6", "7", "8", "11", "12", "14", "16", "22", "23", "23a"}


# Fakes

KINDS = {"topic", "hashtag", "sound", "format", "meme", "creator", "brand", "event"}


def fake_item_id(kind, raw, platform=None):
    """Lane L2's rules: an unknown kind or an empty key raises ValueError; sound and creator need platform."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    key = str(raw or "").strip().lstrip("#@").casefold()
    if not key:
        raise ValueError("empty key")
    if kind in ("sound", "creator"):
        if not platform:
            raise ValueError(f"{kind} needs a platform")
        key = f"{platform}:{key}"
    return f"{kind}|{key}"


class FakeGeo:
    """Lane L2's rules: only ZA, NG and KE; ext_region counts only on TikTok."""

    def __call__(self, platform, market, *, ext_region=None, home_market=None, profile_location=None,
                 text=None, language=None):
        if market not in ("ZA", "NG", "KE"):
            raise ValueError(f"geo_for_post takes ZA, NG or KE, not {market}")
        if platform == "tiktok" and ext_region:
            return ext_region, 0.9, "ext_region"
        return None, None, None


def body_for(route, params, market=None):
    if route in ("tiktok/profile", "instagram/profile/about"):
        return {"success": True, "data": {"author": {"username": params["handle"], "location": None,
                                                     "ext": {"country": None}}}}
    if route == "prism/post-stats":
        return {"success": True, "data": {"items": [
            {"url": u, "status": "ok", "engagement": {"views": 100, "likes": 5, "comments": 1, "shares": 0}}
            for u in params["urls"]]}}
    region = params.get("region") or market
    return copy.deepcopy(FIXTURE.get(f"{route}|{region}") or FIXTURE[route])


class FakeClient:
    """Stands in for SocialCrawlClient.call. script(n, route, market) returns a status or None for ok."""

    def __init__(self, script=None):
        self.script = script or (lambda n, route, market: None)
        self.calls = []

    def account_profile(self, platform, handle, **kwargs):
        route = {"tiktok": "tiktok/profile", "instagram": "instagram/profile/about"}[platform]
        return self.call(route, {"handle": handle}, **kwargs)

    def call(self, route, params=None, *, method=None, market=None, item_id=None, seed_key=None, agent=None,
             lane=None, use_cache=True):
        params = dict(params or {})
        method = method or PRICED[route].method
        quote = quote_for(route, method, params)  # a refused parameter fails the test here
        self.calls.append({"route": route, "params": params, "market": market, "lane": lane,
                           "seed_key": seed_key, "item_id": item_id, "use_cache": use_cache})
        self.charged = getattr(self, "charged", [])
        status = self.script(len(self.calls), route, market)
        if status:
            return Result(status, route, credits_quoted=quote, reason="scripted")
        stored, items, labels = split_vendor_labels(body_for(route, params, market))
        self.charged.append({"route": route, "market": market, "credits": quote})
        return Result("ok", route, "hash", 200, quote, quote, False, stored, items, labels)


class FakeJob:
    def __init__(self, rows=(), total_bytes_processed=None, total_bytes_billed=None):
        self.rows = list(rows)
        self.total_bytes_processed = total_bytes_processed
        self.total_bytes_billed = total_bytes_billed

    def result(self, *, retry=None, job_retry=None):
        return self.rows


class FakeTable:
    schema = []


class FakeBQ:
    """Records queries and load jobs; plays MERGE into posts on a dict keyed by post_id, the creators MERGE
    on (platform, creator_id), and serves raw_responses rows to the backfill."""

    def __init__(self, previous=(), reference=(), pulls=(), seeds=(), watches=(), raw=()):
        self.raw = list(raw)
        self.raw_bytes = 1_000_000
        self.dry_runs = []
        self.configs = []
        self.creators = {}
        self.fail_creators = False
        self.seeds = list(seeds)
        self.previous = list(previous)
        self.pulls = list(pulls)
        self.reference = list(reference)
        self.watches = list(watches)
        self.queries = []
        self.loads = []
        self.posts = {}
        self.mapped = []

    def query(self, sql, job_config=None, **kw):
        params = {p.name: p for p in (job_config.query_parameters if job_config else [])}
        if getattr(job_config, "dry_run", False):
            self.dry_runs.append((sql, params))
            return FakeJob(total_bytes_processed=self.raw_bytes)
        self.queries.append((sql, params))
        self.configs.append(job_config)
        if "post_id IN UNNEST(@ids)" in sql:
            return FakeJob([{"post_id": post_id} for post_id in params["ids"].values if post_id in self.posts])
        if "raw_responses" in sql:
            return FakeJob(self.raw)
        if "seed_queue" in sql:
            return FakeJob(self.seeds)
        if "v_watches_current" in sql:
            return FakeJob(self.watches)
        if sql.lstrip().upper().startswith("MERGE") and "cultural_map" in sql:
            self.mapped += [s.struct_values for s in params["items"].values]
            return FakeJob()
        if sql.lstrip().upper().startswith("MERGE") and "COALESCE(T." in sql:
            rows = [s.struct_values for s in params["rows"].values]
            for row in rows:
                existing = self.posts.get(row["post_id"])
                if existing is None:
                    self.posts[row["post_id"]] = dict(row)
                else:
                    existing.update({key: value for key, value in row.items()
                                     if existing.get(key) is None and value is not None})
            return FakeJob(total_bytes_billed=0)
        if sql.lstrip().upper().startswith("MERGE") and writers.table("creators") in sql:
            if self.fail_creators:
                raise RuntimeError("creators MERGE failed")
            rows = [s.struct_values for s in params["rows"].values]
            keys = [(r["platform"], r["creator_id"]) for r in rows]
            assert len(keys) == len(set(keys)), "a MERGE source may hold each creator once"
            for key, row in zip(keys, rows):
                if key not in self.creators:
                    self.creators[key] = dict(row)
                elif row["last_seen"] >= self.creators[key]["last_seen"]:
                    kept = self.creators[key]
                    kept.update({k: row[k] for k in writers.CREATOR_UPDATES if row[k] is not None})
                    kept["last_seen"] = row["last_seen"]
            return FakeJob()
        if sql.lstrip().upper().startswith("MERGE") and "WHEN NOT MATCHED" not in sql:
            for row in (s.struct_values for s in params["rows"].values):  # the backfill's geo fill
                post = self.posts.get(row["post_id"])
                if post is not None and post["geo_market"] is None and row["geo_market"] is not None:
                    post.update({k: row[k] for k in writers.GEO})
            return FakeJob()
        if sql.lstrip().upper().startswith("MERGE"):
            rows = [s.struct_values for s in params["rows"].values]
            ids = [r["post_id"] for r in rows]
            assert len(ids) == len(set(ids)), "a MERGE source may hold each post_id once"
            for row in rows:
                if row["post_id"] not in self.posts:
                    self.posts[row["post_id"]] = dict(row)
                else:
                    post = self.posts[row["post_id"]]
                    if any(row[k] is not None for k in ("views", "likes", "comments", "shares")):
                        post.update({k: row[k] for k in writers.METRICS})
                    if post["geo_market"] is None and row["geo_market"] is not None:
                        post.update({k: row[k] for k in writers.GEO})
            return FakeJob()
        if "MAX(c.pull_seq)" in sql:
            return FakeJob(self.pulls)
        if "item_counter_daily" in sql:
            return FakeJob(self.previous)
        if "collection_health" in sql:
            return FakeJob(self.reference)
        raise AssertionError("unexpected query")

    def load_table_from_json(self, rows, table_id, job_config=None):
        self.loads.append((table_id, [dict(r) for r in rows], job_config))
        return FakeJob()

    def get_table(self, table_id):
        return FakeTable()

    def loaded(self, table):
        return [row for table_id, rows, _ in self.loads if table_id.endswith("." + table) for row in rows]


class FakeJobs:
    def __init__(self, fail=False):
        self.started = []
        self.fail = fail

    def deployed(self, name):
        raise AssertionError(f"no GET: job identities lack run.jobs.get, so {name} is never probed")

    def run(self, name, env):
        if self.fail:
            raise RuntimeError("Cloud Run Admin API unavailable")
        self.started.append((name, env))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("RUN_DATE", "FORCE_RERUN", "CLOUD_RUN_EXECUTION", "COLLECT_CAP_OVERRIDE", "X_TRENDS_SEED",
                 "CHAIN_UNDERSTAND"):
        monkeypatch.delenv(name, raising=False)


def collect(client, day=TUESDAY, **kw):
    return job.collect(client, day, "collect-test", item_id_fn=kw.pop("item_id_fn", fake_item_id),
                       geo_fn=FakeGeo(), clock=lambda: NOW, **kw)


def no_page(url):
    """The free getter for runs that do not look at the local sources: every URL answers 404."""
    return 404, ""


def no_public_feed(url, *, timeout, max_bytes):
    return 403, {}, b""


def run_main(argv, *, client=None, bq=None, runs=None, jobs=None, env=None, caps_seen=None, clock=None, get=None,
             public_feed_transport=None):
    def make_client(run_id, caps):
        if caps_seen is not None:
            caps_seen.append(caps)
        return client or FakeClient()

    return job.main(argv, env=env or {}, runs=runs or chain.MemoryRunsStore(), jobs=jobs or FakeJobs(),
                    bq=bq or FakeBQ(), make_client=make_client, fns=(fake_item_id, FakeGeo()),
                    clock=clock or (lambda: NOW), get=get or no_page,
                    public_feed_transport=public_feed_transport or no_public_feed)


@functools.lru_cache(maxsize=None)
def _desk_items(market):
    hubs = job.load_config()["hubs"]["markets"][market.lower()]
    return tuple((e["platform"], e["handle"]) for e in hubs.get("culture_desk") or [])


def _desk(call):
    """The culture desk's prism/profiles read: its items are the market's hubs.yaml culture_desk list."""
    return call.route == "prism/profiles" and tuple(
        (i["platform"], i["handle"]) for i in call.params["items"]) == _desk_items(call.market)


def all_planned(day=TUESDAY, **kw):
    return [call for calls in job.plan(day, **kw)["calls"].values() for call in calls]


# The plan

@pytest.mark.parametrize("day", [TUESDAY, MONDAY])
def test_plan_covers_the_stage_1a_rows_for_every_market_within_the_collect_share(day):
    planned = job.plan(day)
    calls = [c for cs in planned["calls"].values() for c in cs]
    for market in ("ZA", "NG", "KE"):
        rows = {c.row for c in planned["calls"][market]}
        assert {"1", "3", "6", "7", "8", "11", "12", "14", "16", "22", "23", "23a"} <= rows, market
    assert {c.row for c in planned["calls"]["GLOBAL"]} == {"4"}
    assert {c.row for c in calls} - {job.REELS_ROW} == STAGE_1A
    assert all(c.market == "ZA" for c in calls if c.row == "2")
    total = sum(c.hold() for c in calls)
    assert 0 < total <= job.load_caps()["ENGINE_DAILY"]["collect"]
    assert planned["total"] == total


def test_plan_holds_price_and_every_planned_parameter_is_accepted():
    for call in all_planned(MONDAY, x_trends=True):
        assert call.hold() == quote_for(call.route, call.method, call.params) > 0


def test_industry_board_only_on_monday_and_thursday():
    def boards(day):
        return [c.params for c in all_planned(day) if c.route == "tiktok/hashtags/popular"]

    assert boards(TUESDAY) == [{"countryCode": "ZA", "period": "7"}]
    assert boards(MONDAY) == [{"countryCode": "ZA", "period": "7"}, {"countryCode": "ZA", "period": "7", "industry": "all"}]
    assert len(boards(date(2026, 10, 1))) == 2


def test_local_feed_pulls_per_market_with_running_pull_numbers():
    assert job.FEED_PULLS == {"ZA": 3, "NG": 3, "KE": 1}
    feed = [c for c in all_planned() if c.route == "tiktok/trending"]
    for market, n in job.FEED_PULLS.items():
        pulls = [c for c in feed if c.market == market]
        assert [c.pull_seq for c in pulls] == list(range(1, n + 1))
        assert all(c.params == {"region": market, "feed": "local"} for c in pulls)
        assert [c.use_cache for c in pulls] == [True, False, False][:n]
        assert all(c.lane_class() == "unbiased_rank" for c in pulls)


@pytest.mark.usefixtures("collect_share_2000")
def test_ke_feed_credits_move_to_country_filtered_search():
    planned = job.plan(TUESDAY)["calls"]

    def credits(market, route):
        return sum(c.hold() for c in planned[market] if c.route == route)

    for market in ("ZA", "NG", "KE"):
        assert credits(market, "tiktok/trending") + credits(market, "tiktok/search/top") == 135
    ke_search = [c for c in planned["KE"] if c.route == "tiktok/search/top"]
    assert len(ke_search) == 65 and all(c.params["country"] == "KE" for c in ke_search)
    explore = [c for c in ke_search if c.lane == "exploration"]
    assert 0.10 <= len(explore) / len(ke_search) <= 0.15
    assert len([c for c in planned["ZA"] if c.route == "tiktok/search/top"]) == 60


def test_parse_reads_region_and_sound_from_the_live_post_ext_shape():
    live = {"success": True, "data": {"items": [{
        "computed": {"language": "sw"}, "vendor_labels": {"relevance": 0.5},
        "post": {"author": {"username": "ke.live"}, "content": {"text": "Sasa #nairobi"},
                 "engagement": {"views": 10, "likes": 1, "comments": 0, "shares": 0},
                 "ext": {"author_id": "a1", "content_language": "sw", "music_id": "m9", "on_screen_texts": [],
                         "published_at_epoch": 1790600000, "region": "KE"},
                 "flags": {}, "id": "7500000000000000099", "published_at": "2026-09-28T20:00:00Z",
                 "url": "https://www.tiktok.com/@ke.live/video/7500000000000000099"}}]}}
    out = parse("tiktok/trending", {"region": "KE", "feed": "local"}, "KE", live, NOW, "r",
                item_id_fn=fake_item_id, geo_fn=FakeGeo(), pull_seq=1)
    [post] = out["posts"]
    assert (post["geo_market"], post["geo_source"], post["sound_id"]) == ("KE", "ext_region", "m9")


@pytest.mark.usefixtures("collect_share_2000")
def test_row_5_plans_a_five_credit_call_for_every_configured_hub():
    config = job.load_config()
    planned = job.plan(TUESDAY, config=config)
    calls = [c for c in all_planned(TUESDAY, config=config) if c.route == "instagram/location/posts"]
    for market in ("ZA", "NG", "KE"):
        hubs = [str(h) for h in config["markets"][market.lower()]["instagram_locations"]["values"]]
        assert [c.params["location_id"] for c in calls if c.market == market] == hubs
    for call in calls:
        assert call.row == "5" and call.source_market == call.market
        assert call.hold() == 5
    assert planned["total"] == 1891  # 1830 and one row 14i reel search in the room left
    skips = [s for s in planned["skipped"] if s["route"] == "instagram/location/posts"]
    assert skips == []


def test_row_5_health_row_stays_invalid_for_markets_without_configured_ids():
    config = job.load_config()
    config["markets"]["ng"].pop("instagram_locations")
    config["markets"]["ke"].pop("instagram_locations")
    run = collect(FakeClient(), config=config)
    rows = writers.health_rows(run.records, run.posts, {}, "collect-test")
    ig = {r["market"]: r for r in rows if r["series"] == "ig_location"}
    assert set(ig) == {"ZA", "NG", "KE"}
    assert (ig["ZA"]["calls"], ig["ZA"]["calls_ok"], ig["ZA"]["units_planned"], ig["ZA"]["units_ok"]) == (1, 1, 1, 1)
    assert ig["ZA"]["valid"] is True and ig["ZA"]["invalid_reason"] is None
    assert all(ig[m]["calls"] == 0 and ig[m]["valid"] is False and ig[m]["invalid_reason"] == "calls: skipped"
               for m in ("NG", "KE"))
    assert all(r["day"] == "2026-09-29" for r in ig.values())


def test_x_trends_seed_is_absent_unless_the_flag_is_on():
    assert not [c for c in all_planned() if c.route == "web/scrape"]
    seeds = [c for c in all_planned(x_trends=True) if c.route == "web/scrape"]
    assert [c.market for c in seeds] == ["ZA", "NG", "KE"]
    assert all(c.row == "23b" and c.params["url"].startswith("https://") for c in seeds)
    assert job.x_trends_on({}) is False
    assert job.x_trends_on({"X_TRENDS_SEED": "1"}) is True


def test_the_culture_desk_panel_is_an_own_feed_of_its_market():
    # Albert's yes to D2 (4 Oct 2026): culture desk posts count as own-feed local, as the curated panel's do.
    # The X hubs stay without a source market.
    calls = all_planned()
    desk = [c for c in calls if _desk(c)]
    assert len(desk) == len(job.MARKETS)
    assert all(c.source_market == c.market for c in desk)
    assert all(c.source_market is None for c in calls if c.route == "twitter/user/tweets")


def test_panels_carry_a_protocol_hashed_from_the_hub_list():
    calls = all_planned()
    desk = [c for c in calls if _desk(c)]
    curated = [c for c in calls if c.route == "prism/profiles" and not _desk(c)]
    x_hub = [c for c in calls if c.route == "twitter/user/tweets"]
    assert len(desk) == 3 and all(len(c.params["items"]) == 12 and c.params["include"] == "posts" for c in desk)
    # The day's rotation goes out in batches of at most 25 profiles, all on the same panel protocol.
    assert curated and all(len(c.params["items"]) <= job.PROFILES_PER_CALL for c in curated)
    assert len({c.protocol for c in curated}) == len(job.MARKETS)
    assert all(len({c.protocol for c in curated if c.market == m}) == 1 for m in job.MARKETS)
    assert all(c.source_market == c.market for c in curated)
    assert all(c.params["since"] == "2026-09-28" for c in desk + x_hub)
    assert all(re.fullmatch(r"panel:[0-9a-f]{12}", c.protocol) for c in desk + x_hub)
    za_x = {c.protocol for c in x_hub if c.market == "ZA"}
    assert len(za_x) == 1 and za_x != {c.protocol for c in x_hub if c.market == "NG"}
    changed = copy.deepcopy(job.load_config())
    changed["hubs"]["markets"]["za"]["x"] = changed["hubs"]["markets"]["za"]["x"][:-1]
    moved = [c for c in job.panel_calls("ZA", TUESDAY, changed) if c.route == "twitter/user/tweets"]
    assert {c.protocol for c in moved} != za_x


def _row14_protocols(market, day, search_top):
    ranked = {"hashtag": [f"<{market} hashtag {i}>" for i in range(1, 81)],
              "sound": [f"<{market} sound {i}>" for i in range(1, job.SONG_VIDEOS + 1)]}
    calls = job.expansion_calls(market, day, ranked, random.Random(f"{day.isoformat()}-{market}"), set(),
                                search_top, queue=job.seeding.placeholders(market, day))
    row14 = [c for c in calls if c.row == "14" and c.route == "tiktok/search/top" and c.series() == "search"]
    others = {(c.row, c.series(), c.series_protocol()) for c in calls if c not in row14}
    return {c.series_protocol() for c in row14}, others, len(row14)


def test_a_changed_search_plan_starts_a_new_search_protocol():
    # The row 14 country searches of a market share one protocol that names the planned call count, the way a
    # panel's protocol names its membership: their day's items grow with the plan (15, 25, then 50 a market).
    before, others_before, n_before = _row14_protocols("ZA", TUESDAY, 25)
    after, others_after, n_after = _row14_protocols("ZA", TUESDAY, 50)
    assert (n_before, n_after) == (25, 50)
    assert before == {"tiktok/search/top?country=ZA&planned=25&publish_time=this-week"}
    assert after == {"tiktok/search/top?country=ZA&planned=50&publish_time=this-week"}
    # Row 16, the placebo and the count rows keep their protocols whatever row 14 plans.
    assert others_before == others_after
    assert _row14_protocols("KE", TUESDAY, 50)[0] != after


def test_an_unchanged_search_plan_keeps_its_search_protocol():
    # The day's queries change every run; only the plan names the series, so it carries on day to day.
    tuesday, _, _ = _row14_protocols("ZA", TUESDAY, 50)
    wednesday, _, _ = _row14_protocols("ZA", TUESDAY + timedelta(days=1), 50)
    assert tuesday == wednesday and len(tuesday) == 1
    planned = job.plan(TUESDAY)
    for market in job.MARKETS:
        protocols = {c.series_protocol() for c in planned["calls"][market]
                     if c.row == "14" and c.route == "tiktok/search/top" and c.series() == "search"}
        assert protocols == {f"tiktok/search/top?country={market}&max_pages=2&planned={job.search_calls(market)}"
                             "&publish_time=this-week"}


def test_a_new_search_plan_is_judged_on_calls_until_it_has_its_own_reference(monkeypatch):
    old = collect(FakeClient())
    old_rows = {r["market"]: r for r in writers.health_rows(old.records, old.posts, {}, "c")
                if r["series"] == "search" and r["route"] == "tiktok/search/top"}
    monkeypatch.setattr(job, "SEARCH_TOP", job.SEARCH_TOP + 10)
    new = collect(FakeClient())
    # The old plan's reference: 3 items a day over five valid days, against this run's 7.
    refs = {"2026-09-29": {(m, "search", r["protocol"]): (3.0, 5) for m, r in old_rows.items()}}
    new_rows = {r["market"]: r for r in writers.health_rows(new.records, new.posts, refs, "c")
                if r["series"] == "search" and r["route"] == "tiktok/search/top"}
    assert set(new_rows) == set(old_rows) == set(job.MARKETS)
    for market, row in new_rows.items():
        assert row["protocol"] != old_rows[market]["protocol"]
        assert (row["ref_items"], row["ref_days"], row["valid"], row["invalid_reason"]) == (None, 0, True, None)
    # Under an unchanged plan the old reference still applies and the items check still holds.
    same = {r["market"]: r for r in writers.health_rows(old.records, old.posts, refs, "c")
            if r["series"] == "search" and r["route"] == "tiktok/search/top"}
    assert all((r["ref_days"], r["valid"], r["invalid_reason"]) == (5, False, "items") for r in same.values())


# Candidates, exploration and the 25% cap

def test_candidates_are_scored_by_frequency_across_rows_and_platforms():
    run = collect(FakeClient())
    za = run.candidates["ZA"]
    assert za["hashtag"][0] == "amapiano"  # tiktok feed, the ZA board and a YouTube title
    assert "fyp" not in za["hashtag"]
    assert za["sound"][0] == "m100"
    assert "afrobeats" in run.candidates["NG"]["hashtag"] and "gengetone" in run.candidates["KE"]["hashtag"]


def test_exploration_takes_10_to_15_percent_of_row_14_from_below_the_cut():
    ranked = {"hashtag": [f"tag{i:02d}" for i in range(60)], "sound": [f"s{i}" for i in range(8)]}
    calls = job.expansion_calls("ZA", TUESDAY, ranked, random.Random("2026-09-29-ZA"), set())
    row14 = [c for c in calls if c.row == "14"]
    explore = [c for c in row14 if c.lane == "exploration"]
    assert len(row14) == job.SEARCH_TOP == 60
    assert 0.10 <= len(explore) / len(row14) <= 0.15
    cut = len(row14) - len(explore)
    assert {c.seed_key for c in row14 if c.lane == "expansion"} == set(ranked["hashtag"][:cut])
    assert all(c.seed_key in ranked["hashtag"][cut:] for c in explore)
    again = job.expansion_calls("ZA", TUESDAY, ranked, random.Random("2026-09-29-ZA"), set())
    assert [c.seed_key for c in again if c.lane == "exploration"] == [c.seed_key for c in explore]
    assert all(c.params["seen"] == "f42-za-2026-09-29" and c.params["country"] == "ZA" for c in row14)
    multi = [c for c in calls if c.row == "16"]
    assert [c.seed_key for c in multi] == ranked["hashtag"][:7]
    assert all(c.params["since"] == "2026-09-22" for c in multi)
    assert [c.params["clipId"] for c in calls if c.row == "11"] == ranked["sound"][:6]
    assert len([c for c in calls if c.row == "12"]) == 10


def test_the_live_hashtag_board_envelope_yields_board_tag_sightings():
    live = json.loads((Path(__file__).parent / "fixtures" / "parse_live.json").read_text(encoding="utf-8"))
    _, items, _ = split_vendor_labels(live["hashtags_popular"])
    call = SimpleNamespace(route="tiktok/hashtags/popular", row="2")
    done = [(call, SimpleNamespace(status="ok", items=items), None)]
    seen = job.sightings(done)
    assert ("hashtag", "shebeenfriday", "2", "tiktok") in seen
    assert ("hashtag", "kotarun", "2", "tiktok") in seen
    assert job.score_candidates(seen)["origin"]["shebeenfriday"] == ("tiktok", "2")


def test_a_bare_hashtag_board_row_still_yields_its_sighting():
    call = SimpleNamespace(route="tiktok/hashtags/popular", row="2")
    done = [(call, SimpleNamespace(status="ok", items=[{"hashtag_name": "amapiano"}]), None)]
    assert job.sightings(done) == [("hashtag", "amapiano", "2", "tiktok")]


def test_a_rule_one_harvest_tag_is_never_a_candidate_so_no_row_calls_it():
    age = ["".join(["tee", "ns"]), "".join(["ge", "nzhumor"]), "".join(["Mil", "lennialMoms"])]
    board = SimpleNamespace(route="tiktok/hashtags/popular", row="2")
    feed = SimpleNamespace(route="tiktok/trending", row="1")
    board_result = SimpleNamespace(status="ok", items=[{"hashtag_name": t} for t in age + ["amapiano"]])
    posts = {"posts": [{"hashtags": ["#" + t for t in age] + [f"tag{i:02d}" for i in range(30)],
                        "platform": "tiktok", "sound_id": None}]}
    done = [(board, board_result, None), (feed, SimpleNamespace(status="ok", items=[]), posts)]
    seen = job.sightings(done)
    assert not {key for _, key, _, _ in seen} & {t.casefold() for t in age}
    ranked = job.score_candidates(seen)
    calls = job.expansion_calls("ZA", TUESDAY, ranked, random.Random(1), set())
    asked = {str(v).casefold() for c in calls for v in c.params.values()}
    assert calls and not asked & {t.casefold() for t in age}


MIXED_SCRIPT = ["g\u0435nz", "\u0430mapiano", "amapi\u03b1no", "braai\u5357\u975e"]  # Cyrillic, Greek, Han in Latin
ONE_SCRIPT = ["\u5357\u975e", "\u6771\u4eac\u30bf\u30ef\u30fc", "\u043c\u043e\u0441\u043a\u0432\u0430",
              "\u0928\u092e\u0938\u094d\u0924\u0947", "\u0645\u0631\u062d\u0628\u0627", "\u12a0\u121b\u122d\u129b",
              "\ud55c\uad6d", "\u03b5\u03bb\u03bb\u03ac\u03b4\u03b1", "\uff41\uff4d\uff41\uff50\uff49\uff41\uff4e\uff4f"]


@pytest.mark.parametrize("raw", UNICODE_AGE_LENS + COHORT_FORMS + ROUND_FORMS + MIXED_SCRIPT + NAME_NEIGHBOURS)
def test_a_disguised_or_mixed_script_tag_is_never_a_candidate(raw):
    assert job._tag(raw) is None
    assert job._tag("#" + raw) is None
    board = SimpleNamespace(route="tiktok/hashtags/popular", row="2")
    done = [(board, SimpleNamespace(status="ok", items=[{"hashtag_name": raw}, {"hashtag_name": "amapiano"}]),
             {"posts": [{"hashtags": ["#" + raw, "#braai"], "platform": "tiktok", "sound_id": None}]})]
    assert {key for _, key, _, _ in job.sightings(done)} == {"amapiano", "braai"}


@pytest.mark.parametrize("raw", ORDINARY_SWEEP + ONE_SCRIPT + NAME_TAGS)
def test_ordinary_and_single_script_tags_stay_candidates(raw):
    assert job._tag("#" + raw) == raw.casefold()


def test_no_candidate_takes_more_than_a_quarter_of_expansion_calls():
    for n in range(1, 10):
        ranked = {"hashtag": [f"t{i}" for i in range(n)], "sound": []}
        calls = [c for c in job.expansion_calls("NG", TUESDAY, ranked, random.Random(1), set())
                 if c.row in ("14", "16")]
        for key in {c.seed_key for c in calls}:
            assert sum(c.seed_key == key for c in calls) <= 0.25 * len(calls), (n, key)
    heavy = [job.Call("14", "tiktok/search/top", {"query": "a"}, "ZA", "expansion", seed_key="a")] * 5
    light = [job.Call("14", "tiktok/search/top", {"query": k}, "ZA", "expansion", seed_key=k) for k in "bcdefg"]
    capped = job.cap_share(heavy + light)
    assert sum(c.seed_key == "a" for c in capped) <= 0.25 * len(capped)
    assert sum(c.seed_key != "a" for c in capped) == 6


def test_watched_counts_are_read_once_per_run_across_markets():
    run = collect(FakeClient())
    counts = [(c["route"], json.dumps(c["params"], sort_keys=True)) for c in run.client_calls
              if c["route"] in ("tiktok/song", "tiktok/hashtag")]
    assert counts and len(counts) == len(set(counts))


# Execution

def test_every_response_is_parsed_and_rows_land_for_three_markets():
    client = FakeClient()
    run = collect(client)
    markets = {o["market"] for o in run.observations}
    assert {"ZA", "NG", "KE", "GLOBAL"} <= markets
    platforms = {p["platform"] for p in run.posts}
    assert len(platforms - {"apple_music"}) >= 5
    assert all(c["lane"] for c in client.calls if c["route"] != "web/scrape")
    assert any(o["lane"] == "exploration" for o in run.observations) or not any(
        c["lane"] == "exploration" for c in client.calls)
    assert not [p for p in run.posts if p["post_id"] and p["native_id"] == "fb_old"]  # before since=


def test_post_stats_is_one_call_per_market_by_first_sighting_market():
    client = FakeClient()
    run = collect(client)
    stats = [c for c in client.calls if c["route"] == "prism/post-stats"]
    assert sorted(c["market"] for c in stats) == ["KE", "NG", "ZA"]
    first = {}
    for obs in run.observations:
        if obs["market"] in ("ZA", "NG", "KE") and obs["route"] != "prism/post-stats":
            first.setdefault(obs["post_id"], obs["market"])
    urls = {p["post_id"]: p["url"] for p in run.posts}
    by_url = {u: pid for pid, u in urls.items()}
    total = 0
    for call in stats:
        assert 0 < len(call["params"]["urls"]) <= 20
        total += len(call["params"]["urls"])
        assert all(first[by_url[u]] == call["market"] for u in call["params"]["urls"])
    assert total <= 60
    assert len({u for c in stats for u in c["params"]["urls"]}) == total


def test_cap_reached_stops_that_market_only():
    def script(n, route, market):
        return "cap_reached" if market == "ZA" and route == "reddit/subreddit" else None

    client = FakeClient(script)
    run = collect(client)
    za = [c for c in client.calls if c["market"] == "ZA"]
    assert za[-1]["route"] == "reddit/subreddit"
    assert sum(c["route"] == "reddit/subreddit" for c in za) == 1
    assert any(c["market"] == "NG" and c["route"] == "prism/profiles" for c in client.calls)
    assert any(c["market"] == "KE" and c["route"] == "prism/post-stats" for c in client.calls)
    assert run.stopped is None and run.stopped_markets == {"ZA"}
    not_made = [r for r in run.records if r["market"] == "ZA" and r["status"] == "not_made"]
    assert not_made and all(not r["ok"] for r in not_made)


@pytest.mark.parametrize("status", ["insufficient_credits", "balance_floor"])
def test_insufficient_credits_or_balance_floor_stops_everything(status):
    def script(n, route, market):
        return status if n == 5 else None

    client = FakeClient(script)
    run = collect(client)
    assert len(client.calls) == 5
    assert run.stopped == status
    assert any(r["status"] == "not_made" for r in run.records)


def test_item_id_value_errors_are_skipped_and_counted():
    def picky(kind, raw, platform=None):
        if kind == "creator":
            raise ValueError("no creators today")
        return fake_item_id(kind, raw, platform)

    run = collect(FakeClient(), item_id_fn=picky)
    assert run.item_id_skips > 0
    assert not [c for c in run.counters if c["item_id"].startswith("creator|")]
    assert run.counts()["item_id_skips"] == run.item_id_skips


def test_geo_gets_only_text_or_none_and_a_type_error_reads_as_unknown():
    seen = []

    def strict(platform, market, *, ext_region=None, home_market=None, profile_location=None, text=None,
               language=None):
        for value in (ext_region, home_market, profile_location, text):
            assert value is None or isinstance(value, str)
        assert language is None or isinstance(language, str) or all(isinstance(x, str) for x in language)
        seen.append(market)
        if text and "Kasi" in text:
            raise TypeError("simulated L2 type check")
        return FakeGeo()(platform, market, ext_region=ext_region)

    wrapped = job.safe_geo(strict)
    assert wrapped("tiktok", "ZA", ext_region="ZA", home_market={"a": 1}, profile_location=7, text=None,
                   language=["en", 3]) == ("ZA", 0.9, "ext_region")
    assert wrapped("tiktok", "ZA", ext_region="ZA", text="Kasi steps") == (None, None, None)
    run = job.collect(FakeClient(), TUESDAY, "collect-test", item_id_fn=fake_item_id, geo_fn=strict,
                      clock=lambda: NOW)
    kasi = [p for p in run.posts if p["text"] and "Kasi" in p["text"]]
    assert kasi and all(p["geo_market"] is None for p in kasi)
    assert set(seen) <= {"ZA", "NG", "KE"}


def test_row_5_reads_every_hub_and_one_rotating_hub_under_a_cap_override():
    config = copy.deepcopy(job.load_config())
    config["markets"]["za"]["instagram_locations"] = {"values": ["111", "222"]}
    calls, skipped = job.harvest_calls("ZA", TUESDAY, config)
    ig = [c for c in calls if c.route == "instagram/location/posts"]
    assert [c.params["location_id"] for c in ig] == ["111", "222"]
    assert not [s for s in skipped if s["route"] == "instagram/location/posts"]
    calls, _ = job.harvest_calls("ZA", TUESDAY, config, override=True)
    ig = [c for c in calls if c.route == "instagram/location/posts"]
    assert len(ig) == 1 and ig[0].params["location_id"] in ("111", "222")


@pytest.mark.parametrize("market,subs", [
    ("ZA", ["southafrica", "Amapiano", "CapeTown", "johannesburg", "PersonalFinanceZA", "Springboks"]),
    ("NG", ["Nigeria", "Lagos", "NigerianFood"]),
    ("KE", ["Kenya", "Nairobi", "Kenyans"]),
])
def test_row_7_reads_each_markets_own_subreddits_hot_and_rising(market, subs):
    calls, _ = job.harvest_calls(market, TUESDAY, job.load_config())
    reddit = [(c.params["subreddit"], c.params["sort"]) for c in calls if c.route == "reddit/subreddit"]
    assert reddit == [(sub, sort) for sub in subs for sort in ("hot", "rising")]


def test_row_7_never_reads_a_pan_african_subreddit_as_a_market_list():
    config = job.load_config()
    for market in job.MARKETS:
        calls, _ = job.harvest_calls(market, TUESDAY, config)
        assert not {c.params["subreddit"] for c in calls if c.route == "reddit/subreddit"} & {
            "Africa", "AfricanMusic", "Afrobeats"}


def test_the_plan_with_row_7_stays_under_the_collect_cap():
    planned = job.plan(TUESDAY)
    local = ls.total_hold(ls.plan(TUESDAY))
    assert planned["total"] + local <= job.load_caps()["ENGINE_DAILY"]["collect"]


def test_the_real_client_accepts_every_call_and_the_ledger_equals_vendor_charges(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "test-key")
    ledger, raw = MemoryLedgerStore(), MemoryRawStore()
    charged = []

    def http(method, url, *, params=None, json=None, headers=None, timeout=60):
        route = url.split("/v1/", 1)[1]
        if route == "credits/balance":
            return 200, {"data": {"balance": 100000}}, {}
        sent = params if method == "GET" else json
        cost = quote_for(route, method, sent)
        charged.append(cost)
        body = body_for(route, sent)
        if route == "prism/profiles":  # the fixture's credits_used is for a 12-profile batch
            body["credits_used"] = cost
        return 200, body, {"x-credit-cost": str(cost)}

    client = SocialCrawlClient(share="collect", run_id="collect-test", mode="live", ledger=ledger, raw=raw,
                               http=http, clock=lambda: NOW)
    run = collect(client)
    statuses = {r["status"] for r in run.records if r["status"] != "skipped"}
    assert statuses <= {"ok", "empty", "cached"}, statuses
    assert ledger.spent(TUESDAY, TUESDAY, job="collect") == sum(charged) == run.credits
    assert {o["market"] for o in run.observations} >= {"ZA", "NG", "KE"}
    assert len(raw.rows) == len(charged)


def test_a_lowered_cap_stops_at_150_through_the_real_client(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "test-key")
    ledger = MemoryLedgerStore()

    def http(method, url, *, params=None, json=None, headers=None, timeout=60):
        route = url.split("/v1/", 1)[1]
        if route == "credits/balance":
            return 200, {"data": {"balance": 100000}}, {}
        sent = params if method == "GET" else json
        return 200, body_for(route, sent), {"x-credit-cost": str(quote_for(route, method, sent))}

    caps = job.collect_caps({"COLLECT_CAP_OVERRIDE": "150"})
    client = SocialCrawlClient(share="collect", run_id="collect-test", mode="live", ledger=ledger,
                               raw=MemoryRawStore(), http=http, clock=lambda: NOW, caps=caps)
    run = collect(client)
    assert 0 < ledger.spent(TUESDAY, TUESDAY, job="collect") <= 150
    assert any(r["status"] == "cap_reached" for r in run.records)


# Writers

def test_merge_sql_has_no_delete_and_batches_rows():
    run = collect(FakeClient())
    posts = writers.dedupe_posts(run.posts)
    bq = FakeBQ()
    statements = writers.merge_posts(bq, posts, batch=4)
    merges = [(sql, p) for sql, p in bq.queries if sql.lstrip().upper().startswith("MERGE")]
    assert statements == len(merges) == -(-len(posts) // 4) > 1
    for sql, params in merges:
        assert "DELETE" not in sql.upper()
        assert re.search(r"ON\s+T\.post_id\s*=\s*S\.post_id", sql)
        assert "UNNEST(@rows)" in sql and "WHEN NOT MATCHED THEN INSERT" in sql
        assert 0 < len(params["rows"].values) <= 4
        assert params["rows"].array_type == "STRUCT"
    for word in ("CREATE", "DROP", "TRUNCATE", "REPLACE", "EXPIRATION"):
        assert word not in "\n".join(sql.upper() for sql, _ in bq.queries)
    assert sorted(bq.posts) == sorted(p["post_id"] for p in posts)


def test_dedupe_keeps_first_sighting_fields_and_the_latest_metrics():
    first = {c: None for c in writers.POST_TYPES}
    first.update(post_id="p1", platform="tiktok", text="first", post_date="2026-09-28", views=10, likes=1,
                 comments=0, shares=0, engagement=1, hashtags=["a"])
    later = dict(first, text="later", views=50, likes=5, comments=1, shares=1, engagement=7)
    empty = dict(first, text="empty", views=None, likes=None, comments=None, shares=None, engagement=None)
    [row] = writers.dedupe_posts([first, later, empty])
    assert row["text"] == "first" and row["views"] == 50 and row["engagement"] == 7


def test_a_second_run_over_the_same_fixture_adds_no_posts():
    bq = FakeBQ()
    first = collect(FakeClient())
    writers.write_run(bq, first, "collect-1")
    posts_after_one = dict(bq.posts)
    observations_one = len(bq.loaded("post_observations"))
    second = collect(FakeClient())
    writers.write_run(bq, second, "collect-2")
    assert sorted(bq.posts) == sorted(posts_after_one)
    assert len(bq.loaded("post_observations")) == 2 * observations_one > 0


def test_appends_use_load_jobs_in_write_append():
    bq = FakeBQ()
    writers.write_run(bq, collect(FakeClient()), "collect-1")
    tables = {table_id.rsplit(".", 1)[1] for table_id, _, _ in bq.loads}
    assert tables == {"post_observations", "item_counter_daily", "collection_health"}
    assert all(cfg.write_disposition == "WRITE_APPEND" for _, _, cfg in bq.loads)
    assert all(table_id.startswith("ogilvy-trends-v2.intelligence_42_core.") for table_id, _, _ in bq.loads)


def rank_row(item, pull, market="ZA", series="feed_tiktok", day="2026-09-29"):
    return {"obs_date": day, "market": market, "platform": "tiktok", "item_id": item, "series": series,
            "route": "tiktok/trending", "protocol": "tiktok/trending?feed=local&region=ZA", "is_board": False,
            "lane_class": "unbiased_rank", "unit": "rank", "pull_seq": pull, "value": 3.0, "source": "live",
            "observed_at": f"2026-09-29T00:0{pull}:00+00:00", "available_at": f"2026-09-29T00:0{pull}:00+00:00",
            "run_id": "collect-1"}


def test_appearances_count_the_pulls_an_item_was_on():
    rows = [rank_row("hashtag|a", 1), rank_row("hashtag|a", 2), rank_row("hashtag|a", 3), rank_row("hashtag|b", 2),
            rank_row("hashtag|a", 1, market="NG")]
    out = writers.appearances(rows, "collect-1")
    got = {(r["market"], r["item_id"]): r for r in out}
    assert got[("ZA", "hashtag|a")]["value"] == 3 and got[("ZA", "hashtag|b")]["value"] == 1
    assert got[("NG", "hashtag|a")]["value"] == 1
    assert all(r["unit"] == "appearances" and r["pull_seq"] is None and r["lane_class"] == "unbiased_rank"
               for r in out)
    assert got[("ZA", "hashtag|a")]["observed_at"] == "2026-09-29T00:03:00+00:00"


def total_row(item, value, day="2026-09-29", at="2026-09-29T00:10:00+00:00"):
    return {"obs_date": day, "market": "GLOBAL", "platform": "tiktok", "item_id": item,
            "series": "counter_tiktok_hashtag", "route": "tiktok/hashtag", "protocol": "tiktok/hashtag",
            "is_board": False, "lane_class": "unbiased_counter", "unit": "total", "pull_seq": None,
            "value": float(value), "source": "live", "observed_at": at, "available_at": at, "run_id": "collect-1"}


def test_delta_is_today_minus_the_previous_days_latest_total_and_absent_without_one():
    today = [total_row("hashtag|a", 1500), total_row("hashtag|b", 70)]
    bq = FakeBQ(previous=[{"market": "GLOBAL", "item_id": "hashtag|a", "series": "counter_tiktok_hashtag",
                           "protocol": "tiktok/hashtag", "value": 1200.0}])
    out = writers.deltas(bq, today, "collect-1")
    assert [(r["item_id"], r["value"], r["unit"], r["obs_date"]) for r in out] == [
        ("hashtag|a", 300.0, "delta", "2026-09-29")]
    sql, params = bq.queries[-1]
    assert "@prev" in sql and params["prev"].value == date(2026, 9, 28)
    assert "unit = 'total'" in sql and "available_at DESC" in sql
    assert sorted(params["items"].values) == ["hashtag|a", "hashtag|b"]
    assert writers.deltas(FakeBQ(), today, "collect-1") == []


def test_delta_uses_the_latest_total_of_this_run():
    today = [total_row("hashtag|a", 1500, at="2026-09-29T00:10:00+00:00"),
             total_row("hashtag|a", 1600, at="2026-09-29T00:20:00+00:00")]
    bq = FakeBQ(previous=[{"market": "GLOBAL", "item_id": "hashtag|a", "series": "counter_tiktok_hashtag",
                           "protocol": "tiktok/hashtag", "value": 1000.0}])
    [row] = writers.deltas(bq, today, "collect-1")
    assert row["value"] == 600.0


# collection_health

def test_validity_rules_follow_data_md_3_3():
    judge = writers.judge
    assert judge(calls=3, calls_ok=3, items=60, ref_items=None, ref_days=0, lane_class="unbiased_rank",
                 units_planned=3, units_ok=3) == (True, None, None)
    assert judge(calls=5, calls_ok=3, items=60, ref_items=None, ref_days=0, lane_class="unbiased_rank",
                 units_planned=5, units_ok=3)[:2] == (False, "calls")
    assert judge(calls=3, calls_ok=3, items=10, ref_items=60, ref_days=3, lane_class="unbiased_rank",
                 units_planned=3, units_ok=3)[:2] == (False, "items")
    assert judge(calls=3, calls_ok=3, items=10, ref_items=60, ref_days=2, lane_class="unbiased_rank",
                 units_planned=3, units_ok=3)[:2] == (True, None)  # fewer than 3 ref days: calls alone
    assert judge(calls=1, calls_ok=1, items=5, ref_items=None, ref_days=0, lane_class="panel",
                 units_planned=12, units_ok=4) == (False, "effort", 3.0)
    assert judge(calls=8, calls_ok=8, items=5, ref_items=None, ref_days=0, lane_class="panel",
                 units_planned=8, units_ok=8) == (True, None, 1.0)
    assert judge(calls=0, calls_ok=0, items=0, ref_items=None, ref_days=0, lane_class="unbiased_rank",
                 units_planned=0, units_ok=0)[:2] == (False, "calls")


def test_health_rows_one_per_market_series_and_protocol():
    run = collect(FakeClient())
    refs = {"2026-09-29": {("ZA", "feed_tiktok", "tiktok/trending?feed=local&region=ZA"): (100.0, 5)}}
    rows = writers.health_rows(run.records, run.posts, refs, "collect-1")
    keys = [(r["day"], r["market"], r["series"], r["protocol"]) for r in rows]
    assert len(keys) == len(set(keys))
    feed = {r["market"]: r for r in rows if r["series"] == "feed_tiktok"}
    assert set(feed) == {"ZA", "NG", "KE"}
    za = feed["ZA"]
    assert (za["calls"], za["calls_ok"], za["units_planned"], za["units_ok"], za["items"]) == (3, 3, 3, 3, 9)
    assert (za["ref_items"], za["ref_days"], za["valid"], za["invalid_reason"]) == (100.0, 5, False, "items")
    assert feed["NG"]["valid"] is True and feed["NG"]["ref_days"] == 0
    assert za["located_share"] == 1.0 and feed["NG"]["located_share"] == 1.0  # GH is a known location
    assert feed["KE"]["located_share"] == pytest.approx(2 / 3)
    assert all(r["located_share"] == 0.0 for r in rows if r["series"] == "list_reddit")
    planned = job.plan(TUESDAY)
    desk_protocols = {c.series_protocol() for cs in planned["calls"].values() for c in cs
                      if _desk(c)}
    curated_protocols = {c.series_protocol() for cs in planned["calls"].values() for c in cs
                         if c.route == "prism/profiles" and not _desk(c)}
    desk = [r for r in rows if r["series"] == "panel_culture_desk" and r["protocol"] in desk_protocols]
    curated = [r for r in rows if r["series"] == "panel_culture_desk" and r["protocol"] in curated_protocols]
    assert all(r["k"] == 12.0 and r["invalid_reason"] == "effort" for r in desk)
    # The fixture answers each batch with one profile row: the day's profiles planned over one ok a batch.
    assert len(curated) == len(job.MARKETS)
    for r in curated:
        batches = [c for c in planned["calls"][r["market"]]
                   if c.route == "prism/profiles" and not _desk(c)]
        assert r["k"] == sum(len(c.params["items"]) for c in batches) / len(batches)
    counters = [r for r in rows if r["series"].startswith("counter_tiktok")]
    assert counters and all(r["market"] == "GLOBAL" for r in counters)
    assert all(r["day"] == "2026-09-29" and r["run_id"] == "collect-1" for r in rows)
    assert set(rows[0]) == set(writers.HEALTH_COLUMNS)


def test_reference_reads_prior_valid_days_with_a_parameterised_select():
    bq = FakeBQ(reference=[{"market": "ZA", "series": "feed_tiktok", "protocol": "p", "ref_items": 60, "ref_days": 4}])
    assert writers.reference(bq, TUESDAY) == {("ZA", "feed_tiktok", "p"): (60.0, 4)}
    sql, params = bq.queries[-1]
    assert params["d"].value == TUESDAY and "@d" in sql and "INTERVAL 28 DAY" in sql
    assert "h.valid" in sql and "status = 'ok'" in sql


# main and the chain

def test_collect_cap_override_can_lower_but_not_raise():
    assert job.collect_caps({})["ENGINE_DAILY"]["collect"] == 2000
    assert job.collect_caps({"COLLECT_CAP_OVERRIDE": "150"})["ENGINE_DAILY"]["collect"] == 150
    share = job.load_caps()["ENGINE_DAILY"]["collect"]
    for bad in (str(share + 100), str(share + 1), "-1", "lots"):
        with pytest.raises(ValueError):
            job.collect_caps({"COLLECT_CAP_OVERRIDE": bad})


def test_main_passes_the_lowered_cap_and_refuses_a_higher_one():
    seen, runs = [], chain.MemoryRunsStore()
    assert run_main(["--run-date", "2026-09-29"], env={"COLLECT_CAP_OVERRIDE": "150"}, caps_seen=seen, runs=runs) == 0
    assert seen[0]["ENGINE_DAILY"]["collect"] == 150
    refused = chain.MemoryRunsStore()
    assert run_main(["--run-date", "2026-09-29"], env={"COLLECT_CAP_OVERRIDE": "5000"}, runs=refused) != 0
    assert refused.rows == []


def test_main_finishes_ok_then_starts_the_next_job():
    runs, jobs, bq = chain.MemoryRunsStore(), FakeJobs(), FakeBQ()
    assert run_main(["--run-date", "2026-09-29"], runs=runs, jobs=jobs, bq=bq) == 0
    assert [r["status"] for r in runs.rows] == ["running", "ok"]
    final = runs.rows[-1]
    assert final["run_date"] == "2026-09-29" and final["counts"]["posts"] > 0 and final["counts"]["calls"] > 0
    assert jobs.started == [(chain.JOBS["detect"], {"RUN_DATE": "2026-09-29"})]
    assert bq.posts and bq.loaded("collection_health")


def test_a_live_run_refuses_a_run_date_other_than_today_in_sast(monkeypatch):
    monkeypatch.setenv("RUN_DATE", "2026-09-30")
    runs, client = chain.MemoryRunsStore(), FakeClient()
    assert run_main([], runs=runs, client=client) != 0
    assert run_main(["--run-date", "2026-09-28"], runs=runs, client=client) != 0
    assert runs.rows == [] and client.calls == []
    monkeypatch.setenv("RUN_DATE", "2026-09-29")
    assert run_main([], runs=runs) == 0
    assert runs.rows[-1]["run_date"] == "2026-09-29"


def test_main_marks_the_run_failed_and_exits_non_zero_on_an_exception():
    class Broken(FakeBQ):
        def load_table_from_json(self, rows, table_id, job_config=None):
            raise RuntimeError("load failed")

    runs, jobs = chain.MemoryRunsStore(), FakeJobs()
    assert run_main(["--run-date", "2026-09-29"], runs=runs, jobs=jobs, bq=Broken()) != 0
    assert [r["status"] for r in runs.rows] == ["running", "failed"]
    assert "load failed" in runs.rows[-1]["error"]
    assert jobs.started == []


def test_a_run_whose_balance_could_not_be_read_finishes_failed_and_starts_nothing_next():
    class Unreadable(FakeClient):
        def call(self, route, params=None, **kw):
            super().call(route, params, **kw)
            return Result("balance_floor", route, reason="balance could not be read", failure="balance_unread")

    runs, jobs, client = chain.MemoryRunsStore(), FakeJobs(), Unreadable()
    assert run_main(["--run-date", "2026-09-29"], runs=runs, jobs=jobs, client=client) != 0
    assert [r["status"] for r in runs.rows] == ["running", "failed"]
    final = runs.rows[-1]
    assert "balance" in final["error"] and "could not be read" in final["error"]
    assert final["counts"]["stopped"] == "balance_floor" and final["counts"]["credits_charged"] == 0
    assert len(client.calls) == 1
    assert jobs.started == []


def test_a_run_stopped_below_the_floor_is_not_reported_as_an_unread_balance():
    class Low(FakeClient):
        def call(self, route, params=None, **kw):
            super().call(route, params, **kw)
            return Result("balance_floor", route, reason="balance 100 is below the floor 20000", failure="")

    runs = chain.MemoryRunsStore()
    assert run_main(["--run-date", "2026-09-29"], runs=runs, client=Low()) == 0
    assert runs.rows[-1]["status"] == "ok"


def test_the_runs_row_never_counts_more_ok_calls_than_calls():
    runs = chain.MemoryRunsStore()
    assert run_main(["--run-date", "2026-09-29"], runs=runs) == 0
    counts = runs.rows[-1]["counts"]
    assert counts["local_records"] > 0
    assert counts["calls_ok"] <= counts["calls"]


def test_a_collect_that_raises_midway_still_records_the_calls_and_credits_it_made():
    def script(n, route, market):
        if n == 6:
            raise RuntimeError("vendor client crashed")

    runs, jobs, client = chain.MemoryRunsStore(), FakeJobs(), FakeClient(script)
    assert run_main(["--run-date", "2026-09-29"], runs=runs, jobs=jobs, client=client) != 0
    final = runs.rows[-1]
    assert final["status"] == "failed" and "vendor client crashed" in final["error"]
    assert final["counts"]["calls"] == 5 and final["counts"]["credits_charged"] > 0
    assert jobs.started == []


def test_already_done_exits_zero_without_calls():
    runs = chain.MemoryRunsStore()
    assert run_main(["--run-date", "2026-09-29"], runs=runs) == 0
    client = FakeClient()
    assert run_main(["--run-date", "2026-09-29"], runs=runs, client=client) == 0
    assert client.calls == []
    assert runs.rows[-1]["status"] == chain.SKIPPED


def test_plan_flag_prints_every_call_and_the_total_without_network(capsys):
    def boom(*a, **k):
        raise AssertionError("no client in plan mode")

    code = job.main(["--plan", "--run-date", "2026-09-29"], env={}, make_client=boom, bq=object(),
                    runs=object(), jobs=object())
    out = capsys.readouterr().out
    assert code == 0
    for market in ("GLOBAL", "ZA", "NG", "KE"):
        assert re.search(rf"^{market}\b", out, re.M)
    hubs = sum(len(job.load_config()["markets"][m.lower()]["instagram_locations"]["values"]) for m in job.MARKETS)
    assert out.count("instagram/location/posts") == hubs
    assert "no Instagram location ids" not in out
    total = sum(c.hold() for c in all_planned())
    assert re.search(rf"total hold {total} credits over {len(all_planned())} calls", out)


def vendor_http(method, url, *, params=None, json=None, headers=None, timeout=60):
    route = url.split("/v1/", 1)[1]
    if route == "credits/balance":
        return 200, {"data": {"balance": 100000}}, {}
    sent = params if method == "GET" else json
    return 200, body_for(route, sent), {"x-credit-cost": str(quote_for(route, method, sent))}


# Review fixes: market-local days, the 150 override, running pull numbers, restarts

def test_a_run_crossing_23_00_sast_stops_ke_calls_and_writes_no_next_day_rows():
    ticks = []

    def counting():
        ticks.append(1)
        return NOW

    job.collect(FakeClient(), TUESDAY, "c", item_id_fn=fake_item_id, geo_fn=FakeGeo(), clock=counting)
    start = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc) - timedelta(seconds=len(ticks) // 2)
    state = {"i": 0}

    def ticking():  # one second a reading, crossing 23:00 SAST (midnight EAT) half way through the run
        state["i"] += 1
        return start + timedelta(seconds=state["i"])

    client = FakeClient()
    run = job.collect(client, TUESDAY, "c", item_id_fn=fake_item_id, geo_fn=FakeGeo(), clock=ticking)
    rows = writers.health_rows(run.records, run.posts, {}, "c")
    assert {r["day"] for r in rows} == {"2026-09-29"}
    assert {o["observed_date"] for o in run.observations} == {"2026-09-29"}
    assert {c["obs_date"] for c in run.counters if c["source"] == "live"} == {"2026-09-29"}
    changed = [r for r in run.records if r["status"] == "day_changed"]
    assert changed and {r["market"] for r in changed} >= {"KE"}
    assert all(r["calls"] == 0 and r["units_planned"] == 0 and r["reason"] == "day_changed" for r in changed)
    ke_made = [i for i, c in enumerate(client.calls) if c["market"] == "KE"]
    za_made = [i for i, c in enumerate(client.calls) if c["market"] == "ZA"]
    assert ke_made and za_made and max(za_made) > max(ke_made)  # ZA and NG keep going on their own day
    for r in rows:
        if r["series"] in ("feed_tiktok", "list_reddit", "board_youtube"):
            obs = [o for o in run.observations if (o["market"], o["series"], o["protocol"], o["observed_date"])
                   == (r["market"], r["series"], r["protocol"], r["day"])]
            assert r["items"] == len(obs), r
    ke_feed = [r for r in rows if r["market"] == "KE" and r["series"] == "feed_tiktok"]
    assert ke_feed and all(r["calls"] == r["calls_ok"] for r in ke_feed)


@pytest.mark.parametrize("utc, run_date", [
    (datetime(2026, 9, 29, 21, 30, tzinfo=timezone.utc), "2026-09-29"),  # 23:30 SAST: already 30 Sep in KE
    (datetime(2026, 9, 29, 22, 30, tzinfo=timezone.utc), "2026-09-30"),  # 00:30 SAST: still 29 Sep in NG
])
def test_a_live_run_refuses_to_start_unless_every_market_is_on_the_run_date(utc, run_date):
    runs, client = chain.MemoryRunsStore(), FakeClient()
    assert run_main(["--run-date", run_date], runs=runs, client=client, clock=lambda: utc) != 0
    assert runs.rows == [] and client.calls == []


@pytest.mark.parametrize("utc, run_date, ok", [
    (datetime(2026, 9, 28, 23, 30, tzinfo=timezone.utc), "2026-09-29", True),   # 01:30 SAST
    (datetime(2026, 9, 29, 20, 30, tzinfo=timezone.utc), "2026-09-29", True),   # 22:30 SAST
    (datetime(2026, 9, 28, 23, 29, tzinfo=timezone.utc), "2026-09-29", False),  # 01:29 SAST
    (datetime(2026, 9, 29, 20, 31, tzinfo=timezone.utc), "2026-09-29", False),  # 22:31 SAST
])
def test_a_live_run_starts_only_between_01_30_and_22_30_sast(utc, run_date, ok):
    runs, client = chain.MemoryRunsStore(), FakeClient()
    code = run_main(["--run-date", run_date], runs=runs, client=client, clock=lambda: utc)
    if ok:
        assert code == 0 and runs.rows[-1]["status"] == "ok"
    else:
        assert code != 0 and runs.rows == [] and client.calls == []


def test_a_call_that_returns_after_23_00_sast_writes_no_next_day_rows():
    state = {"now": datetime(2026, 9, 29, 20, 59, 59, tzinfo=timezone.utc)}  # 22:59:59 SAST, 23:59:59 EAT

    class SlowClient(FakeClient):
        def call(self, route, params=None, **kw):
            result = super().call(route, params, **kw)
            if kw.get("market") == "KE":
                state["now"] = datetime(2026, 9, 29, 21, 0, 1, tzinfo=timezone.utc)  # 23:00:01 SAST
            return result

    client = SlowClient()
    run = job.collect(client, TUESDAY, "c", item_id_fn=fake_item_id, geo_fn=FakeGeo(), clock=lambda: state["now"])
    assert [c for c in client.calls if c["market"] == "KE"]  # the first KE call was made, then the day moved
    assert not [o for o in run.observations if o["market"] == "KE" or o["observed_date"] != "2026-09-29"]
    assert not [c for c in run.counters if c["source"] == "live" and c["obs_date"] != "2026-09-29"]
    ke = [r for r in run.records if r["market"] == "KE" and r["status"] != "skipped"]  # row 5 has no input
    assert ke and all(r["status"] == "day_changed" and r["calls"] == 0 and r["post_ids"] == [] for r in ke)
    rows = writers.health_rows(run.records, run.posts, {}, "c")
    assert {r["day"] for r in rows} == {"2026-09-29"}
    assert run.credits == sum(c["credits"] for c in client.charged)


def test_markets_run_interleaved_row_by_row():
    client = FakeClient()
    collect(client)
    first = client.calls[2:5]
    assert [c["market"] for c in first] == ["ZA", "NG", "KE"]
    assert all(c["route"] == "tiktok/trending" for c in first)


def test_plan_under_the_150_override_splits_the_cap_and_puts_cheap_harvest_first():
    planned = job.plan(MONDAY, share_cap=150)
    assert 0 < planned["total"] <= 150
    shares = job.shares(150)
    assert shares["ZA"] == shares["NG"] == shares["KE"] and shares["GLOBAL"] < shares["ZA"]
    spent = {}
    for calls in planned["calls"].values():
        for c in calls:
            spent[job.budget_key(c)] = spent.get(job.budget_key(c), 0) + c.hold()
    assert all(spent[k] <= shares[k] for k in spent)
    assert not [c for c in planned["calls"]["ZA"] if c.params.get("industry")]
    cheap = ("tiktok/trending", "youtube/videos/trending", "reddit/subreddit", "apple_music/charts")
    for market in ("ZA", "NG", "KE"):
        calls = planned["calls"][market]
        assert {c.route for c in calls} >= set(cheap)
        last_cheap = max(i for i, c in enumerate(calls) if c.route in cheap and (c.pull_seq or 1) == 1)
        first_expansion = min(i for i, c in enumerate(calls) if c.row in ("11", "12", "14", "16"))
        assert last_cheap < first_expansion
        assert planned["over"][market]
    assert len(all_planned(MONDAY)) > sum(len(c) for c in planned["calls"].values())


def test_a_150_override_lands_calls_in_every_market_on_five_platforms(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "test-key")
    ledger = MemoryLedgerStore()
    caps = job.collect_caps({"COLLECT_CAP_OVERRIDE": "150"})
    client = SocialCrawlClient(share="collect", run_id="collect-test", mode="live", ledger=ledger,
                               raw=MemoryRawStore(), http=vendor_http, clock=lambda: NOW, caps=caps)
    run = collect(client, share_cap=150)
    assert 0 < ledger.spent(TUESDAY, TUESDAY, job="collect") <= 150
    assert not [r for r in run.records if r["status"] == "cap_reached"]
    for market in ("ZA", "NG", "KE"):
        assert [r for r in run.records if r["market"] == market and r["ok"]], market
    shares = job.shares(150)
    assert all(run.spent[k] <= shares[k] for k in run.spent)
    platforms = {r["platform"] for r in run.observations + run.counters if r["market"] in ("ZA", "NG", "KE")}
    assert len(platforms) >= 5, platforms
    assert [r for r in run.records if r["status"] == "over_share"]
    health = writers.health_rows(run.records, run.posts, {}, "collect-test")
    feed = [r for r in health if r["series"] == "feed_tiktok"]
    assert {r["market"] for r in feed} == {"ZA", "NG", "KE"}
    assert all(r["valid"] and r["calls"] == r["calls_ok"] == r["units_ok"] >= 1 for r in feed), feed


def test_pull_numbers_continue_from_the_previous_max_per_series_and_protocol():
    last = {("ZA", "feed_tiktok", "tiktok/trending?feed=local&region=ZA"): 41}
    run = collect(FakeClient(), last_pulls=last)

    def pulls(market, series="feed_tiktok"):
        return {o["pull_seq"] for o in run.observations if o["market"] == market and o["series"] == series}

    assert pulls("ZA") == {42, 43, 44} and pulls("NG") == {1, 2, 3}
    assert {c["pull_seq"] for c in run.counters if c["market"] == "ZA" and c["series"] == "feed_tiktok"} <= {42, 43, 44}
    assert pulls("ZA", "list_reddit") == {1}


def test_last_pulls_reads_the_max_with_a_parameterised_select():
    bq = FakeBQ(pulls=[{"market": "ZA", "series": "feed_tiktok", "protocol": "p", "last_pull": 41}])
    assert writers.last_pulls(bq, TUESDAY) == {("ZA", "feed_tiktok", "p"): 41}
    sql, params = bq.queries[-1]
    assert "MAX(c.pull_seq)" in sql and "@series" in sql
    assert {"feed_tiktok", "board_youtube", "list_reddit"} <= set(params["series"].values)


def test_main_continues_pull_numbers_from_bigquery():
    bq = FakeBQ(pulls=[{"market": "ZA", "series": "feed_tiktok", "protocol": "tiktok/trending?feed=local&region=ZA",
                        "last_pull": 7}])
    assert run_main(["--run-date", "2026-09-29"], bq=bq) == 0
    za = {o["pull_seq"] for o in bq.loaded("post_observations") if o["market"] == "ZA" and o["series"] == "feed_tiktok"}
    assert za == {8, 9, 10}


def test_already_done_restarts_the_next_job_when_it_never_started():
    runs = chain.MemoryRunsStore()
    assert run_main(["--run-date", "2026-09-29"], runs=runs, jobs=FakeJobs(fail=True)) != 0
    assert [r["status"] for r in runs.rows] == ["running", "ok"]
    assert run_main(["--run-date", "2026-09-29"], runs=runs, jobs=FakeJobs(fail=True)) != 0  # Cloud Run retries
    jobs, client = FakeJobs(), FakeClient()
    assert run_main(["--run-date", "2026-09-29"], runs=runs, jobs=jobs, client=client) == 0
    assert jobs.started == [(chain.JOBS["detect"], {"RUN_DATE": "2026-09-29"})] and client.calls == []
    runs.append({"run_id": "detect-x", "stage": "detect", "run_date": "2026-09-29", "status": "running",
                 "started_at": NOW.isoformat(), "finished_at": None, "counts": None, "error": None})
    later = FakeJobs()
    assert run_main(["--run-date", "2026-09-29"], runs=runs, jobs=later) == 0
    assert later.started == []


class GetTrap:
    """An HTTP session that fails on any GET and records each POST."""

    def __init__(self):
        self.posts = []

    def get(self, url, **kw):
        raise AssertionError(f"no GET: {url}")

    def post(self, url, json=None, **kw):
        self.posts.append(url.rsplit("/", 1)[1])
        return type("R", (), {"status_code": 200, "raise_for_status": lambda s: None, "json": lambda s: {}})()


@pytest.mark.parametrize("value,stage", [(None, "detect"), ("0", "detect"), ("1", "understand")])
def test_restart_next_follows_chain_understand_with_no_get(monkeypatch, value, stage):
    if value is not None:
        monkeypatch.setenv("CHAIN_UNDERSTAND", value)
    day = date(2026, 9, 29)
    runs = chain.MemoryRunsStore()
    runs.append({"run_id": "collect-x", "stage": "collect", "run_date": day.isoformat(), "status": "ok",
                 "started_at": NOW.isoformat(), "finished_at": NOW.isoformat(), "counts": None, "error": None})
    session = GetTrap()
    assert job.next_stage() == stage
    assert job.restart_next(day, runs, chain.CloudRunJobs(session=session)) == 0
    assert session.posts == [chain.JOBS[stage] + ":run"]
    runs.append({"run_id": f"{stage}-x", "stage": stage, "run_date": day.isoformat(), "status": "running",
                 "started_at": NOW.isoformat(), "finished_at": None, "counts": None, "error": None})
    assert job.restart_next(day, runs, chain.CloudRunJobs(session=session)) == 0
    assert session.posts == [chain.JOBS[stage] + ":run"]


def test_nothing_in_the_chain_probes_a_job():
    assert not hasattr(chain.CloudRunJobs, "deployed") and not hasattr(chain, "OPTIONAL")


def test_plan_runs_as_a_module_from_the_command_line():
    done = subprocess.run([sys.executable, "-m", "core.collect.job", "--plan", "--run-date", "2026-09-29"],
                          cwd=ROOT, capture_output=True, timeout=120)
    out = done.stdout.decode("utf-8")
    assert done.returncode == 0, done.stderr.decode("utf-8")
    assert "tiktok/trending" in out and "total hold" in out


# The seed loop: seed_queue in, one yield row per seed used out

def queue_seed(query, day=TUESDAY - timedelta(days=1), lane="expansion", template="search/multi", market="ZA",
               ttl=3, kind="topic", item_id=None):
    return {"seed_date": day, "market": market, "item_id": item_id or f"{kind}|{query.casefold()}", "query": query,
            "kind": kind, "lane": lane, "priority": 3.0, "template": template, "ttl_days": ttl,
            "credits_estimate": 10.0, "yield_posts": None, "yield_new_creators": None}


@pytest.mark.usefixtures("collect_share_2000")
def test_plan_runs_seed_driven_expansion_inside_the_same_slots():
    planned = job.plan(TUESDAY)
    for market in ("ZA", "NG", "KE"):
        calls = [c for c in planned["calls"][market] if c.row in ("14", "16")]
        seeded = [c for c in calls if c.seed.family in ("seed_queue", "anchor")]
        multi = [c for c in seeded if c.row == "16"]
        assert multi and all(c.params["query"].startswith(f"<{market} expansion seed") for c in multi)
        assert all(c.params["since"] == "2026-09-28" for c in multi)
        assert {c.seed.lane for c in seeded} >= {"expansion", "anchor"}
        assert len([c for c in calls if c.row == "14"]) == job.search_calls(market)
        assert len([c for c in calls if c.row == "16"]) == job.MULTI_TOP
        assert sum(c.hold() for c in calls if c.seed.lane == "anchor") <= 0.15 * sum(c.hold() for c in calls)
        assert {c.lane for c in calls} <= {"expansion", "exploration", "anchor"}
    assert planned["total"] <= job.load_caps()["ENGINE_DAILY"]["collect"]


def test_main_loads_research_terms_inside_existing_search_calls():
    queue = [queue_seed(f"normal {market} {i}", market=market, template="tiktok/search/top")
             for market in ("ZA", "NG", "KE") for i in range(4)]
    bq, client = FakeBQ(seeds=queue), FakeClient()
    assert run_main(["--run-date", "2026-09-29"], bq=bq, client=client) == 0
    research = [c for c in client.calls if c["seed_key"] and c["seed_key"].startswith("research_r2:")]
    assert research
    assert all(c["route"] == "tiktok/search/top" and
               set(c["params"]) == {"query", "country", "publish_time", "seen", "max_pages"} for c in research)
    assert {market: sum(c["market"] == market for c in research) for market in ("ZA", "NG", "KE")} == {
        "ZA": 1, "NG": 1, "KE": 1}


@pytest.mark.usefixtures("collect_share_2000")
def test_research_terms_rotate_inside_existing_search_holds():
    wednesday = job.plan(date(2026, 9, 30))
    monday = job.plan(MONDAY)
    expected = {
        "2026-09-30": ({"ZA": (56, 7), "NG": (56, 7), "KE": (65, 7)}, 368, 1891, 1925),  # with one row 14i call
        "2026-10-05": ({"ZA": (60, 7), "NG": (56, 7), "KE": (61, 7)}, 368, 1926, 1960),
    }

    for day, planned in (("2026-09-30", wednesday), ("2026-10-05", monday)):
        counts, total_calls, social_hold, combined_hold = expected[day]
        assert sum(map(len, planned["calls"].values())) == total_calls
        assert planned["total"] == social_hold
        assert planned["total"] + ls.total_hold(ls.plan(date.fromisoformat(day))) == combined_hold
        for market, (row14, row16) in counts.items():
            calls = planned["calls"][market]
            assert (sum(c.row == "14" for c in calls), sum(c.row == "16" for c in calls)) == (row14, row16)
            research = [c for c in calls if c.seed_key and c.seed_key.startswith("research_r2:")]
            assert research
            assert len(research) == 1
            assert all(c.row == "14" and c.route == "tiktok/search/top" for c in research)
            assert all(set(c.params) == {"query", "country", "publish_time", "seen", "max_pages"} for c in research)
            assert all(c.seed_key == f"research_r2:{c.params['query']}" and c.params["country"] == market
                       for c in research)

    for market in ("ZA", "NG", "KE"):
        wednesday_terms = {c.params["query"] for c in wednesday["calls"][market]
                           if c.seed_key and c.seed_key.startswith("research_r2:")}
        monday_terms = {c.params["query"] for c in monday["calls"][market]
                        if c.seed_key and c.seed_key.startswith("research_r2:")}
        assert wednesday_terms != monday_terms


def test_research_search_term_does_not_become_location_evidence_or_a_seed_queue_row():
    research = queue_seed("Johannesburg", day=TUESDAY, template="tiktok/search/top", item_id=None)
    research["item_id"] = None
    research["source"] = "research_r2"
    research["source_urls"] = ["https://example.com/research"]
    research["location_evidence"] = False
    fillers = [queue_seed(f"filler {i}") for i in range(4)]
    calls = job.expansion_calls("ZA", TUESDAY, {"hashtag": [], "sound": []}, random.Random(1), set(),
                                queue=[research, *fillers])
    [call] = [c for c in calls if c.seed and c.seed.query == "Johannesburg"]
    assert call.seed_key == "research_r2:Johannesburg"
    assert call.params == {"query": "Johannesburg", "country": "ZA", "publish_time": "this-week",
                           "seen": "f42-za-2026-09-29"}

    body = {"success": True, "data": {"items": [{
        "id": "987654321", "url": "https://www.tiktok.com/@maker/video/987654321", "platform": "tiktok",
        "author": {"username": "maker"}, "content": {"text": "A local trend"},
    }]}}
    runner = job._Runner(FakeClient(), job.Collected("research-test"), job._CountedIds(fake_item_id),
                         job.safe_geo(FakeGeo()), lambda: NOW, job.Budget(), None, TUESDAY)
    result = SimpleNamespace(status="ok", body=body)
    parsed = runner._parse(call, result, NOW)
    [post] = parsed["posts"]
    [creator] = runner.run.creators
    [observation] = parsed["observations"]
    assert (post["geo_market"], creator["profile_location"]) == (None, None)
    assert (observation["source_market"], observation["source_region"]) == (None, None)

    normal_call = copy.copy(call)
    normal_call.seed = None
    normal_call.seed_key = "normal-search"
    normal = runner._parse(normal_call, result, NOW)
    [normal_observation] = normal["observations"]
    assert (normal_observation["source_market"], normal_observation["source_region"]) == ("ZA", "ZA")

    geo_body = copy.deepcopy(body)
    geo_body["data"]["items"][0]["ext"] = {"region": "ZA"}
    geo_runner = job._Runner(FakeClient(), job.Collected("research-geo-test"), job._CountedIds(fake_item_id),
                             job.safe_geo(FakeGeo()), lambda: NOW, job.Budget(), None, TUESDAY)
    geo_parsed = geo_runner._parse(call, SimpleNamespace(status="ok", body=geo_body), NOW)
    [geo_post] = geo_parsed["posts"]
    [geo_observation] = geo_parsed["observations"]
    assert (geo_post["geo_market"], geo_post["geo_source"]) == ("ZA", "ext_region")
    assert (geo_observation["source_market"], geo_observation["source_region"]) == (None, None)

    unscoped = {key: value for key, value in call.params.items() if key != "country"}
    unscoped_result = job.parse_with_creators(call.route, unscoped, "ZA", body, NOW, "research-test",
                                              item_id_fn=fake_item_id, geo_fn=FakeGeo(), lane=call.lane,
                                              seed_key=call.seed_key)
    [unscoped_observation] = unscoped_result["observations"]
    assert (unscoped_observation["source_market"], unscoped_observation["source_region"]) == (None, None)
    assert job.seeding.yield_rows(TUESDAY, "ZA", [(call, SimpleNamespace(status="ok"), parsed)]) == []


def test_plan_prints_the_seeds_and_the_expansion_shares(capsys):
    assert job.main(["--plan", "--run-date", "2026-09-29"], env={}, bq=object(), runs=object(), jobs=object()) == 0
    out = capsys.readouterr().out
    assert "<ZA expansion seed 1>" in out and "<KE anchor seed 1>" in out
    assert len(re.findall(r"^  expansion \d+ credits in rows 14 and 16: queue seeds \d+%, anchor \d+%, "
                          r"exploration \d+%, largest cluster \d+%$", out, re.M)) == 3


@pytest.mark.parametrize("template,kind,query,item_id,route,params", [
    ("search/multi", "topic", "Cyril Ramaphosa", "topic|cyril", "search/multi",
     {"query": "Cyril Ramaphosa", "platforms": job.MULTI_PLATFORMS, "since": "2026-09-27"}),
    ("search/multi", "brand", "Sunday Times", "brand|sunday-times", "search/multi",
     {"query": "Sunday Times", "platforms": "twitter,threads,reddit", "since": "2026-09-27"}),
    ("tiktok/search/top", "topic", "braai", "topic|braai", "tiktok/search/top",
     {"query": "braai", "country": "ZA", "publish_time": "this-week", "seen": "f42-za-2026-09-29"}),
    ("tiktok/search/hashtag", "hashtag", "kotarun", None, "tiktok/search/hashtag",
     {"hashtag": "kotarun", "region": "ZA", "max_age_days": 7}),
    ("tiktok/song/videos", "sound", "7300000000000000009", "sound|tiktok:7300000000000000009",
     "tiktok/song/videos", {"clipId": "7300000000000000009", "use": 1}),
    ("instagram/audio/reels", "sound", "ig_audio_77", "sound|instagram:ig_audio_77", "instagram/audio/reels",
     {"audio_id": "ig_audio_77"}),
    ("tiktok/profile/videos", "creator", "kasi.keys", "creator|tiktok:kasi.keys", "tiktok/profile/videos",
     {"handle": "kasi.keys"}),
    ("twitter/user/tweets", "creator", "SundayTimesZA", "creator|twitter:sundaytimesza", "twitter/user/tweets",
     {"handle": "SundayTimesZA", "since": "2026-09-27"}),
    ("facebook/profile/posts", "brand", "12345", "brand|sunday-times", "facebook/profile/posts",
     {"pageId": "12345", "since": "2026-09-27"}),
])
def test_expansion_calls_preserve_each_seed_template_lane_and_key(template, kind, query, item_id, route, params):
    target = queue_seed(query, template=template, kind=kind, item_id=item_id)
    if item_id is None:
        target["item_id"] = None
    fillers = [queue_seed(f"filler {i}") for i in range(4)]
    calls = job.expansion_calls("ZA", TUESDAY, {"hashtag": [], "sound": []}, random.Random(1), set(),
                                queue=[target, *fillers])
    [call] = [call for call in calls if call.seed and call.seed.query == query]
    assert (call.route, call.params, call.lane, call.seed_key, call.seed.item_id) == (
        route, params, "expansion", item_id or query, item_id)


def test_expansion_calls_keep_anchor_lane_and_queued_item_key():
    queue = [queue_seed(f"filler {i}") for i in range(4)]
    anchor = queue_seed("braai", lane="anchor", template="tiktok/search/top", item_id="topic|braai")
    ranked = {"hashtag": [f"harvest{i}" for i in range(15)], "sound": []}
    calls = job.expansion_calls("ZA", TUESDAY, ranked, random.Random(1), set(), queue=[*queue, anchor])
    [call] = [call for call in calls if call.seed and call.seed.query == "braai"]
    assert (call.route, call.lane, call.seed_key, call.seed.item_id) == (
        "tiktok/search/top", "anchor", "topic|braai", "topic|braai")


def test_placebo_runs_on_row_23c_with_its_template_lane_and_key_within_expansion_hold():
    queue = [queue_seed(f"filler {i}") for i in range(4)]
    placebo = queue_seed("low topic", lane="placebo", template="tiktok/search/top", item_id="topic|low")
    queue.append(placebo)
    live = job.seeding.live(queue, TUESDAY)["ZA"]
    calls = job.expansion_calls("ZA", TUESDAY, {"hashtag": [], "sound": []}, random.Random(1), set(),
                                queue=live)
    call = next((c for c in calls if c.row == "23c" and c.seed_key == "topic|low"), None)
    assert call is not None
    assert (call.route, call.market, call.lane, call.seed_key, call.params["query"]) == (
        "tiktok/search/top", "ZA", "placebo", "topic|low", "low topic")
    assert sum(c.hold() for c in calls if c.row in ("14", "16", "23c")) <= job.SEARCH_TOP + 7 * 10


def test_route_specific_row14_exploration_hold_keeps_placebo_reserve_within_caps():
    class PreferQueued:
        values = iter((0.1, 0.2, 0.99))

        def betavariate(self, alpha, beta):
            return next(self.values)

    queue = [queue_seed(f"filler {i}") for i in range(4)]
    queue += [queue_seed("explore multi", lane="exploration", item_id="topic|explore-multi")]
    queue += [queue_seed("placebo top", lane="placebo", template="tiktok/search/top", item_id="topic|placebo")]
    ranked = {"hashtag": [f"harvest{i:02}" for i in range(15)], "sound": []}
    live = job.seeding.live(queue, TUESDAY)["ZA"]
    calls = job.expansion_calls("ZA", TUESDAY, ranked, PreferQueued(), set(), queue=live)
    row14 = sum(call.hold() for call in calls if call.row == "14")
    placebo = sum(call.hold() for call in calls if call.row == "23c")
    total = sum(call.hold() for call in calls if call.row in ("14", "16", "23c"))
    assert row14 + placebo <= job.SEARCH_TOP
    assert total <= job.SEARCH_TOP + 7 * 10


@pytest.mark.usefixtures("collect_share_2000")
def test_unaffordable_placebo_is_skipped_with_expansion_share_reason():
    queue = [dict(queue_seed(f"low topic {i}", lane="placebo", template="tiktok/search/top",
                             item_id=f"topic|low {i}"), priority=96 - i) for i in range(96)]
    client = FakeClient()
    run = collect(client, seeds=queue)
    placebo_calls = [c for c in client.calls if c["lane"] == "placebo"]
    cut = [r for r in run.records if r["row"] == "23c" and r["status"] == "skipped"]
    assert [c["seed_key"] for c in placebo_calls] == [f"topic|low {i}" for i in range(95)]
    assert len(cut) == 1 and "expansion share" in cut[0]["reason"]


def test_main_reads_live_seeds_and_appends_one_yield_row_per_seed_used():
    queue = [queue_seed("Cyril Ramaphosa"), queue_seed("old news", day=TUESDAY - timedelta(days=3)),
             queue_seed("braai", lane="anchor", template="tiktok/search/top")]
    bq, client = FakeBQ(seeds=queue), FakeClient()
    assert run_main(["--run-date", "2026-09-29"], bq=bq, client=client) == 0
    [(sql, params)] = [(s, p) for s, p in bq.queries if "seed_queue" in s]
    assert sql.lstrip().upper().startswith("SELECT") and params["d"].value == TUESDAY
    cyril = [c for c in client.calls if c["params"].get("query") == "Cyril Ramaphosa"]
    assert [c["params"] for c in cyril] == [{"query": "Cyril Ramaphosa", "platforms": job.MULTI_PLATFORMS,
                                             "since": "2026-09-27"}]
    assert cyril[0]["item_id"] == "topic|cyril ramaphosa" and cyril[0]["lane"] == "expansion"
    assert not [c for c in client.calls if c["params"].get("query") == "old news"]
    braai = [c for c in client.calls if c["params"].get("query") == "braai"]
    assert braai and braai[0]["lane"] == "anchor"
    rows = bq.loaded("seed_queue")
    got = {(r["market"], r["query"], r["lane"]): r for r in rows}
    row = got[("ZA", "Cyril Ramaphosa", "expansion")]
    assert (row["item_id"], row["seed_date"], row["ttl_days"], row["template"], row["credits_estimate"]) == (
        "topic|cyril ramaphosa", "2026-09-29", 0, "search/multi", None)
    assert row["yield_posts"] > 0 and row["yield_new_creators"] is not None
    assert ("ZA", "braai", "anchor") in got
    assert {m for m, _, lane in got if lane == "exploration"} == {"ZA", "NG", "KE"}
    assert all(r["ttl_days"] == 0 and r["yield_posts"] is not None for r in rows)
    assert all(cfg.write_disposition == "WRITE_APPEND" for t, _, cfg in bq.loads if t.endswith(".seed_queue"))
    assert not [s for s, _ in bq.queries if "seed_queue" in s and re.search(r"\b(MERGE|UPDATE|INSERT)\b", s)]


def test_main_subtracts_known_posts_and_counts_distinct_seed_posts(monkeypatch):
    body = copy.deepcopy(FIXTURE["search/multi"])
    instagram_items = body["data"]["sources"]["instagram"]["items"]
    instagram_items.append(copy.deepcopy(instagram_items[0]))
    monkeypatch.setitem(FIXTURE, "search/multi", body)
    bq = FakeBQ(seeds=[queue_seed("Cyril Ramaphosa")])
    known = ids.post_id("instagram", "ig_j1")
    other = ids.post_id("twitter", "x_j1")
    bq.posts[known] = {"post_id": known, "geo_market": None}
    assert run_main(["--run-date", "2026-09-29"], bq=bq, client=FakeClient()) == 0
    [row] = [r for r in bq.loaded("seed_queue") if r["query"] == "Cyril Ramaphosa" and r["lane"] == "expansion"]
    assert row["yield_posts"] == 1
    [(sql, params)] = [(s, p) for s, p in bq.queries if "post_id IN UNNEST(@ids)" in s]
    assert "posts" in sql and {known, other} <= set(params["ids"].values)
    assert bq.loaded("post_observations") and bq.loaded("collection_health")


def test_main_keeps_writes_and_unknown_yield_when_known_posts_read_fails():
    class FailedKnownPostsRead(FakeBQ):
        def query(self, sql, job_config=None, **kw):
            if "post_id IN UNNEST(@ids)" in sql:
                raise RuntimeError("posts read timed out")
            return super().query(sql, job_config, **kw)

    bq = FailedKnownPostsRead(seeds=[queue_seed("Cyril Ramaphosa")])
    runs = chain.MemoryRunsStore()
    assert run_main(["--run-date", "2026-09-29"], bq=bq, runs=runs, client=FakeClient()) == 0
    [row] = [r for r in bq.loaded("seed_queue") if r["query"] == "Cyril Ramaphosa" and r["lane"] == "expansion"]
    assert row["yield_posts"] is None and runs.rows[-1]["status"] == "ok"
    assert bq.loaded("post_observations") and bq.loaded("collection_health")


def test_collect_parses_seed_templates_with_their_lane_and_key():
    seed_id = "sound|tiktok:7300000000000000009"
    queue = [queue_seed("7300000000000000009", template="tiktok/song/videos", kind="sound", item_id=seed_id)]
    queue.extend(queue_seed(f"filler {i}") for i in range(4))
    client = FakeClient()
    run = collect(client, seeds=queue)
    call = next((call for call in client.calls if call["route"] == "tiktok/song/videos" and
                 call["seed_key"] == seed_id), None)
    assert call is not None
    observation = next((o for o in run.observations if o["seed_key"] == seed_id), None)
    assert observation is not None
    assert (call["lane"], call["seed_key"], call["item_id"]) == ("expansion", seed_id, seed_id)
    assert (observation["route"], observation["lane"], observation["lane_class"], observation["series"]) == (
        "tiktok/song/videos", "expansion", "search_presence", "search")
    assert (observation["source_market"], observation["source_region"]) == (None, None)


def test_two_consecutive_days_show_earned_seeds_used():
    wednesday = TUESDAY + timedelta(days=1)
    day1 = [queue_seed("Cyril Ramaphosa", day=TUESDAY), queue_seed("gqom", day=TUESDAY, lane="exploration")]
    first = collect(FakeClient(), seeds=day1)
    day2 = day1 + first.seed_rows + [queue_seed("Tyla", day=wednesday)]
    second = job.collect(FakeClient(), wednesday, "collect-test-2", item_id_fn=fake_item_id, geo_fn=FakeGeo(),
                         clock=lambda: NOW + timedelta(days=1), seeds=day2)
    for run in (first, second):
        earned = [r for r in run.seed_rows if r["market"] == "ZA" and r["item_id"] and r["lane"] == "expansion"]
        assert earned, run.run_id
    assert {"Cyril Ramaphosa", "Tyla"} <= {r["query"] for r in second.seed_rows if r["market"] == "ZA"}
    assert second.counts()["seeds_used"] == len(second.seed_rows)


# The local sources phase (task 2.12): after the SocialCrawl phases, on the collect share, lane local

class BothClient(FakeClient):
    """The job's fake for the SocialCrawl phases; a call in lane local goes to the local sources fake."""

    def __init__(self, script=None, local=None):
        super().__init__(script)
        self.local = local or LocalClient()

    def call(self, route, params=None, *, lane=None, **kw):
        if lane == "local":
            return self.local.call(route, params, lane=lane, **kw)
        return super().call(route, params, lane=lane, **kw)


def local_collect(client=None, get=None, **kw):
    return collect(client or BothClient(), local_get=get or FakeGet(), **kw)


def local_records(run):
    return [r for r in run.records if r["row"] == ls.ROW]


def charged(calls):
    return sum(quote_for(c["route"], PRICED[c["route"]].method, c["params"]) for c in calls)


BLOCKED_SEED = bytes([71, 101, 110, 32, 90]).decode() + " Afrobeats"  # spelt in bytes so no .pyc holds the word


def test_the_local_phase_runs_after_the_socialcrawl_phases_with_ledger_lane_local():
    client = BothClient()
    run = local_collect(client)
    assert client.local.calls and {c["lane"] for c in client.local.calls} == {"local"}
    assert not [c for c in client.calls if c["lane"] == "local"]
    local = local_records(run)
    assert len(local) == len(ls.plan(TUESDAY)) and run.records[-len(local):] == local
    assert {"board_kworb_spotify", "board_boomplay", "board_shazam", "board_turntable",
            "board_app_store_iphone"} <= {c["series"] for c in run.counters}
    assert "panel_ig_gossip" in {o["series"] for o in run.observations}
    assert "instagram" in {p["platform"] for p in run.posts}
    assert not {"board_audiomack", "board_nairaland"} & {r["series"] for r in local}  # retired
    assert run.local_credits == charged(client.local.calls) > 0
    counts = run.counts()
    assert counts["local_credits"] == run.local_credits and counts["local_records"] == len(local)
    assert counts["credits_charged"] == run.credits + run.local_credits
    assert counts["credits_by_share"]["LOCAL"] == run.local_credits
    assert sum(counts["credits_by_share"].values()) == counts["credits_charged"]
    assert counts["local_error"] is None


def test_without_a_local_getter_the_collect_makes_only_the_socialcrawl_calls():
    client = BothClient()
    run = collect(client)
    assert client.local.calls == [] and local_records(run) == [] and run.local_credits == 0


def both_http(method, url, *, params=None, json=None, headers=None, timeout=60):
    """vendor_http, with the local sources' chart pages and gossip panel answered from their own fixtures."""
    route = url.split("/v1/", 1)[1]
    if route == "credits/balance":
        return 200, {"data": {"balance": 100000}}, {}
    sent = params if method == "GET" else json
    gossip = route == "prism/profiles" and sent["items"][0]["handle"] in sum(ls.IG_GOSSIP.values(), ())
    body = envelope(SCRAPE[HOSTS[urlsplit(sent["url"]).netloc]], sent["url"]) if route == "web/scrape" \
        else copy.deepcopy(PROFILES) if gossip else body_for(route, sent)
    return 200, body, {"x-credit-cost": str(quote_for(route, method, sent))}


def test_the_real_client_ledgers_local_calls_in_lane_local_against_the_collect_share(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "test-key")
    ledger = MemoryLedgerStore()
    client = SocialCrawlClient(share="collect", run_id="collect-test", mode="live", ledger=ledger,
                               raw=MemoryRawStore(), http=both_http, clock=lambda: NOW)
    run = local_collect(client)
    local = [r for r in ledger.rows if r["lane"] == "local"]
    assert {r["route"] for r in local} == {"web/scrape", "prism/profiles"}
    assert not [r for r in ledger.rows if r["route"] == "web/scrape" and r["lane"] != "local"]
    assert {r["job"] for r in ledger.rows} == {"collect"}
    assert sum(r["credits_charged"] for r in local) == run.local_credits > 0
    assert ledger.spent(TUESDAY, TUESDAY, job="collect") == run.credits + run.local_credits <= \
        job.load_caps()["ENGINE_DAILY"]["collect"]
    local = local_records(run)
    assert not [r for r in local if r["market"] == "KE" and r["series"] == "board_kworb_spotify"]
    assert not [r for r in local if "ewn.co.za" in r["protocol"]]
    assert {r["status"] for r in local} <= {"ok", "not_priced", "http_500"}


def test_the_local_phase_is_not_made_when_the_collect_share_has_less_than_its_hold_left(monkeypatch):
    # The baseline run's share sits just under PAGES_SHARE, so row 14 and 16 have the size they keep at the
    # lowered caps below (job.volume); the curated panel is then the only spend that moves with the cap.
    below = copy.deepcopy(job.load_caps())
    below["ENGINE_DAILY"]["collect"] = job.PAGES_SHARE - 1
    monkeypatch.setattr(job, "load_caps", lambda: copy.deepcopy(below))
    assert job.volume() == job.volume(job.PAGES_SHARE - 1000)
    baseline_client = FakeClient()
    baseline_run = collect(baseline_client)
    creator_hold = sum(c.hold() for cs in job.plan(TUESDAY)["calls"].values() for c in cs
                       if c.route == "prism/profiles" and not _desk(c))
    # Row 14i also moves with the cap (it takes only the room left), and below it is never made.
    reels_charged = sum(c["credits"] for c in baseline_client.charged if c["route"] == job.REELS_ROUTE)
    spent = baseline_run.credits - creator_hold - reels_charged
    hold = ls.total_hold(ls.plan(TUESDAY))
    for cap, made in ((spent + hold - 1, False), (spent + hold, True)):
        caps = copy.deepcopy(job.load_caps())
        caps["ENGINE_DAILY"]["collect"] = cap
        monkeypatch.setattr(job, "load_caps", lambda caps=caps: caps)
        client, get = BothClient(), FakeGet()
        run = local_collect(client, get)
        local = local_records(run)
        assert len(local) == len(ls.plan(TUESDAY)), cap
        if made:
            assert client.local.calls and get.urls and run.local_credits > 0
        else:
            assert client.local.calls == [] and get.urls == [] and run.local_credits == 0
            assert all(r["status"] == "not_made" and r["calls"] == 0 and not r["ok"] for r in local)
            assert all(f"{cap - spent} of the collect share's {cap} left" in r["reason"] for r in local)
            assert all(f"local hold {hold}" in r["reason"] for r in local)


def test_under_a_cap_override_the_local_phase_sees_what_the_socialcrawl_phases_charged():
    client = BothClient()
    run = local_collect(client, share_cap=150)
    assert run.credits > 150 - ls.total_hold(ls.plan(TUESDAY))
    assert client.local.calls == [] and run.local_credits == 0
    assert {r["status"] for r in local_records(run)} == {"not_made"}


def test_a_local_stop_does_not_stop_the_socialcrawl_phases():
    alone = collect(FakeClient())
    local = LocalClient(status=lambda n, route: "cap_reached" if n == 1 else "ok")
    run = local_collect(BothClient(local=local))
    crawl = [r for r in run.records if r["row"] != ls.ROW]
    assert [(r["row"], r["route"], r["market"], r["status"]) for r in crawl] == \
        [(r["row"], r["route"], r["market"], r["status"]) for r in alone.records]
    assert run.stopped is None and not run.stopped_markets and run.credits == alone.credits
    statuses = [r["status"] for r in local_records(run) if r["route"] in ("web/scrape", "prism/profiles")]
    assert statuses[0] == "cap_reached" and set(statuses[1:]) == {"not_made"} and len(local.calls) == 1


def test_a_socialcrawl_market_stop_does_not_stop_the_local_phase():
    def script(n, route, market):
        return "cap_reached" if market == "ZA" else None

    client = BothClient(script)
    run = local_collect(client)
    assert run.stopped_markets == {"ZA"}
    assert {c["market"] for c in client.local.calls} == {"ZA", "NG", "KE"}
    assert [r for r in local_records(run) if r["market"] == "ZA" and r["ok"]]


@pytest.mark.parametrize("status", ["insufficient_credits", "balance_floor"])
def test_a_run_stopped_for_credits_or_balance_makes_no_local_fetch(status):
    client, get = BothClient(lambda n, route, market: status if n == 3 else None), FakeGet()
    run = local_collect(client, get)
    assert run.stopped == status and client.local.calls == [] and get.urls == []
    local = local_records(run)
    assert local and all(r["status"] == "not_made" and r["calls"] == 0 and status in r["reason"] for r in local)


def test_news_seeds_are_rule_one_filtered_then_appended_to_seed_queue(monkeypatch):
    assert blocked(BLOCKED_SEED)
    real = ls.run

    def with_a_blocked_seed(*args, **kw):
        out = real(*args, **kw)
        out["seeds"].append(dict(out["seeds"][0], query=BLOCKED_SEED, item_id="topic|blocked"))
        return out

    monkeypatch.setattr(ls, "run", with_a_blocked_seed)
    bq, runs = FakeBQ(), chain.MemoryRunsStore()
    assert run_main(["--run-date", "2026-09-29"], bq=bq, runs=runs, client=BothClient(), get=FakeGet()) == 0
    rows = bq.loaded("seed_queue")
    news = [r for r in rows if r["ttl_days"] == ls.SEED_TTL_DAYS]
    assert news and {r["market"] for r in news} == {"NG", "KE"}
    assert not [r for r in rows if blocked(r["query"])]
    assert all(set(r) == {name for name, _ in SEED_FIELDS} for r in news)
    assert all(r["seed_date"] == "2026-09-29" and r["lane"] == "expansion" and r["template"] == "search/multi"
               for r in news)
    json.dumps(rows)
    assert runs.rows[-1]["counts"]["news_seeds"] == len(news)
    assert all(cfg.write_disposition == "WRITE_APPEND" for t, _, cfg in bq.loads if t.endswith(".seed_queue"))


def test_main_writes_the_local_rows_through_the_existing_writers_and_counts_them():
    kworb = next(f for f in ls.plan(TUESDAY) if f.source == "kworb_spotify" and f.market == "NG")
    bq = FakeBQ(pulls=[{"market": "NG", "series": kworb.series, "protocol": kworb.protocol(), "last_pull": 4}])
    runs, client = chain.MemoryRunsStore(), BothClient()
    assert run_main(["--run-date", "2026-09-29"], bq=bq, runs=runs, client=client, get=FakeGet()) == 0
    counters = bq.loaded("item_counter_daily")
    assert {c["pull_seq"] for c in counters if c["series"] == kworb.series and c["market"] == "NG"
            and c["unit"] == "rank"} == {5}
    assert {"board_kworb_spotify", "board_app_store_iphone"} <= {c["series"] for c in counters}
    assert "panel_ig_gossip" in {o["series"] for o in bq.loaded("post_observations")}
    assert any(p["platform"] == "instagram" for p in bq.posts.values())
    observations = bq.loaded("post_observations")
    for route in ("tiktok/trending", "youtube/videos/trending", "tiktok/search/top"):
        scoped = [o for o in observations if o["route"] == route]
        if route == "tiktok/search/top":
            research = [o for o in scoped if str(o.get("seed_key") or "").startswith("research_r2:")]
            assert research and all((o["source_market"], o["source_region"]) == (None, None) for o in research)
            scoped = [o for o in scoped if not str(o.get("seed_key") or "").startswith("research_r2:")]
        assert scoped and all((o["source_market"], o["source_region"]) == (o["market"], o["market"])
                              for o in scoped)
    facebook = [o for o in observations if o["route"] == "facebook/profile/posts"]
    assert facebook and all(o["source_market"] == o["market"] and o["source_region"] is None for o in facebook)
    gossip = [o for o in observations if o["series"] == "panel_ig_gossip"]
    assert gossip and all(o["source_market"] is None and o["source_region"] is None for o in gossip)
    health = {(h["market"], h["series"]) for h in bq.loaded("collection_health")}
    assert {("NG", "board_turntable"), ("ZA", "board_kworb_spotify"), ("KE", "news_rss"),
            ("NG", "board_google_play")} <= health
    assert not {h for h in health if h[1] in ("board_nairaland", "board_audiomack")}  # retired, no health row
    counts = runs.rows[-1]["counts"]
    assert counts["local_credits"] == charged(client.local.calls) > 0
    assert counts["local_records"] == len(ls.plan(TUESDAY))
    assert counts["credits_charged"] == charged(client.calls) + counts["local_credits"]


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_configured_instagram_location_call_carries_its_listed_market(market):
    config = job.load_config()
    calls, _ = job.harvest_calls(market, TUESDAY, config)
    call = [c for c in calls if c.route == "instagram/location/posts"][0]
    assert call.params["location_id"] in config["markets"][market.lower()]["instagram_locations"]["values"]
    assert call.source_market == market
    run = job.Collected("source-market-test")
    runner = job._Runner(None, run, fake_item_id, FakeGeo(), lambda: NOW, None, {}, TUESDAY)
    stored, items, labels = split_vendor_labels(body_for(call.route, call.params, call.market))
    result = Result("ok", call.route, "hash", 200, call.hold(), call.hold(), False, stored, items, labels)
    parsed = runner._parse(call, result, NOW)
    assert parsed["observations"]
    assert {(o["source_market"], o["source_region"]) for o in parsed["observations"]} == {(market, None)}


def test_row_5_health_key_names_the_location_it_read():
    """Each hub is its own series, so the items reference compares a location with its own past days,
    not with the median of every location read."""
    config = job.load_config()
    hubs = config["markets"]["za"]["instagram_locations"]["values"]
    seen = {}
    calls, _ = job.harvest_calls("ZA", TUESDAY, config)
    for call in [c for c in calls if c.route == "instagram/location/posts"]:
        hub = call.params["location_id"]
        assert call.series_protocol() == f"instagram/location/posts?location_id={hub}"
        seen[hub] = call.series_protocol()
    assert set(seen) == set(hubs) and len(set(seen.values())) == len(hubs)


def test_row_5_items_rule_reads_the_reference_of_the_same_location():
    busy = "instagram/location/posts?location_id=busy"
    quiet = "instagram/location/posts?location_id=quiet"

    def record(protocol, items):
        return {"row": "5", "route": "instagram/location/posts", "market": "ZA", "day": "2026-10-02",
                "platform": "instagram", "series": "ig_location", "protocol": protocol,
                "lane_class": "search_presence", "status": "ok", "ok": True, "calls": 1, "units_planned": 1,
                "units_ok": 1, "items": items, "post_ids": []}

    refs = {"2026-10-02": {("ZA", "ig_location", busy): (24.0, 5), ("ZA", "ig_location", quiet): (6.0, 5)}}
    [row] = writers.health_rows([record(quiet, 6)], [], refs, "c")
    assert (row["valid"], row["invalid_reason"], row["ref_items"]) == (True, None, 6.0)
    [row] = writers.health_rows([record(busy, 6)], [], refs, "c")
    assert (row["valid"], row["invalid_reason"]) == (False, "items")


def test_instagram_location_fixture_preserves_the_recorded_body_types():
    sample_path = ROOT / "core/collect/samples/8b_instagram-location-posts.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))["body"]

    def sample_shape(value):
        if isinstance(value, dict):
            return {key: sample_shape(child) for key, child in value.items()}
        if isinstance(value, list):
            rows = value
            if rows and isinstance(rows[0], str) and re.fullmatch(r"<list of \d+>", rows[0]):
                rows = rows[1:]
            return [sample_shape(row) for row in rows]
        if isinstance(value, str):
            match = re.fullmatch(r"<(null|bool|int|str)>", value)
            if match:
                return {"null": type(None), "bool": bool, "int": int, "str": str}[match.group(1)]
        return type(value)

    def fixture_shape(value):
        if isinstance(value, dict):
            return {key: fixture_shape(child) for key, child in value.items()}
        if isinstance(value, list):
            return [fixture_shape(child) for child in value]
        return type(value)

    assert fixture_shape(FIXTURE["instagram/location/posts"]) == sample_shape(sample)


def test_last_pulls_include_the_local_rank_series_from_counters_and_observations():
    bq = FakeBQ(pulls=[{"market": "NG", "series": "board_nairaland", "protocol": "p", "last_pull": 9}])
    assert writers.last_pulls(bq, TUESDAY) == {("NG", "board_nairaland", "p"): 9}
    sql, params = bq.queries[-1]
    assert set(params["series"].values) == (set(ls.LOCAL_RANK_SERIES) | set(writers.RANK_SERIES)) - {"board_nairaland"}
    assert params["observation_series"].values == ["board_nairaland"]
    assert params["day"].value == TUESDAY and params["day"].type_ == "DATE"
    assert not re.search(r"\b(MERGE|UPDATE|INSERT|DELETE)\b", sql)


def test_last_pulls_counter_branch_is_bounded_on_its_date_partition():
    bq = FakeBQ()
    writers.last_pulls(bq, TUESDAY)
    sql, _ = bq.queries[-1]
    counters, observations = sql.split("UNION ALL")
    assert "item_counter_daily" in counters and "post_observations" not in counters
    assert f"obs_date >= DATE_SUB(@day, INTERVAL {writers.LAST_PULL_DAYS} DAY)" in counters
    assert "series IN UNNEST(@series)" in counters


def test_last_pulls_observation_branch_is_bounded_and_takes_only_observation_only_series():
    bq = FakeBQ()
    writers.last_pulls(bq, TUESDAY)
    sql, _ = bq.queries[-1]
    _, observations = sql.split("UNION ALL")
    assert "post_observations" in observations and "item_counter_daily" not in observations
    assert f"observed_date >= DATE_SUB(@day, INTERVAL {writers.LAST_PULL_DAYS} DAY)" in observations
    assert "lane_class = 'unbiased_rank'" in observations
    assert "series IN UNNEST(@observation_series)" in observations and "@series)" not in observations
    assert writers.LAST_PULL_DAYS >= 35  # longer than the 28 day reference window plus a week of outage


class LocalLedgerFails(MemoryLedgerStore):
    """A credit_ledger whose append fails for the local phase's rows only."""

    def append(self, row):
        if row.get("lane") == "local":
            raise RuntimeError("ledger append failed for a local row")
        super().append(row)


def real_client(monkeypatch, ledger=None):
    monkeypatch.setenv(KEY_ENV, "test-key")
    return SocialCrawlClient(share="collect", run_id="collect-test", mode="live", ledger=ledger or MemoryLedgerStore(),
                             raw=MemoryRawStore(), http=both_http, clock=lambda: NOW)


def test_a_local_phase_error_is_recorded_and_the_socialcrawl_rows_are_still_written(monkeypatch):
    bq, runs = FakeBQ(), chain.MemoryRunsStore()
    client = real_client(monkeypatch, LocalLedgerFails())
    assert run_main(["--run-date", "2026-09-29"], bq=bq, runs=runs, client=client, get=FakeGet()) == 0
    final = runs.rows[-1]
    assert final["status"] == "ok"
    assert "RuntimeError" in final["counts"]["local_error"] and "ledger append failed" in final["counts"]["local_error"]
    assert {o["series"] for o in bq.loaded("post_observations")} >= {"feed_tiktok", "list_reddit"}
    assert bq.posts
    local = [h for h in bq.loaded("collection_health") if h["series"] in ls.LOCAL_RANK_SERIES]
    assert local and not any(h["valid"] for h in local)
    assert final["counts"]["local_records"] == len(ls.plan(TUESDAY))


def test_a_getter_that_raises_fails_only_the_fetches_that_need_it(monkeypatch):
    def broken(url):
        raise OSError("no route to host")

    bq, runs, client = FakeBQ(), chain.MemoryRunsStore(), BothClient()
    assert run_main(["--run-date", "2026-09-29"], bq=bq, runs=runs, client=client, get=broken) == 0
    counts = runs.rows[-1]["counts"]
    assert runs.rows[-1]["status"] == "ok" and counts["local_error"] is None
    assert bq.loaded("post_observations") and bq.posts
    run = local_collect(BothClient(), broken)
    local = local_records(run)
    fetched = [r for r in local if r["route"] in ("web/scrape", "rss", "apple_rss")]
    assert not [r for r in fetched if r["market"] == "KE" and r["series"] == "board_kworb_spotify"]
    assert not [r for r in fetched if "ewn.co.za" in r["protocol"]]
    assert fetched and all(r["status"] == "robots_disallowed" and r["calls"] == 0 for r in fetched)
    assert {r["status"] for r in local if r["route"] == "prism/profiles"} == {"ok"}


def test_a_ledger_with_earlier_same_day_collect_spend_makes_local_calls_cap_reached(monkeypatch):
    share = job.load_caps()["ENGINE_DAILY"]["collect"]
    alone = collect(real_client(monkeypatch))
    ledger = MemoryLedgerStore()
    ledger.append({"trend_date": TUESDAY.isoformat(), "job": "collect", "lane": "sweep", "credits_charged":
                   share - alone.credits - 1})
    run = local_collect(real_client(monkeypatch, ledger))
    assert [(r["row"], r["route"], r["market"], r["status"]) for r in run.records if r["row"] != ls.ROW] == \
        [(r["row"], r["route"], r["market"], r["status"]) for r in alone.records]
    statuses = [r["status"] for r in local_records(run) if r["route"] in ("web/scrape", "prism/profiles")]
    assert statuses[0] == "ok" and statuses[1] == "cap_reached" and set(statuses[2:]) == {"not_made"}
    assert ledger.spent(TUESDAY, TUESDAY, job="collect") == share
    assert run.local_credits == 1


def test_local_fetches_whose_market_day_has_moved_are_not_made():
    late = datetime(2026, 9, 29, 21, 30, tzinfo=timezone.utc)  # 23:30 SAST, 22:30 WAT, 00:30 EAT on the 30th
    client, get = BothClient(), FakeGet()
    run = job.collect(client, TUESDAY, "c", item_id_fn=fake_item_id, geo_fn=FakeGeo(), clock=lambda: late,
                      local_get=get)
    assert not [c for c in client.local.calls if c["market"] == "KE"]
    assert not [u for u in get.urls if u in {f.url for f in ls.FEEDS if f.market == "KE"} or "/ke/" in u]
    ke = [r for r in local_records(run) if r["market"] == "KE"]
    assert ke and all(r["status"] == "day_changed" and r["calls"] == 0 and r["day"] == "2026-09-29" for r in ke)
    assert {c["market"] for c in client.local.calls} == {"ZA", "NG"}
    assert not [c for c in run.counters if c["market"] == "KE" and c["series"] in ls.LOCAL_RANK_SERIES]
    assert not [o for o in run.observations if o["market"] == "KE" and o["series"] == "panel_ig_gossip"]


def test_plan_prints_the_local_phase_and_the_combined_hold_against_the_collect_cap(capsys):
    assert job.main(["--plan", "--run-date", "2026-09-30"], env={}, bq=object(), runs=object(), jobs=object()) == 0
    out = capsys.readouterr().out
    day = date(2026, 9, 30)
    fetches, crawl = ls.plan(day), job.plan(day)["total"]
    hold = ls.total_hold(fetches)
    share = job.load_caps()["ENGINE_DAILY"]["collect"]
    assert all(f.url in out for f in fetches)
    assert re.search(rf"^  local sources hold {hold} credits over {len(fetches)} fetches, "
                     rf"sub-limit {ls.LOCAL_DAILY_CAP} of the collect share$", out, re.M)
    assert re.search(rf"^combined hold {crawl + hold} credits against the collect cap {share}: "
                     rf"SocialCrawl phases {crawl}, local sources {hold}$", out, re.M)
    assert crawl + hold <= share and hold <= ls.LOCAL_DAILY_CAP


# Hygiene

# Google Trends left this list on 1 Oct 2026, when Albert brought it back into 42 (SocialCrawl
# google_trends/trending, ZA, NG and KE, 24 hours, inside the collect cap); the other words stay banned.
BANNED = ["".join(p) for p in (["gen", "z"], ["gen", " z"], ["gen", "-z"], ["you", "th"], ["stu", "dent"],
                                ["te", "en"], ["mill", "ennial"], ["gener", "ation"], ["boo", "mer"],
                                ["you", "ng"], ["var", "sity"], ["adoles", "cent"])]
DASHES = [chr(0x2014), chr(0x2013)]
FORBIDDEN_SQL = re.compile(r"\b(DELETE|DROP|TRUNCATE|CREATE|REPLACE|WRITE_TRUNCATE)\b|expiration", re.I)


@pytest.mark.parametrize("name", ["job.py", "writers.py"])
def test_sources_hold_no_banned_literals_dashes_or_destructive_sql(name):
    text = (ROOT / "core" / "collect" / name).read_text(encoding="utf-8")
    lower = text.casefold()
    assert not [w for w in BANNED if w in lower]
    assert not [d for d in DASHES if d in text]
    assert not re.search(r"-{2}(?![a-z])", text), "double hyphen outside a CLI flag"
    assert not FORBIDDEN_SQL.search(text)


# The Google Trends phase: one 24 hour trending read per market on the collect share, inside the collect cap

TRENDS_BODY = {"success": True, "data": {"items": [{"title": "Springbok rugby", "rank": 1},
                                                   {"title": "Load shedding", "rank": 2}]}}


class TrendsClient(BothClient):
    """BothClient with the share and run id a real collect client carries; Google Trends calls answer here."""

    share, run_id = "collect", "collect-test"

    def __init__(self, *a, trends=None, **kw):
        super().__init__(*a, **kw)
        self.trends = trends or (lambda market: Result("ok", job.google_trends.SC_ROUTE, "hash", 200, 5, 5, False,
                                                        TRENDS_BODY))
        self.trends_calls = []

    def call(self, route, params=None, *, lane=None, market=None, **kw):
        if route == job.google_trends.SC_ROUTE:
            self.trends_calls.append({"params": dict(params or {}), "market": market, "lane": lane})
            return self.trends(market)
        return super().call(route, params, lane=lane, market=market, **kw)


def test_the_trends_phase_reads_each_market_once_on_the_collect_share_and_counts_its_credits():
    client = TrendsClient()
    run = local_collect(client, search_signals=True)
    assert [(c["market"], c["params"], c["lane"]) for c in client.trends_calls] == [
        (m, {"location": m, "hours": "24"}, "google_trending") for m in ("ZA", "NG", "KE")]
    assert run.trends_credits == 15
    assert {(s["market"], s["term"]) for s in run.search_signals} == {
        (m, t) for m in ("ZA", "NG", "KE") for t in ("Springbok rugby", "Load shedding")}
    counts = run.counts()
    assert counts["search_signals"] == 6
    assert counts["search_signal_states"] == {"ZA": "ok", "NG": "ok", "KE": "ok"}
    assert counts["credits_charged"] == run.credits + run.local_credits + 15
    assert counts["credits_by_share"]["TRENDS"] == 15
    assert sum(counts["credits_by_share"].values()) == counts["credits_charged"]


def test_without_search_signals_the_collect_makes_no_trends_call_and_its_counts_are_unchanged():
    client = TrendsClient()
    run = local_collect(client)
    assert client.trends_calls == []
    counts = run.counts()
    assert "search_signals" not in counts and "TRENDS" not in counts["credits_by_share"]


def test_the_trends_phase_is_not_made_when_the_cap_less_the_local_hold_has_under_15_credits_left():
    client = TrendsClient()
    run = job.Collected("collect-test", credits=700)
    job.trends_phase(run, client, TUESDAY, clock=lambda: NOW, cap=800, reserve=86)
    assert client.trends_calls == [] and run.trends_credits == 0
    assert run.counts()["search_signal_states"] == {m: "not_made" for m in ("ZA", "NG", "KE")}
    job.trends_phase(run, client, TUESDAY, clock=lambda: NOW, cap=800, reserve=85)
    assert len(client.trends_calls) == 3 and run.trends_credits == 15


def test_the_local_phase_sees_what_the_trends_phase_charged():
    client = TrendsClient()
    local_hold = ls.total_hold(ls.plan(TUESDAY))
    run = job.Collected("collect-test", credits=800 - local_hold - 15)
    job.trends_phase(run, client, TUESDAY, clock=lambda: NOW, cap=800, reserve=local_hold)
    assert run.trends_credits == 15
    job.local_phase(run, client, TUESDAY, get=FakeGet(), clock=lambda: NOW, item_id_fn=fake_item_id,
                    geo_fn=FakeGeo(), last_pulls=None, cap=800 - 1)
    assert client.local.calls == []


def test_a_trends_failure_never_stops_the_run_or_the_local_phase():
    def boom(market):
        raise RuntimeError("socialcrawl down")

    client = TrendsClient(trends=boom)
    run = local_collect(client, search_signals=True)
    assert len(client.trends_calls) == 3 and run.trends_credits == 0
    assert run.counts()["search_signal_states"] == {m: "unknown" for m in ("ZA", "NG", "KE")}
    assert client.local.calls and run.posts


def test_the_trends_phase_is_not_made_after_a_stop_for_credits():
    client = TrendsClient(lambda n, route, market: "insufficient_credits")
    run = local_collect(client, search_signals=True)
    assert run.stopped == "insufficient_credits" and client.trends_calls == []
    assert run.counts()["search_signal_states"] == {m: "not_made" for m in ("ZA", "NG", "KE")}


def test_main_turns_the_trends_phase_on():
    import inspect
    source = inspect.getsource(job.main)
    assert "search_signals=True" in source


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_reddit_posts_from_a_markets_own_subreddit_are_seen_in_its_feeds(market):
    config = job.load_config()
    subreddits = config["markets"][market.lower()]["subreddits"]
    own = {str(s).lower() for s in subreddits["own"]}
    calls, _ = job.harvest_calls(market, TUESDAY, config)
    reddit = [c for c in calls if c.route == "reddit/subreddit"]
    assert reddit
    for call in reddit:
        expected = market if call.params["subreddit"].lower() in own else None
        assert call.source_market == expected
        run = job.Collected("reddit-source-market-test")
        runner = job._Runner(None, run, fake_item_id, FakeGeo(), lambda: NOW, None, {}, TUESDAY)
        stored, items, labels = split_vendor_labels(body_for(call.route, call.params, call.market))
        result = Result("ok", call.route, "hash", 200, call.hold(), call.hold(), False, stored, items, labels)
        parsed = runner._parse(call, result, NOW)
        assert parsed["observations"]
        assert {(o["source_market"], o["source_region"]) for o in parsed["observations"]} == {(expected, None)}


def test_only_a_markets_own_subreddits_count_as_its_feeds():
    config = job.load_config()
    pan_african = {"africa", "africanmusic", "afrobeats", "amapiano"}
    for market in ("za", "ng", "ke"):
        subreddits = config["markets"][market]["subreddits"]
        own = {str(s).lower() for s in subreddits["own"]}
        assert own and own <= {str(s).lower() for s in subreddits["values"]}
        assert not own & pan_african


# The free BigQuery Google Trends tables: read after collect, stored as search interest, never fatal

class TrendsTablesBQ(FakeBQ):
    """FakeBQ that also answers the Google Trends partition and table reads."""

    def __init__(self, *a, fail_signals_table=False, **kw):
        from core.collect.tests.test_google_trends import FakeTrendsBQ

        super().__init__(*a, **kw)
        self.trends_tables = FakeTrendsBQ(newest={"international_top_terms": "20260928",
                                                  "international_top_rising_terms": "20260928"})
        for rows in self.trends_tables.rows.values():
            for row in rows:
                row["refresh_date"] = date(2026, 9, 28)
        self.fail_signals_table = fail_signals_table

    def query(self, sql, job_config=None, **kw):
        if "bigquery-public-data.google_trends" in sql:
            return self.trends_tables.query(sql, job_config, **kw)
        return super().query(sql, job_config, **kw)

    def get_table(self, table_id):
        if self.fail_signals_table and table_id.endswith(".google_search_signals"):
            raise RuntimeError("404 Not found: Table google_search_signals")
        return super().get_table(table_id)


@pytest.fixture
def google_bq_on(monkeypatch):
    """The BigQuery Google Trends phase is parked by default (W8-DEC-04); these tests cover it when enabled."""
    monkeypatch.setattr(job, "google_bq_enabled", lambda: True)


def test_google_bq_is_parked_by_config():
    assert job.google_bq_enabled() is False
    assert yaml.safe_load((job.CONFIG / "google_sources.yaml").read_text(encoding="utf-8"))["google_bq"]["enabled"] is False


def test_a_missing_google_sources_file_reads_as_parked(monkeypatch, tmp_path):
    monkeypatch.setattr(job, "CONFIG", tmp_path)
    assert not (tmp_path / "google_sources.yaml").exists()
    assert job.google_bq_enabled() is False


def test_a_google_sources_file_with_no_enabled_key_reads_as_parked(monkeypatch, tmp_path):
    monkeypatch.setattr(job, "CONFIG", tmp_path)
    for lines in ([], ["other: 1"], ["google_bq:"], ["google_bq: {}"], ["google_bq:", "  other: true"]):
        (tmp_path / "google_sources.yaml").write_text("\n".join(lines), encoding="utf-8")
        assert job.google_bq_enabled() is False, lines


def test_a_parked_google_bq_makes_no_public_table_read_and_says_so_in_the_counts():
    class NoPublicTables(TrendsTablesBQ):
        def query(self, sql, job_config=None, **kw):
            assert "bigquery-public-data" not in sql, "the public Google Trends tables were read"
            return super().query(sql, job_config, **kw)

    runs, bq = chain.MemoryRunsStore(), NoPublicTables()
    assert run_main(["--run-date", "2026-09-29"], runs=runs, bq=bq) == 0
    counts = runs.rows[-1]["counts"]
    assert runs.rows[-1]["status"] == "ok"
    assert set(counts["google_bq_states"].values()) == {"parked"} and counts["google_bq_signals"] == 0
    assert counts["google_bq_bytes_billed"] == 0 and counts["google_bq_error"] is None
    assert all(r["source"] != "google_bq" for r in bq.loaded("google_search_signals"))
    assert not bq.loaded("posts")


def test_google_rss_and_google_trending_stay_live_while_google_bq_is_parked(monkeypatch):
    phases = []
    real_trends, real_rss = job.trends_phase, job.google_rss.read_signals
    monkeypatch.setattr(job, "trends_phase", lambda *a, **kw: (phases.append("google_trending"), real_trends(*a, **kw))[1])
    monkeypatch.setattr(job.google_rss, "read_signals", lambda *a, **kw: (phases.append("google_rss"), real_rss(*a, **kw))[1])
    runs, bq = chain.MemoryRunsStore(), TrendsTablesBQ()
    assert run_main(["--run-date", "2026-09-29"], runs=runs, bq=bq) == 0
    assert set(phases) == {"google_trending", "google_rss"}


@pytest.mark.usefixtures("google_bq_on")
def test_main_reads_the_trends_tables_and_appends_their_rows_as_search_signals():
    runs, bq = chain.MemoryRunsStore(), TrendsTablesBQ()
    assert run_main(["--run-date", "2026-09-29"], runs=runs, bq=bq) == 0
    counts = runs.rows[-1]["counts"]
    assert runs.rows[-1]["status"] == "ok"
    assert counts["google_bq_signals"] == 3 and counts["google_search_signals"] == 3
    assert counts["google_bq_states"] == {"ZA:top": "ok", "NG:top": "ok", "ZA:rising": "ok", "NG:rising": "empty"}
    assert counts["google_bq_bytes_billed"] == 10 + 76_546_048 * 2 and counts["google_bq_error"] is None
    loaded = bq.loaded("google_search_signals")
    assert {(r["source"], r["kind"], r["market"], r["rank"]) for r in loaded} == {
        ("google_bq", "top", "ZA", 1), ("google_bq", "top", "NG", 2), ("google_bq", "rising", "ZA", 1)}
    assert {r["raw_payload"]["region_code"] for r in loaded} == {"ZA-GT", "NG-LA"}
    assert not bq.loaded("posts") and all(r.get("post_id") is None for r in loaded)


@pytest.mark.usefixtures("google_bq_on")
def test_a_failed_trends_table_read_never_stops_the_run():
    runs, jobs = chain.MemoryRunsStore(), FakeJobs()
    assert run_main(["--run-date", "2026-09-29"], runs=runs, jobs=jobs, bq=FakeBQ()) == 0
    counts = runs.rows[-1]["counts"]
    assert runs.rows[-1]["status"] == "ok" and jobs.started
    assert set(counts["google_bq_states"].values()) == {"unknown"}
    assert counts["google_bq_signals"] == 0 and counts["google_search_signals"] == 0


@pytest.mark.usefixtures("google_bq_on")
def test_an_exception_in_the_trends_table_phase_is_recorded_and_the_run_goes_on(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("client gone")

    monkeypatch.setattr(job.google_trends, "read_bq_signals", boom)
    runs = chain.MemoryRunsStore()
    assert run_main(["--run-date", "2026-09-29"], runs=runs, bq=TrendsTablesBQ()) == 0
    counts = runs.rows[-1]["counts"]
    assert runs.rows[-1]["status"] == "ok"
    assert counts["google_bq_error"] == "RuntimeError: client gone"
    assert set(counts["google_bq_states"].values()) == {"error"}


@pytest.mark.usefixtures("google_bq_on")
def test_a_failed_search_signals_append_never_stops_the_run():
    runs, bq = chain.MemoryRunsStore(), TrendsTablesBQ(fail_signals_table=True)
    assert run_main(["--run-date", "2026-09-29"], runs=runs, bq=bq) == 0
    counts = runs.rows[-1]["counts"]
    assert runs.rows[-1]["status"] == "ok"
    assert counts["google_search_signals"] == 0
    assert counts["google_search_signals_error"].startswith("RuntimeError: 404 Not found")
    assert bq.loaded("collection_health")


@pytest.mark.usefixtures("google_bq_on")
def test_the_trends_table_rows_join_the_search_signals_with_the_same_label():
    run = job.Collected("collect-test")
    job.bq_trends_phase(run, TrendsTablesBQ(), date(2026, 9, 29), clock=lambda: NOW)
    assert {(s["source"], s["label"]) for s in run.search_signals} == {("google_bq", "Google search interest")}
    assert all(set(s) == {"term", "market", "source", "rank", "refreshed_at", "label"} for s in run.search_signals)


@pytest.mark.usefixtures("google_bq_on")
def test_the_trending_read_rows_go_to_google_search_signals_beside_the_trends_table_rows():
    # The SocialCrawl trending read is the only Google search interest for KE (the public tables have no KE
    # rows), and Searching now and the readiness report read source google_trending from the table.
    run = local_collect(TrendsClient(), search_signals=True)
    job.bq_trends_phase(run, TrendsTablesBQ(), date(2026, 9, 29), clock=lambda: NOW)
    trending = [r for r in run.search_rows if r["source"] == "google_trending"]
    assert {(r["market"], r["term"], r["rank"], r["kind"]) for r in trending} == {
        (m, t, n, "trending") for m in ("ZA", "NG", "KE") for t, n in (("Springbok rugby", 1), ("Load shedding", 2))}
    assert {r["source"] for r in run.search_rows} == {"google_trending", "google_bq"}
    assert all(isinstance(r["fetched_at"], str) for r in run.search_rows)
    counts = run.counts()
    assert counts["google_bq_signals"] == 3 and counts["search_signals"] == 9
    bq = FakeBQ()
    assert writers.write_search_signals(bq, run.search_rows) == (9, None)
    assert {(r["market"], r["source"]) for r in bq.loaded("google_search_signals")} >= {
        ("KE", "google_trending"), ("ZA", "google_bq")}


@pytest.mark.usefixtures("collect_share_2000")
def test_row14_reads_60_country_searches_a_market_and_ke_keeps_its_feed_credits():
    # Row 14 posts come back located by TikTok's own region field, the supply the Today locality check reads.
    # 60 a market under the 2,000 collect share, and KE adds the 10 credits of its two dropped local feed pulls.
    for day in (TUESDAY, MONDAY):
        planned = job.plan(day)["calls"]
        # Row 14's credits: 60 searches (KE 65) at two pages; a queued seed with its own template can take
        # several of those credits in one call, so the credits are what the plan holds to.
        credits = {m: sum(c.hold() for c in planned[m] if c.row == "14") for m in ("ZA", "NG", "KE")}
        assert credits == {"ZA": 120, "NG": 120, "KE": 130}
        # Row 14 can also carry an exploration seed on its own template (search/multi), which has no country.
        assert all(c.params["country"] == m for m in ("ZA", "NG", "KE") for c in planned[m]
                   if c.row == "14" and c.route == "tiktok/search/top")
    for day in (TUESDAY, MONDAY):
        combined = job.plan(day)["total"] + ls.total_hold(ls.plan(day))
        assert combined <= job.load_caps()["ENGINE_DAILY"]["collect"]


def test_volume_grows_with_the_collect_share_and_keeps_today_s_plan_below_2000():
    assert job.volume(1000) == job.volume(1999) == (job.SEARCH_TOP, 1, job.MULTI_TOP)
    assert job.volume(2000) == (job.SEARCH_TOP, 2, job.MULTI_TOP)
    assert job.volume(3000) == (job.SEARCH_TOP + 83, 2, job.MULTI_TOP + 16)
    assert job.volume(4000) == (job.SEARCH_TOP + 166, 2, job.MULTI_TOP + 33)


@pytest.mark.parametrize("share", [1000, 2000, 3000, 4000])
def test_the_scaled_plan_fits_its_collect_share_with_row_14_and_16_sized_by_it(monkeypatch, share):
    caps = copy.deepcopy(job.load_caps())
    caps["ENGINE_DAILY"]["collect"] = share
    monkeypatch.setattr(job, "load_caps", lambda: copy.deepcopy(caps))
    size = job.volume()
    for day in (TUESDAY, MONDAY):
        planned = job.plan(day)
        assert planned["total"] + ls.total_hold(ls.plan(day)) <= share
        for market in job.MARKETS:
            calls = planned["calls"][market]
            row14 = [c for c in calls if c.row == "14" and c.route == "tiktok/search/top"]
            # A queued seed with its own template can take several credits in one call: the credits hold.
            assert sum(c.hold() for c in calls if c.row == "14") == job.search_calls(market, size) * size.pages
            assert all(c.params.get("max_pages", 1) == size.pages and c.hold() == size.pages for c in row14)
            assert sum(c.row == "16" for c in calls) == size.multi_top
            assert all(c.lane_class() == "search_presence" for c in row14)


def test_song_curve_samples_are_skipped_and_counted_never_written_as_counters():
    fixtures = json.loads((Path(__file__).resolve().parent / "fixtures" / "parse_stored_song_videos.json")
                          .read_text(encoding="utf-8"))
    runner = job._Runner(FakeClient(), job.Collected("curve-test"), job._CountedIds(fake_item_id),
                         job.safe_geo(FakeGeo()), lambda: NOW, job.Budget(), None, TUESDAY)
    call = job.Call("11", "tiktok/song/videos", {"clipId": "m100", "use": 1}, "ZA", "watchlist", seed_key="m100")
    for name in ("song_videos_1", "song_videos_2"):
        parsed = runner._parse(call, SimpleNamespace(status="ok", body=fixtures[name]["body"]), NOW)
        assert parsed["counters"] == [] and parsed["posts"]
    assert runner.run.counters == []
    assert runner.run.counts()["song_curve_sample_skipped"] == 14 + 16
