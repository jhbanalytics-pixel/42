import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.collect import job, location_sources, parse
from core.collect.socialcrawl_client import quote_for
from core.collect.tests.test_parse import fake_item_id
from core.collect.tests.test_socialcrawl_client import FakeHTTP, make
from core.detect.geo import geo_for_post


NOW = datetime(2026, 10, 7, 3, tzinfo=timezone.utc)
FIXTURE = json.loads((Path(__file__).parent / "fixtures/supplier_profile_20261007.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("raw,expected", [("ZA", "ZA"), ("NG", "NG"), ("KE", "KE"), ("US", "US"),
    ("South Africa", "ZA"), ("Nigeria", "NG"), ("Kenya", "KE"), ("United States", "US"),
    ("XX", None), ("Nowhere", None), ("", None), (None, None), ({"country": "ZA"}, None)])
def test_explicit_profile_country_normalization(raw, expected):
    assert location_sources.country_code(raw) == expected


def test_recorded_profile_route_metadata_does_not_create_a_post():
    body = FIXTURE["tiktok_profile"]
    out = parse.parse_with_creators("tiktok/profile", {"handle": "charlidamelio"}, "NG", body, NOW,
        "country", item_id_fn=fake_item_id, geo_fn=lambda *a, **kw: pytest.fail("profile is not a post"), lane="panel")
    assert out["posts"] == [] and out["observations"] == []
    assert out["creators"][0]["home_market"] == "US"
    assert out["creators"][0]["followers"] == body["data"]["author"]["followers"]
    assert quote_for("tiktok/profile", "GET", {"handle": "charlidamelio"}) == 1
    calls, _ = location_sources.plan(date(2026, 10, 7), {"markets": {"ng": {
        "location_collection": {"tiktok_profiles": ["charlidamelio"]}}}})
    assert calls[0]["route"] == "tiktok/profile"


def test_about_uses_explicit_country_and_checks_actual_owner():
    body = {"success": True, "data": {"author": {"username": "instagram", "ext": {"country": "United States"}}}}
    assert location_sources.profile_country(body, "instagram", platform="instagram") == "US"
    assert location_sources.profile_country(body, "other", platform="instagram") is None
    assert quote_for("instagram/profile/about", "GET", {"handle": "instagram"}) == 1


def test_recorded_about_country_name_is_account_metadata():
    out = parse.parse_with_creators("instagram/profile/about", {"handle": "instagram"}, "NG",
        FIXTURE["instagram_about"], NOW, "about", item_id_fn=fake_item_id,
        geo_fn=lambda *a, **kw: pytest.fail("About is not a post"), lane="panel")
    assert out["creators"][0]["home_market"] == "US"
    assert out["posts"] == [] and out["observations"] == []


@pytest.mark.parametrize("platform,country,expected", [("tiktok", "ZA", "ZA"), ("tiktok", "US", "US"),
    ("instagram", "Kenya", "KE"), ("instagram", "United States", "US")])
def test_cached_country_uses_existing_confidence_before_local_caption(platform, country, expected):
    cache = location_sources.ProfileCache()
    cache.remember(platform, "creator", country)
    post = {"geo_market": "NG", "geo_confidence": 0.7, "geo_source": "place_mention"}
    cache.bind(post, platform, "NG", "creator", {"ext_region": None, "home_market": None,
        "profile_location": None, "text": "Lagos celebration", "language": "en"})
    cache.apply(geo_for_post)
    assert (post["geo_market"], post["geo_confidence"], post["geo_source"]) == (expected, 0.8, "home_market")


def test_ext_region_keeps_its_existing_precedence():
    cache = location_sources.ProfileCache()
    cache.remember("tiktok", "creator", "US")
    post = {}
    cache.bind(post, "tiktok", "NG", "creator", {"ext_region": "NG", "home_market": None,
        "profile_location": None, "text": "Lagos", "language": None})
    cache.apply(geo_for_post)
    assert (post["geo_market"], post["geo_confidence"], post["geo_source"]) == ("NG", 0.9, "ext_region")


