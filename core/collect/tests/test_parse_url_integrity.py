import pytest

from core.collect import ids
from core.collect.tests.test_parse import fixture, run


def test_malformed_url_without_native_id_is_skipped_and_valid_siblings_survive(caplog):
    body = fixture("rank", "tiktok_trending")
    first = body["data"]["items"][0]["post"]
    first.pop("id")
    first["url"] = "https://[invalid"
    rows = run("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", body, pull_seq=1)
    assert len(rows["posts"]) == len(rows["observations"]) == 2
    assert rows["posts"][0]["native_id"] == "7400000000000000002"
    assert "malformed URL" in caplog.text
    assert "https://[invalid" not in caplog.text


def test_valid_native_identity_does_not_depend_on_url_parsing():
    assert ids.post_id("tiktok", "7400000000000000001", "https://[invalid") == ids.post_id("tiktok", "7400000000000000001")
    assert ids.canonical_url("HTTPS://EXAMPLE.com:443/post?utm_source=x&a=1") == "https://example.com/post?a=1"


@pytest.mark.parametrize("body", [{"success": True}, {"success": True, "data": None}, {"success": True, "data": "bad"}])
def test_direct_parser_rejects_unreadable_data_instead_of_empty_rows(body):
    with pytest.raises(ValueError, match="data"):
        run("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", body, pull_seq=1)


def test_direct_parser_keeps_legitimate_empty_list():
    rows = run("tiktok/trending", {"region": "ZA", "feed": "local"}, "ZA", {"success": True, "data": {"items": []}}, pull_seq=1)
    assert rows == {"posts": [], "observations": [], "counters": []}


def test_board_url_fallback_rejects_bad_identity_and_keeps_valid_sibling(caplog):
    body = {"success": True, "data": {"items": [{"post": {"url": "https://[invalid"}},
        {"post": {"url": "https://www.tiktok.com/tag/amapiano"}}]}}
    rows = run("tiktok/hashtags/popular", {"countryCode": "ZA"}, "ZA", body, pull_seq=1)
    assert len(rows["counters"]) == 1
    assert rows["counters"][0]["item_id"] == "hashtag|amapiano"
    assert "malformed URL" in caplog.text


def test_restat_platform_fallback_rejects_bad_url_and_keeps_valid_sibling(caplog):
    body = {"success": True, "data": {"results": [{"post": {"url": "https://[invalid"}},
        {"post": {"id": "1", "url": "https://x.com/example/status/1", "engagement": {"views": 10}}}]}}
    rows = run("prism/post-stats", {}, "ZA", body)
    assert len(rows["observations"]) == 1
    assert rows["observations"][0]["platform"] == "twitter"
    assert rows["observations"][0]["views"] == 10
    assert "malformed URL" in caplog.text
