import copy
import json
from datetime import date
from pathlib import Path

import pytest

from core.collect import parse as parser
from core.collect.socialcrawl_client import quote_for
from core.collect.tests.test_parse import FakeGeo, fake_item_id
from core.collect.tests.test_socialcrawl_client import FakeHTTP, NOW, make, paid


FIXTURE = json.loads((Path(__file__).parent / "fixtures/location_contracts.json").read_text(encoding="utf-8"))


def parsed(route, params, body, geo=None):
    return parser.parse_with_creators(route, params, "NG", body, NOW, "test-location",
                                      item_id_fn=fake_item_id, geo_fn=geo or FakeGeo(), lane="expansion")


def test_recorded_youtube_advanced_keeps_null_engagement_and_no_request_geo():
    record = FIXTURE["youtube"]
    body = {"success": True, "data": {"items": [record["first_item"], record["second_item"]]}}
    geo = FakeGeo()
    out = parsed("youtube/search/advanced", {"region": "NG", "location": "reviewed", "location_radius": "1km"}, body, geo)
    assert [p["native_id"] for p in out["posts"]] == ["HOAVuKjZxrw", "ZrAffIkFGFg"]
    assert all(p["views"] is None and p["engagement"] is None and p["duration_s"] is None for p in out["posts"])
    assert all(p["geo_market"] is None for p in out["posts"])
    assert all(o["source_market"] is None for o in out["observations"])
    assert all(g["ext_region"] is None and g["home_market"] is None for g in geo.calls)


def test_tiktok_place_supplier_projection_never_promotes_proxy_region():
    body = {"success": True, "data": {"items": FIXTURE["tiktok_place"]["sample_rows"]}}
    out = parsed("tiktok/location/posts", {"location_id": "fixture-only", "region": "NG"}, body)
    assert len(out["posts"]) == 2
    assert all(p["geo_market"] is None for p in out["posts"])
    assert all(o["source_market"] is None for o in out["observations"])
    assert quote_for("tiktok/location/posts", "GET", {"location_id": "fixture-only", "region": "NG"}) == 1


def test_tiktok_country_supplier_projection_is_creator_metadata_only():
    from core.collect.location_sources import profile_country

    body = {"success": True, "data": {"author": copy.deepcopy(FIXTURE["tiktok_region"]["author"])}}
    assert profile_country(body, "stoolpresidente") == "US"
    body["data"]["author"]["location"] = None
    assert profile_country(body, "stoolpresidente") is None
    assert profile_country(body, "other") is None


def test_location_plan_missing_reviewed_inputs_makes_no_calls():
    from core.collect.location_sources import plan

    assert plan(date(2026, 10, 7), {"markets": {"ng": {}}}) == ([], [])
    calls, held = plan(date(2026, 10, 7), {"markets": {"ng": {"location_collection": {
        "youtube": [{"query": "BBNaija"}], "tiktok_places": [""]}}}})
    assert calls == []
    assert {row["reason"] for row in held} == {"missing_reviewed_geo", "invalid_place_id"}


def test_discovery_projection_removes_generated_text_before_raw(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "fake")
    answer = "GENERATED TEXT MUST NEVER BE STORED"
    http = FakeHTTP({"twitter/ai-search": (200, {"success": True, "credits_used": 5,
        "request_id": "fake-discovery", "data": {"answer": answer, "sources": [
            {"url": "https://x.com/ARISEtv/status/2104603248652882043", "title": answer},
            {"url": "https://twitter.com/ARISEtv/status/2104603248652882043"},
            {"url": "https://x.com/ARISEtv"}, {"url": "https://evil.test/a/status/22"}],
        "tool_calls_count": 1}})})
    client = make(http=http)
    params = {"query": "Find posts", "from_handles": "ARISEtv", "from_date": "2026-09-28", "to_date": "2026-09-29"}
    assert client.call("twitter/ai-search", params).status == "forbidden"
    result = client.discover_x(params, market="NG")
    assert result.status == "ok"
    assert result.body["data"]["sources"] == [{"url": "https://x.com/ARISEtv/status/2104603248652882043"}]
    assert answer not in json.dumps([client.raw.rows, result.body, result.items, client.ledger.rows])
    assert paid(client)[0]["credits_charged"] == 5
    assert paid(client)[0]["posts_new"] == 0


def test_discovery_lookup_validates_native_author_and_id(monkeypatch):
    from core.collect.x_discovery import verified_lookup

    record = FIXTURE["twitter"]
    body = {"success": True, "data": {"post": copy.deepcopy(record["single_post"]), "computed": record["single_computed"]}}
    url = "https://x.com/ARISEtv/status/2104603248652882043"
    assert verified_lookup(body, url, ("ARISEtv",)) is not None
    body["data"]["post"]["author"]["username"] = "other"
    assert verified_lookup(body, url, ("ARISEtv",)) is None
    body["data"]["post"]["author"]["username"] = "ARISEtv"
    body["data"]["post"]["id"] = "999"
    assert verified_lookup(body, url, ("ARISEtv",)) is None