def test_empty_or_bad_refresh_preserves_cached_metadata():
    cache = location_sources.ProfileCache()
    cache.seed([{"platform": "instagram", "handle": "Creator", "country": "US", "country_source": "instagram/profile/about"}])
    for bad in (None, "", "Nowhere", {"country": "ZA"}):
        cache.remember("instagram", "creator", bad)
    assert cache.country("instagram", "CREATOR") == "US"
    assert cache.country("tiktok", "creator") is None


def test_bare_home_market_or_panel_market_is_not_supplier_cache_proof():
    cache = location_sources.ProfileCache()
    cache.seed([{"platform": "instagram", "handle": "creator", "home_market": "NG"},
                {"platform": "tiktok", "handle": "creator", "country": "NG", "country_source": "panel_market"}])
    assert cache.country("instagram", "creator") is None and cache.country("tiktok", "creator") is None


def test_later_empty_profile_does_not_erase_source_qualified_reel_country():
    cache = location_sources.ProfileCache()
    cache.seed([{"platform": "instagram", "handle": "creator", "country": None, "country_source": "instagram/profile/about"},
                {"platform": "instagram", "handle": "creator", "country": "United States", "country_source": "instagram/search/reels"}])
    assert cache.country("instagram", "creator") == "US"


def test_country_name_normalization_does_not_change_x_free_text():
    cache = location_sources.ProfileCache()
    signals = {"ext_region": None, "home_market": "United States", "profile_location": None,
               "text": "Lagos", "language": None}
    assert cache.signals("twitter", "creator", signals) == signals


def test_malformed_tiktok_country_name_is_not_treated_as_a_code():
    cache = location_sources.ProfileCache()
    cache.seed([{"platform": "tiktok", "handle": "creator", "country": "United States", "country_source": "tiktok/profile"}])
    assert cache.country("tiktok", "creator") is None


def test_country_capture_runs_once_per_account_and_reuses_existing_resolver(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "fake")
    posts = [{"post": {"id": str(n), "url": f"https://www.tiktok.com/@charlidamelio/video/{n}",
        "author": {"username": "charlidamelio"}, "content": {"text": "Lagos celebration"}}} for n in (1, 2)]
    http = FakeHTTP({"tiktok/profile/videos": (200, {"success": True, "data": {"items": posts}}),
                     "tiktok/profile": (200, FIXTURE["tiktok_profile"])})
    c = make(http=http)
    c.clock = lambda: NOW

    def drive(execute, *args, **kwargs):
        execute({"NG": [job.Call("10", "tiktok/profile/videos", {"handle": "charlidamelio"}, "NG", "watchlist")]})
        return []

    monkeypatch.setattr(job, "drive", drive)
    monkeypatch.setattr(job, "_curated_limit", lambda *a, **kw: 0)
    run = job.collect(c, NOW.date(), "country", item_id_fn=fake_item_id, geo_fn=geo_for_post,
        clock=lambda: NOW, country_profiles=lambda keys, **kw: [])
    assert http.routes().count("tiktok/profile") == 1
    assert all(p["geo_market"] == "US" and p["geo_confidence"] == 0.8 for p in run.posts)
    assert run.credits == 2


def test_persistent_country_avoids_another_profile_call(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "fake")
    http = FakeHTTP({"tiktok/profile/videos": (200, {"success": True, "data": {"items": [{"post": {
        "id": "1", "url": "https://www.tiktok.com/@charlidamelio/video/1", "author": {"username": "charlidamelio"},
        "content": {"text": "Lagos"}}}]}})})
    c = make(http=http)
    c.clock = lambda: NOW
    monkeypatch.setattr(job, "drive", lambda execute, *a, **kw: execute({"NG": [
        job.Call("10", "tiktok/profile/videos", {"handle": "charlidamelio"}, "NG", "watchlist")]}))
    monkeypatch.setattr(job, "_curated_limit", lambda *a, **kw: 0)
    run = job.collect(c, NOW.date(), "country-cached", item_id_fn=fake_item_id, geo_fn=geo_for_post,
        clock=lambda: NOW, country_profiles=lambda keys, **kw: [{"platform": "tiktok", "handle": "charlidamelio", "country": "US", "country_source": "tiktok/profile"}])
    assert "tiktok/profile" not in http.routes() and run.posts[0]["geo_market"] == "US"


