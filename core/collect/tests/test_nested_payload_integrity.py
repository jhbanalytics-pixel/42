import copy

import pytest

from core.collect import job, writers
from core.collect.tests.test_parse import run
from core.collect.tests.test_socialcrawl_client import FakeHTTP, NOW, SAMPLES, TODAY, make, paid


@pytest.fixture(autouse=True)
def synthetic_key(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "synthetic-nested-payload")


def collect_one(client):
    collected = job.Collected("synthetic-nested-envelope")
    runner = job._Runner(client, collected, job._CountedIds(lambda kind, raw, platform: str(raw)),
                         job.safe_geo(lambda *args, **kwargs: (None, None, None)),
                         lambda: NOW, job.Budget(), None, TODAY)
    calls = [call for call in job.harvest_calls("ZA", TODAY, job.load_config())[0]
             if call.route == "tiktok/trending"]
    runner.calls(calls[:1], "ZA")
    return collected


@pytest.mark.parametrize("bad", ["malformed", None, False, {"post": {"id": "1"}}])
def test_nested_items_failure_preserves_paid_raw_and_failed_health(bad):
    body = {"success": True, "credits_used": 5, "data": {"items": bad}}
    client = make(http=FakeHTTP({"tiktok/trending": (200, body)}))
    collected = collect_one(client)
    assert paid(client)[0]["credits_charged"] == 5
    assert paid(client)[0]["posts_new"] == 0
    assert client.raw.rows[0]["body"] == body
    [record] = collected.records
    assert record["ok"] is False and record["failure"] == "parse"
    assert collected.posts == [] and collected.observations == []
    [health] = writers.health_rows(collected.records, [], {}, collected.run_id)
    assert health["valid"] is False and health["invalid_reason"] == "calls: parse"
    cached = client.call("tiktok/trending", {"region": "ZA", "feed": "local"})
    assert cached.status == "error" and cached.failure == "parse"
    assert cached.cache_hit and cached.credits_charged == 0


@pytest.mark.parametrize("bad", ["malformed", None, False, {"post": {"id": "1"}}])
def test_direct_parser_rejects_nested_items_field(bad):
    with pytest.raises(ValueError, match="items"):
        run("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA",
            {"success": True, "data": {"items": bad}}, pull_seq=1)


@pytest.mark.parametrize("data", [{"items": []}, [], {}])
def test_actual_empty_controls_reach_readable_collection_health(data):
    client = make(http=FakeHTTP({"tiktok/trending": (200, {"success": True, "credits_used": 0, "data": data})}))
    collected = collect_one(client)
    assert collected.records[0]["ok"] is True
    assert collected.records[0]["status"] == "empty"
    assert collected.posts == [] and collected.observations == []


def test_actual_one_post_control_reaches_rows_and_readable_health():
    body = copy.deepcopy(SAMPLES["trending"])
    body["data"]["items"] = body["data"]["items"][:1]
    client = make(http=FakeHTTP({"tiktok/trending": (200, body)}))
    collected = collect_one(client)
    assert collected.records[0]["ok"] is True
    assert len(collected.posts) == len(collected.observations) == 1


def test_counter_numeric_posts_is_a_total_not_a_malformed_post_list():
    body = {"success": True, "credits_used": 1, "data": {"posts": 7}}
    client = make(http=FakeHTTP({"tiktok/hashtag": (200, body)}))
    response = client.call("tiktok/hashtag", {"hashtag": "amapiano"})
    assert response.status == "ok"
    parsed = run("tiktok/hashtag", {"hashtag": "amapiano"}, "ZA", body)
    assert parsed["counters"][0]["value"] == 7


@pytest.mark.parametrize("key", ["posts", "results", "videos", "data"])
def test_rank_alias_row_fields_cannot_hide_scalar_values(key):
    body = {"success": True, "credits_used": 5, "data": {key: "malformed"}}
    client = make(http=FakeHTTP({"tiktok/trending": (200, body)}))
    assert client.call("tiktok/trending", {"region": "ZA", "feed": "local"}).failure == "parse"
    with pytest.raises(ValueError, match=key):
        run("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", body, pull_seq=1)


@pytest.mark.parametrize("bad", ["malformed", None, False, {}])
def test_grouped_search_row_container_is_validated(bad):
    body = {"success": True, "credits_used": 2, "data": {"sources": {"tiktok": {"items": bad}}}}
    client = make(http=FakeHTTP({"search/multi": (200, body)}))
    result = client.call("search/multi", {"query": "dance", "platforms": "tiktok"})
    assert result.status == "error" and result.failure == "parse"
    assert paid(client)[0]["credits_charged"] == result.credits_charged
    with pytest.raises(ValueError, match="items"):
        run("search/multi", {"query": "dance", "platforms": "tiktok"}, "ZA", body, lane="expansion")


def test_grouped_search_and_profile_objects_remain_supported():
    post = copy.deepcopy(SAMPLES["trending"]["data"]["items"][0])
    body = {"success": True, "credits_used": 1, "data": {"sources": {"tiktok": {"items": [post]}}}}
    client = make(http=FakeHTTP({"search/multi": (200, body)}))
    assert client.call("search/multi", {"query": "dance", "platforms": "tiktok"}).status == "ok"
    assert len(run("search/multi", {"query": "dance"}, "ZA", body, lane="expansion")["posts"]) == 1
    profile = {"success": True, "credits_used": 0, "data": {"author": {"username": "example"}}}
    client = make(http=FakeHTTP({"tiktok/profile": (200, profile)}))
    assert client.account_profile("tiktok", "example").status == "ok"