def test_recorded_native_tweet_has_real_rows():
    record = FIXTURE["twitter"]
    body = {"success": True, "data": {"post": record["single_post"], "computed": record["single_computed"]}}
    out = parsed("twitter/tweet", {"url": record["single_post"]["url"]}, body)
    assert out["posts"][0]["native_id"] == "2104603248652882043"
    assert out["posts"][0]["creator_id"] == "ARISEtv"
    assert out["observations"][0]["views"] == 56456


def test_discovery_holds_five_credits_before_http(monkeypatch):
    from core.collect.tests.test_socialcrawl_client import NoHTTP, spent

    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "fake")
    client = make(http=NoHTTP())
    spent(client.ledger, "collect", client.share_caps["collect"] - 4)
    result = client.discover_x({"query": "posts", "from_handles": "ARISEtv",
                                "from_date": "2026-09-27", "to_date": "2026-09-28"})
    assert result.status == "cap_reached" and result.credits_quoted == 5
    assert client.raw.rows == []


@pytest.mark.parametrize("url", ["https://x.com/a/status/no", "https://x.com/a/status/1/extra",
    "http://x.com/a/status/1", "https://x.com.evil.test/a/status/1", "https://x.com/a", "bad"])
def test_discovery_refuses_nonstatus_sources(url):
    from core.collect.x_discovery import project_discovery

    assert project_discovery({"data": {"answer": "discard", "sources": [{"url": url}]}})["data"]["sources"] == []


def test_failed_lookup_does_not_create_evidence_and_next_lookup_runs(monkeypatch):
    from core.collect.x_discovery import discover_and_lookup

    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "fake")
    native = FIXTURE["twitter"]
    discovery = {"success": True, "data": {"answer": "discard", "sources": [
        {"url": "https://x.com/ARISEtv/status/1"}, {"url": native["single_post"]["url"]}]}}
    http = FakeHTTP({"twitter/ai-search": (200, discovery), "twitter/tweet": [
        TimeoutError("fake timeout"), (200, {"success": True, "data": {"post": native["single_post"],
                                                                   "computed": native["single_computed"]}})]})
    client = make(http=http)
    results, records = discover_and_lookup(client, {"query": "posts", "from_handles": "ARISEtv",
        "from_date": "2026-09-27", "to_date": "2026-09-29"}, market="NG")
    assert len(results) == 1 and results[0].body["data"]["post"]["id"] == "2104603248652882043"
    assert [r["status"] for r in records] == ["ok", "lookup_refused", "ok"]
    assert sum(r["credits_charged"] for r in paid(client)) == 7


def test_profile_country_parser_has_no_posts_or_observations():
    body = {"success": True, "data": {"author": FIXTURE["tiktok_region"]["author"]}}
    out = parser.parse_with_creators("tiktok/profile", {"handle": "stoolpresidente"}, "NG", body,
        NOW, "profile", item_id_fn=fake_item_id, geo_fn=FakeGeo(), lane="panel")
    assert out["posts"] == [] and out["observations"] == [] and out["counters"] == []
    assert out["creators"][0]["profile_location"] == "US"


def test_location_parser_failure_is_isolated(monkeypatch):
    from core.collect import job
    from core.collect.socialcrawl_client import Result

    class Client:
        def call(self, route, *args, **kwargs):
            body = {"success": True, "data": {"items": []}}
            return Result("ok", route, body=body, credits_charged=1)

    real = job.parse_with_creators

    def broken(route, *args, **kwargs):
        if route == "youtube/search/advanced":
            raise ValueError("unreadable response")
        return real(route, *args, **kwargs)

    monkeypatch.setattr(job, "parse_with_creators", broken)
    run = job.Collected("location-failure")
    runner = job._Runner(Client(), run, fake_item_id, FakeGeo(), lambda: NOW, job.Budget(), {}, date(2026, 9, 28))
    runner.calls([job.Call("C1", "youtube/search/advanced", {"query": "posts"}, "NG", "expansion"),
                  job.Call("C1", "tiktok/location/posts", {"location_id": "fixture-only"}, "NG", "expansion")], "NG")
    assert [r["status"] for r in run.records] == ["unparsable", "ok"]
    assert run.stopped is None and run.credits == 2


def test_collect_consumes_explicit_profile_inputs_without_location_evidence(monkeypatch):
    from core.collect import job
    from core.collect.socialcrawl_client import Result

    class Client:
        def call(self, route, *args, **kwargs):
            assert route == "tiktok/profile"
            return Result("ok", route, body={"success": True, "data": {"author": FIXTURE["tiktok_region"]["author"]}},
                          credits_charged=1)

    config = job.load_config()
    config["markets"]["ng"]["location_collection"] = {"tiktok_profiles": ["stoolpresidente"]}
    monkeypatch.setattr(job, "drive", lambda *args, **kwargs: [])
    monkeypatch.setattr(job, "_curated_limit", lambda *args, **kwargs: 0)
    run = job.collect(Client(), date(2026, 9, 28), "profile", item_id_fn=fake_item_id, geo_fn=FakeGeo(),
                      clock=lambda: NOW, config=config)
    assert run.credits == 1 and len(run.creators) == 1
    assert run.posts == [] and run.observations == []