def test_reel_creator_country_avoids_duplicate_about_call(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "fake")
    http = FakeHTTP({"instagram/search/reels": (200, {"success": True, "data": {"items": [{"post": {
        "id": "1", "url": "https://www.instagram.com/reel/one/", "author": {"username": "instagram"},
        "content": {"text": "Lagos"}, "ext": {"author_country": "Kenya"}}}]}})})
    c = make(http=http)
    c.clock = lambda: NOW
    monkeypatch.setattr(job, "drive", lambda execute, *a, **kw: execute({"NG": [job.Call("14i", "instagram/search/reels",
        {"query": "weekend", "include": "creator"}, "NG", "expansion")]}))
    monkeypatch.setattr(job, "_curated_limit", lambda *a, **kw: 0)
    run = job.collect(c, NOW.date(), "country-fill", item_id_fn=fake_item_id, geo_fn=geo_for_post,
        clock=lambda: NOW, country_profiles=lambda keys, **kw: [])
    assert "instagram/profile/about" not in http.routes()
    assert run.posts[0]["geo_market"] == "KE" and any(r.get("home_market") == "KE" for r in run.creators)


def test_owner_hash_is_part_of_persistent_country_authority():
    from core.collect import writers
    from core.collect.socialcrawl_client import params_hash

    class BQ:
        def query(self, sql, job_config, **kwargs):
            assert job_config.maximum_bytes_billed == 64 * 1024 ** 2
            if "SELECT DISTINCT DATE(fetched_at)" in sql:
                return type("Result", (), {"result": lambda self, **kw: [{"day": NOW.date()}]})()
            assert "JSON_VALUE(body, '$.data.author.username')" in sql
            assert "IN UNNEST(@profile_requests)" in sql
            values = {p.name: p for p in job_config.query_parameters}
            expected = "tiktok/profile:" + params_hash("GET", {"handle": "creator"}) + ":creator"
            assert expected in values["profile_requests"].values
            assert "home_market" not in sql
            return type("Result", (), {"result": lambda self, **kw: []})()

    assert list(writers.read_profile_countries(BQ(), [("tiktok", "Creator")])) == []


def test_malformed_raw_country_does_not_complete_the_lookup_cache():
    cache = location_sources.ProfileCache()
    cache.seed([{"platform": "instagram", "handle": "creator", "country": None,
                 "country_type": "object", "country_source": "instagram/profile/about"}])
    assert ("instagram", "creator") not in cache.attempted


def test_empty_country_receipt_does_not_complete_the_country_lookup():
    cache = location_sources.ProfileCache()
    cache.bind({}, "instagram", "NG", "creator", {"home_market": None})
    cache.seed([{"platform": "instagram", "handle": "creator", "country": None,
                 "country_type": "null", "country_source": "instagram/profile/about"}])
    assert len(cache.needed()) == 1 and cache.country("instagram", "creator") is None


def test_partitioned_reader_retains_older_country_and_stops_when_resolved():
    from core.collect import writers

    class BQ:
        def __init__(self):
            self.calls = 0

        def query(self, sql, job_config, **kwargs):
            self.calls += 1
            assert job_config.maximum_bytes_billed == 64 * 1024 ** 2
            if self.calls == 1:
                rows = [{"day": NOW.date() - timedelta(days=n)} for n in range(3)]
            else:
                assert self.calls <= 3
                rows = [{"platform": "instagram", "handle": "creator", "country_source": "instagram/profile/about",
                    "country": None if self.calls == 2 else "United States", "country_type": "null" if self.calls == 2 else "string",
                    "recognised": self.calls == 3}]
                assert "fetched_at >= @start AND fetched_at < @end" in sql
            return type("Result", (), {"result": lambda self, **kw: rows})()

    bq = BQ()
    cache = location_sources.ProfileCache()
    cache.seed(writers.read_profile_countries(bq, [("instagram", "creator")], timeout=60))
    assert cache.country("instagram", "creator") == "US" and bq.calls == 3


