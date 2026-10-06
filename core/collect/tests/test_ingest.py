"""Tests for core.collect.parse.ingest: a confirm-lane SocialCrawl Result into posts and post_observations."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.collect import ids, parse as parse_module, writers
from core.collect.parse import ingest
from core.collect.socialcrawl_client import Result, split_vendor_labels
from core.collect.tests.test_parse import FakeGeo, fake_item_id

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FETCHED = datetime(2026, 9, 28, 4, 10, tzinfo=timezone.utc)  # 06:10 SAST, 05:10 WAT, 07:10 EAT
MULTI_PARAMS = {"query": "invented", "since": "2026-09-21", "platforms": "instagram,twitter,reddit"}
TOP_PARAMS = {"query": "matatu", "country": "KE", "sort_by": "date-posted"}


def live(key):
    return json.loads((FIXTURES / "parse_live.json").read_text(encoding="utf-8"))[key]


def result(route, body, status="ok"):
    """A Result as SocialCrawlClient hands it back: vendor labels already split onto each row."""
    if body is None:
        return Result(status, route)
    stored, items, labels = split_vendor_labels(body)
    return Result(status, route, "h", 200, 1, 1, status == "cached", stored, items, labels)


class Job:
    def result(self):
        return []


class Table:
    schema = []


class FakeBQ:
    """Plays the posts MERGE on a dict keyed by post_id and records load jobs."""

    def __init__(self, posts=None):
        self.posts = dict(posts or {})
        self.queries, self.loads = [], []

    def query(self, sql, job_config=None):
        assert sql.lstrip().upper().startswith("MERGE") and "posts" in sql, sql
        self.queries.append(sql)
        params = {p.name: p for p in job_config.query_parameters}
        for struct in params["rows"].values:
            row = struct.struct_values
            if row["post_id"] not in self.posts:
                self.posts[row["post_id"]] = dict(row)
            elif any(row[k] is not None for k in ("views", "likes", "comments", "shares")):
                self.posts[row["post_id"]].update({k: row[k] for k in writers.METRICS})
        return Job()

    def load_table_from_json(self, rows, table_id, job_config=None):
        self.loads.append((table_id, [dict(r) for r in rows]))
        return Job()

    def get_table(self, table_id):
        return Table()

    def loaded(self, name):
        return [r for table_id, rows in self.loads if table_id.endswith("." + name) for r in rows]


def run(bq, res, market, **kw):
    kw = {"params": MULTI_PARAMS, "run_id": "brief1", "fetched_at": FETCHED, "item_id_fn": fake_item_id,
          "geo_fn": FakeGeo(), **kw}
    return ingest(bq, res, market, **kw)


def test_search_multi_confirm_finds_become_posts_and_confirm_observations():
    bq = FakeBQ()
    out = run(bq, result("search/multi", live("search_multi")), "ZA", seed_key="invented")
    expected = [ids.post_id("instagram", "3700000000000000001"), ids.post_id("twitter", "1972000000000000001"),
                ids.post_id("reddit", "t3_synth1")]
    assert [p["post_id"] for p in out["posts"]] == expected
    assert sorted(bq.posts) == sorted(expected)
    assert {p["platform"] for p in bq.posts.values()} == {"instagram", "twitter", "reddit"}
    assert all(p["endpoint"] == "search/multi" and p["run_id"] == "brief1" for p in out["posts"])
    obs = bq.loaded("post_observations")
    assert obs == out["observations"]
    assert [o["post_id"] for o in obs] == expected
    assert {(o["lane"], o["lane_class"], o["series"], o["market"], o["seed_key"], o["run_id"]) for o in obs} == {
        ("confirm", "search_presence", "search", "ZA", "invented", "brief1")}
    assert {(o["source_market"], o["source_region"]) for o in obs} == {(None, None)}
    assert {o["protocol"] for o in obs} == {"search/multi?platforms=instagram,twitter,reddit"}
    assert {(o["observed_at"], o["observed_date"]) for o in obs} == {("2026-09-28T04:10:00+00:00", "2026-09-28")}
    assert len(bq.queries) == 1


def test_tiktok_search_top_confirm_finds_become_posts_and_confirm_observations():
    bq = FakeBQ()
    geo = FakeGeo()
    out = run(bq, result("tiktok/search/top", live("search_top"), status="cached"), "KE", params=TOP_PARAMS,
              geo_fn=geo)
    pid = ids.post_id("tiktok", "7500000000000000003")
    assert [p["post_id"] for p in out["posts"]] == [pid]
    assert bq.posts[pid]["platform"] == "tiktok"
    assert bq.posts[pid]["geo_market"] == "KE"
    assert geo.calls and geo.calls[0]["market"] == "KE"
    [obs] = bq.loaded("post_observations")
    assert (obs["post_id"], obs["platform"], obs["market"], obs["lane"], obs["lane_class"]) == (
        pid, "tiktok", "KE", "confirm", "search_presence")
    assert obs["protocol"] == "tiktok/search/top?country=KE&sort_by=date-posted"
    assert obs["observed_date"] == "2026-09-28"
    assert (obs["source_market"], obs["source_region"]) == ("KE", "KE")


def test_a_confirm_find_already_collected_is_not_duplicated():
    pid = ids.post_id("tiktok", "7500000000000000003")
    first = {"post_id": pid, "platform": "tiktok", "endpoint": "tiktok/trending", "run_id": "collect1", "views": 10,
             "likes": 1, "comments": 0, "shares": 0, "engagement": 1}
    bq = FakeBQ(posts={pid: first})
    run(bq, result("tiktok/search/top", live("search_top")), "KE", params=TOP_PARAMS)
    assert list(bq.posts) == [pid]
    assert (bq.posts[pid]["endpoint"], bq.posts[pid]["run_id"]) == ("tiktok/trending", "collect1")
    assert bq.posts[pid]["views"] == 2500
    assert len(bq.loaded("post_observations")) == 1


@pytest.mark.parametrize("status", ["error", "cap_reached", "forbidden", "balance_floor", "insufficient_credits",
                                    "not_in_replay", "refunded"])
def test_a_failed_result_writes_nothing(status):
    bq = FakeBQ()
    assert run(bq, result("search/multi", None, status=status), "ZA") == {"posts": [], "observations": []}
    assert (bq.queries, bq.loads) == ([], [])


def test_an_ok_result_with_a_failed_body_writes_nothing():
    bq = FakeBQ()
    assert run(bq, result("search/multi", {"success": False, "error": {"code": "X"}}), "ZA") == {
        "posts": [], "observations": []}
    assert (bq.queries, bq.loads) == ([], [])


def test_an_empty_result_writes_nothing():
    bq = FakeBQ()
    assert run(bq, result("search/multi", live("search_multi_estimate"), status="empty"), "ZA") == {
        "posts": [], "observations": []}
    assert (bq.queries, bq.loads) == ([], [])


@pytest.mark.parametrize("market", ["GLOBAL", "global", "US", "", None])
def test_global_or_unknown_market_is_refused_before_any_write(market):
    bq = FakeBQ()
    with pytest.raises(ValueError):
        run(bq, result("search/multi", live("search_multi")), market)
    with pytest.raises(ValueError):
        run(bq, result("search/multi", None, status="error"), market)
    assert (bq.queries, bq.loads) == ([], [])


def test_lowercase_market_is_read_as_the_market():
    bq = FakeBQ()
    out = run(bq, result("search/multi", live("search_multi")), "za")
    assert {o["market"] for o in out["observations"]} == {"ZA"}


def test_item_ids_and_geo_come_from_the_injected_functions(monkeypatch):
    seen = {}
    real = parse_module.parse

    def spy(*args, **kw):
        seen.update(kw)
        return real(*args, **kw)

    monkeypatch.setattr(parse_module, "parse", spy)

    def item_id_fn(kind, raw, platform):
        return f"injected|{kind}|{raw}"

    geo = FakeGeo()
    run(FakeBQ(), result("search/multi", live("search_multi")), "ZA", item_id_fn=item_id_fn, geo_fn=geo)
    assert seen["item_id_fn"] is item_id_fn
    assert seen["geo_fn"] is geo
    assert (seen["lane"], seen["seed_key"]) == ("confirm", None)
    assert len(geo.calls) == 3


def test_a_route_outside_the_search_family_is_refused():
    bq = FakeBQ()
    with pytest.raises(ValueError):
        run(bq, result("tiktok/trending", live("tiktok_trending")), "ZA", params={"region": "ZA", "feed": "local"})
    assert (bq.queries, bq.loads) == ([], [])


# A confirm find is linked to the candidate it was searched for (seed_key, the candidate's item id) when the post
# itself carries that item, so the next evidence read can cite it. Detect itemises only posts first sighted on its
# own day, so before this a find ingested after detect had no post_items row and no later read ever saw it.

class LinkBQ(FakeBQ):
    """FakeBQ that also plays the post_items insert, keeping one row per post and item."""

    def __init__(self, posts=None):
        super().__init__(posts)
        self.post_items = set()

    def query(self, sql, job_config=None):
        if sql.lstrip().upper().startswith("INSERT INTO") and "post_items" in sql:
            assert "NOT EXISTS" in sql
            self.queries.append(sql)
            for struct in {p.name: p for p in job_config.query_parameters}["rows"].values:
                row = struct.struct_values
                self.post_items.add((row["post_id"], row["item_id"], row["via"]))
            return Job()
        return super().query(sql, job_config)


def _tagged_find(tag):
    body = live("search_top")
    body = json.loads(json.dumps(body))
    for item in body["data"]["items"]:
        item["post"]["content"]["text"] = f"Weekend plans #{tag}"
        item["post"]["content"]["hashtags"] = [tag]
    return body


def test_a_confirm_find_carrying_the_candidate_is_linked_to_it():
    from core.detect import items

    candidate = items.item_id("hashtag", "matatu")
    bq = LinkBQ()
    out = run(bq, result("tiktok/search/top", _tagged_find("matatu")), "KE", params=TOP_PARAMS, seed_key=candidate)
    [post] = out["posts"]
    assert "matatu" in [str(h).lstrip("#").lower() for h in post["hashtags"] or []]
    assert bq.post_items == {(post["post_id"], candidate, "hashtag")}


def test_a_confirm_find_without_the_candidate_is_not_linked():
    from core.detect import items

    bq = LinkBQ()
    run(bq, result("tiktok/search/top", _tagged_find("somethingelse")), "KE", params=TOP_PARAMS,
        seed_key=items.item_id("hashtag", "matatu"))
    assert bq.post_items == set()
    run(bq, result("tiktok/search/top", _tagged_find("matatu")), "KE", params=TOP_PARAMS, seed_key=None)
    assert bq.post_items == set()