def test_offline_plan_includes_explicit_profile_input():
    from core.collect import job

    config = job.load_config()
    config["markets"]["ng"]["location_collection"] = {"tiktok_profiles": ["stoolpresidente"]}
    out = job.plan(date(2026, 9, 28), config=config, include_curated=False, reels={})
    assert any(c.route == "tiktok/profile" for c in out["calls"]["NG"])


@pytest.mark.parametrize("route", ["youtube/search/advanced", "tiktok/location/posts"])
def test_location_empty_items_differ_from_invalid_body(route):
    assert parsed(route, {}, {"success": True, "data": {"items": []}})["posts"] == []
    with pytest.raises(ValueError, match="items"):
        parsed(route, {}, {"success": True, "data": {"unexpected": []}})


def test_recorded_native_tweet_accepts_canonical_handle_case_difference():
    from core.collect.x_discovery import verified_lookup

    record = FIXTURE["twitter"]
    body = {"success": True, "data": {"post": copy.deepcopy(record["single_post"]), "computed": record["single_computed"]}}
    url = "https://x.com/arisetv/status/2104603248652882043"
    assert verified_lookup(body, url, ("arisetv",)) is body
    assert parsed("twitter/tweet", {"url": url}, body)["posts"][0]["creator_id"] == "ARISEtv"
    body["data"]["post"]["author"]["username"] = "other"
    assert verified_lookup(body, url, ("arisetv",)) is None


def test_discovery_rejects_recorded_tweet_outside_requested_window(monkeypatch):
    from core.collect.x_discovery import discover_and_lookup

    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "fake")
    native = FIXTURE["twitter"]
    http = FakeHTTP({"twitter/ai-search": (200, {"success": True, "data": {"sources": [
        {"url": native["single_post"]["url"]}]}}), "twitter/tweet": (200, {"success": True,
        "data": {"post": native["single_post"], "computed": native["single_computed"]}})})
    results, records = discover_and_lookup(make(http=http), {"query": "posts", "from_handles": "ARISEtv",
        "from_date": "2026-10-06", "to_date": "2026-10-07"}, market="NG")
    assert results == []
    assert records[-1]["status"] == "lookup_refused"


@pytest.mark.parametrize("published_at", [None, "invalid", "2026-09-28", "2026-10-07T00:00:00Z"])
def test_discovery_holds_unusable_native_dates_and_continues(monkeypatch, published_at):
    from core.collect.x_discovery import discover_and_lookup

    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "fake")
    native = FIXTURE["twitter"]
    bad = copy.deepcopy(native["single_post"])
    bad.update(id="1", url="https://x.com/arisetv/status/1", published_at=published_at)
    http = FakeHTTP({"twitter/ai-search": (200, {"success": True, "data": {"sources": [
        {"url": bad["url"]}, {"url": native["single_post"]["url"]}]}}), "twitter/tweet": [
        (200, {"success": True, "data": {"post": bad}}),
        (200, {"success": True, "data": {"post": native["single_post"], "computed": native["single_computed"]}})]})
    results, records = discover_and_lookup(make(http=http), {"query": "posts", "from_handles": "arisetv",
        "from_date": "2026-09-27", "to_date": "2026-09-29"}, market="NG")
    assert [r.body["data"]["post"]["id"] for r in results] == ["2104603248652882043"]
    assert [r["status"] for r in records] == ["ok", "lookup_refused", "ok"]


@pytest.mark.parametrize("bounds", [{}, {"from_date": None, "to_date": "2026-09-29"},
    {"from_date": "2026-09-27", "to_date": "invalid"},
    {"from_date": "2026-09-29", "to_date": "2026-09-27"}])
def test_discovery_holds_missing_or_invalid_window_before_http(bounds):
    from core.collect.tests.test_socialcrawl_client import NoHTTP
    from core.collect.x_discovery import discover_and_lookup

    results, records = discover_and_lookup(make(http=NoHTTP()), {"query": "posts", "from_handles": "ARISEtv", **bounds}, market="NG")
    assert results == [] and records[0]["status"] == "forbidden"


@pytest.mark.parametrize("start,end", [("2026-09-27", "2026-09-28"), ("2026-09-28", "2026-09-29"),
                                      ("2026-09-28", "2026-09-28")])
def test_discovery_native_date_window_includes_both_bounds(monkeypatch, start, end):
    from core.collect.x_discovery import discover_and_lookup

    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "fake")
    native = FIXTURE["twitter"]
    http = FakeHTTP({"twitter/ai-search": (200, {"success": True, "data": {"sources": [
        {"url": native["single_post"]["url"]}]}}), "twitter/tweet": (200, {"success": True,
        "data": {"post": native["single_post"], "computed": native["single_computed"]}})})
    results, _ = discover_and_lookup(make(http=http), {"query": "posts", "from_handles": "ARISEtv",
        "from_date": start, "to_date": end}, market="NG")
    assert len(results) == 1