def test_existing_creator_country_survives_an_empty_profile_merge():
    from core.collect import writers
    from core.collect.tests.test_job import FakeBQ

    bq = FakeBQ()
    row = {c: None for c in writers.CREATOR_TYPES}
    row.update(platform="instagram", creator_id="creator", handle="creator", home_market="US",
               first_seen=NOW.isoformat(), last_seen=NOW.isoformat())
    writers.merge_creators(bq, [row])
    writers.merge_creators(bq, [{**row, "home_market": None}])
    assert bq.creators[("instagram", "creator")]["home_market"] == "US"


def test_only_remaining_collect_room_is_available_to_country_capture(monkeypatch):
    from core.collect.tests.test_socialcrawl_client import NoHTTP

    cache = location_sources.ProfileCache()
    cache.bind({}, "tiktok", "NG", "creator", {"home_market": None})
    run = job.Collected("country-room")
    run.credits, run.local_credits, run.trends_credits = 1984, 1, 15
    c = make(http=NoHTTP())
    runner = job._Runner(c, run, fake_item_id, geo_for_post, lambda: NOW, job.Budget(), {}, NOW.date(), profiles=cache)
    job.country_phase(run, runner, lambda keys: [], 2000)
    assert run.credits == 1984 and run.records[-1]["status"] == "over_share"


def test_unreadable_persistent_cache_holds_new_country_calls():
    from core.collect.tests.test_socialcrawl_client import NoHTTP

    cache = location_sources.ProfileCache()
    cache.bind({}, "instagram", "NG", "creator", {"home_market": None})
    run = job.Collected("country-cache-unreadable")
    runner = job._Runner(make(http=NoHTTP()), run, fake_item_id, geo_for_post, lambda: NOW, job.Budget(), {}, NOW.date(), profiles=cache)
    job.country_phase(run, runner, lambda keys: (_ for _ in ()).throw(RuntimeError("cache unreadable")), 2000)
    assert run.credits == 0 and run.client_calls == []


def test_unknown_charge_failure_keeps_existing_client_hold(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "fake")
    c = make(http=FakeHTTP({"tiktok/profile": TimeoutError("fake timeout")}))
    c.clock = lambda: NOW
    result = c.account_profile("tiktok", "creator", market="NG")
    assert result.status == "error" and result.credits_charged == 1
    assert c.raw.rows[-1]["body"] is None


def test_optional_country_call_does_not_consume_the_write_time_reserve(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "fake")
    c = make(http=FakeHTTP({"tiktok/profile": (200, {"success": True, "data": {"author": {
        "username": "creator", "location": "NG"}}})}))
    c.clock = lambda: NOW
    cache = location_sources.ProfileCache()
    cache.bind({}, "tiktok", "NG", "creator", {"home_market": None})
    run = job.Collected("country-time")
    runner = job._Runner(c, run, fake_item_id, geo_for_post, lambda: NOW, job.Budget(), {}, NOW.date(), profiles=cache)
    runner.country_deadline = NOW + timedelta(seconds=c.timeout)
    job.country_phase(run, runner, lambda keys, **kw: [], 2000)
    assert run.client_calls == [] and run.credits == 0


def test_transient_country_endpoint_failure_does_not_repeat_across_accounts(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "fake")
    c = make(http=FakeHTTP({"tiktok/profile": [TimeoutError("fake timeout"), (200, {"success": True,
        "data": {"author": {"username": "creator2", "location": "NG"}}})],
        "instagram/profile/about": (200, {"success": True, "data": {"author": {
            "username": "instagram", "ext": {"country": "Kenya"}}}})}))
    c.clock = lambda: NOW
    cache = location_sources.ProfileCache()
    for platform, handle in [("tiktok", "creator1"), ("tiktok", "creator2"), ("instagram", "instagram")]:
        cache.bind({}, platform, "NG", handle, {"home_market": None})
    run = job.Collected("country-endpoint")
    runner = job._Runner(c, run, fake_item_id, geo_for_post, lambda: NOW, job.Budget(), {}, NOW.date(), profiles=cache)
    job.country_phase(run, runner, lambda keys, **kw: [], 2000)
    assert c.http.routes().count("tiktok/profile") == 1
    assert c.http.routes().count("instagram/profile/about") == 1 and run.stopped is None
