import copy

import pytest

from core.collect.tests.test_socialcrawl_client import FakeHTTP, SAMPLES, make, paid


@pytest.fixture(autouse=True)
def synthetic_key(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "synthetic-payload-integrity")


@pytest.mark.parametrize("labels", ["unreadable", 1, ["unreadable"]])
def test_malformed_labels_remain_accounted_and_are_not_replayed_as_success(labels):
    body = copy.deepcopy(SAMPLES["trending"])
    body["credits_used"] = 5
    body["data"]["items"][0]["vendor_labels"] = labels
    client = make(http=FakeHTTP({"tiktok/trending": (200, body)}))
    params = {"region": "ZA", "feed": "local"}

    result = client.call("tiktok/trending", params)

    assert result.status == "error" and result.failure == "parse"
    assert result.credits_charged == 5
    assert paid(client)[0]["credits_charged"] == 5
    assert paid(client)[0]["posts_new"] == 0
    assert len(client.raw.rows) == 1
    assert client.raw.rows[0]["body"] == body
    assert result.items == []

    replayed = client.call("tiktok/trending", params)
    assert replayed.status == "error" and replayed.failure == "parse"
    assert replayed.credits_charged == 0
    assert len(client.http.requests) == 2


def test_valid_labels_keep_returned_rows_and_paid_accounting():
    body = copy.deepcopy(SAMPLES["trending"])
    body["credits_used"] = 5
    client = make(http=FakeHTTP({"tiktok/trending": (200, body)}))
    result = client.call("tiktok/trending", {"region": "ZA", "feed": "local"})
    assert result.status == "ok"
    assert result.items and result.vendor_labels
    assert paid(client)[0]["credits_charged"] == 5
    assert paid(client)[0]["posts_new"] == len(result.items)


@pytest.mark.parametrize("data", ["missing", None, "unreadable", 1])
def test_unreadable_data_envelope_is_failed_with_paid_and_cached_receipts(data):
    body = {"success": True, "credits_used": 5}
    if data != "missing":
        body["data"] = data
    client = make(http=FakeHTTP({"tiktok/trending": (200, body)}))
    params = {"region": "ZA", "feed": "local"}
    first = client.call("tiktok/trending", params)
    assert first.status == "error" and first.failure == "parse"
    assert first.credits_charged == paid(client)[0]["credits_charged"] == 5
    assert paid(client)[0]["posts_new"] == 0
    assert client.raw.rows[0]["body"] == body
    cached = client.call("tiktok/trending", params)
    assert cached.status == "error" and cached.failure == "parse"
    assert cached.cache_hit and cached.credits_charged == 0
    assert client.http.routes().count("tiktok/trending") == 1


@pytest.mark.parametrize("data", [{"items": []}, [], {}])
def test_legitimate_empty_envelopes_keep_empty_and_cached_status(data):
    client = make(http=FakeHTTP({"tiktok/trending": (200, {"success": True, "credits_used": 0, "data": data})}))
    params = {"region": "ZA", "feed": "local"}
    first = client.call("tiktok/trending", params)
    assert first.status == "empty" and first.failure == ""
    assert first.credits_charged == 0 and first.items == []
    cached = client.call("tiktok/trending", params)
    assert cached.status == "cached" and cached.failure == ""
    assert cached.credits_charged == 0 and cached.items == []


def test_missing_envelope_reaches_collection_health_as_parse_failure():
    from core.collect import job, writers
    from core.collect.tests.test_socialcrawl_client import NOW, TODAY

    client = make(http=FakeHTTP({"tiktok/trending": (200, {"success": True, "credits_used": 5})}))
    collected = job.Collected("synthetic-envelope-health")
    runner = job._Runner(client, collected, job._CountedIds(lambda kind, raw, platform: str(raw)),
                         job.safe_geo(lambda *args, **kwargs: (None, None, None)),
                         lambda: NOW, job.Budget(), None, TODAY)
    calls = [call for call in job.harvest_calls("ZA", TODAY, job.load_config())[0]
             if call.route == "tiktok/trending"]
    runner.calls(calls[:1], "ZA")
    assert collected.observations == [] and collected.posts == []
    [record] = collected.records
    assert record["ok"] is False and record["failure"] == "parse"
    [health] = writers.health_rows(collected.records, [], {}, collected.run_id)
    assert health["valid"] is False and health["invalid_reason"] == "calls: parse"
