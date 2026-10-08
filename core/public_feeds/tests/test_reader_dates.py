"""Publish dates for the news homepage scrape, read only from the HTML already fetched (W8-DEC-12, plan item 6)."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.public_feeds import reader
from core.public_feeds.catalog import confirmed_feeds


NOW = datetime(2026, 9, 30, 10, 30, tzinfo=timezone.utc)
DATED = Path(__file__).resolve().parent / "samples" / "dated"


def feed(feed_id):
    return next(f for f in confirmed_feeds() if f.feed_id == feed_id)


def read(feed_id, name):
    html = (DATED / name).read_text(encoding="utf-8")
    return {e["text"]: e["published_at"] for e in reader.parse_feed(feed(feed_id), html, NOW)}


def instant(value):
    return None if value is None else datetime.fromisoformat(value).astimezone(timezone.utc)


def test_json_ld_date_is_matched_to_the_headline_by_article_url():
    got = read("ng_punch", "ng_punch_jsonld_itemlist.html")

    assert instant(got["Alpha story headline"]) == datetime(2026, 9, 30, 7, 15, tzinfo=timezone.utc)
    assert instant(got["Beta story headline"]) == datetime(2026, 9, 30, 7, 5, tzinfo=timezone.utc)


def test_a_date_without_an_offset_or_without_a_time_stays_undated():
    got = read("ng_punch", "ng_punch_jsonld_itemlist.html")

    assert got["Gamma story headline"] is None
    assert got["Delta story headline"] is None


def test_page_level_meta_and_other_scripts_and_other_hosts_never_date_a_headline():
    got = read("ng_punch", "ng_punch_jsonld_itemlist.html")

    assert got["Zeta story headline"] is None
    assert got["Eta story headline"] is None


def test_json_ld_never_adds_a_post_that_has_no_headline_on_the_page():
    got = read("ng_punch", "ng_punch_jsonld_itemlist.html")

    assert set(got) == {"Alpha story headline", "Beta story headline", "Gamma story headline",
                        "Delta story headline", "Zeta story headline", "Eta story headline"}


def test_article_card_meta_gives_the_published_time_not_the_modified_time_or_the_page_meta():
    got = read("ng_channels_television", "ng_channels_article_microdata.html")

    assert got["Fixture one headline"] == "2026-09-30T10:20:00+01:00"
    assert got["Fixture two headline"] == "2026-09-30T09:00:00+01:00"
    assert got["Fixture three headline"] is None
    assert got["Fixture four headline"] is None


def test_a_time_element_is_bound_to_its_own_article_even_when_it_comes_before_the_heading():
    got = read("za_enca", "za_enca_time_before_heading.html")

    assert got["Fixture first headline"] == "2026-09-30T08:00:00+02:00"
    assert got["Fixture second headline"] is None
    assert got["Fixture third headline"] == "2026-09-30T07:45:00+02:00"
    assert got["Fixture fourth headline"] is None


def test_a_card_that_names_two_different_publish_times_stays_undated():
    got = read("za_enca", "za_enca_time_before_heading.html")

    assert got["Fixture fifth headline"] is None


def test_an_article_wrapping_several_headlines_keeps_the_heading_order_binding():
    got = read("za_enca", "za_enca_wrapper_article.html")

    assert got["Wrapped one headline"] == "2026-09-30T06:10:00+02:00"
    assert got["Wrapped two headline"] == "2026-09-30T06:40:00+02:00"


def test_two_sources_that_disagree_on_the_instant_leave_the_post_undated():
    got = read("ng_punch", "ng_punch_jsonld_conflict.html")

    assert got["Conflict headline"] is None


def test_two_json_ld_entries_for_one_url_that_disagree_leave_the_post_undated():
    got = read("ng_punch", "ng_punch_jsonld_conflict.html")

    assert got["Twice headline"] is None


def test_two_sources_that_name_the_same_instant_in_different_offsets_date_the_post():
    got = read("ng_punch", "ng_punch_jsonld_conflict.html")

    assert instant(got["Agree headline"]) == datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc)


def test_malformed_json_ld_is_ignored_without_failing_the_read():
    got = read("ng_punch", "ng_punch_jsonld_conflict.html")

    assert set(got) == {"Conflict headline", "Agree headline", "Twice headline"}


def test_dating_uses_only_the_body_already_fetched_one_transport_call_and_nothing_else(monkeypatch):
    calls = []
    body = (DATED / "ng_punch_jsonld_itemlist.html").read_bytes()

    def transport(url, *, timeout, max_bytes):
        calls.append(url)
        return 200, {"content-type": "text/html; charset=utf-8"}, body

    def no_network(*args, **kwargs):
        raise AssertionError("dating must not fetch anything")

    monkeypatch.setattr("requests.Session.get", no_network)
    entries = reader.read_feed(feed("ng_punch"), fetched_at=NOW, transport=transport)

    assert calls == [feed("ng_punch").url]
    assert sum(1 for e in entries if e["published_at"]) == 2


@pytest.mark.parametrize("feed_id,name", [
    ("za_sabc_news", "za_sabc_news_20260930.html"),
    ("za_enca", "za_enca_20260930.html"),
    ("ng_channels_television", "ng_channels_tv_20260930.html"),
])
def test_saved_homepages_keep_their_dates_exactly_as_before(feed_id, name):
    html = (Path(__file__).resolve().parent / "samples" / name).read_text(encoding="utf-8")
    entries = reader.parse_feed(feed(feed_id), html, NOW)

    dated = {e["text"]: e["published_at"] for e in entries if e["published_at"]}
    if feed_id == "za_sabc_news":
        assert dated["ActionSA calls for probe into eThekwini contract awarded to Moriel"] == \
            "2026-09-30T12:16:00+02:00"
    else:
        assert dated == {}
