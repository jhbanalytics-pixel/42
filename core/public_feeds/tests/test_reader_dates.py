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


# Feeds that read their headline from a pulse story card or an article card link take a date from the page's
# JSON-LD only: there is no heading order to fall back on.


def parse(feed_id, html):
    return {e["text"]: e["published_at"] for e in reader.parse_feed(feed(feed_id), html, NOW)}


def ld_page(url, value):
    return ('<!doctype html><html><head><script type="application/ld+json">'
            f'{{"@type":"NewsArticle","url":"{url}","datePublished":"{value}"}}</script></head><body>')


PULSE_DATED = "https://www.pulse.ng/story/fixture-dated-2026093008000000001"
PULSE_OTHER = "https://www.pulse.ng/story/fixture-other-2026093008000000002"


def test_a_pulse_story_card_is_dated_from_the_json_ld_for_its_url():
    html = (ld_page(PULSE_DATED, "2026-09-30T08:15:00+01:00")
            + f'<article><a class="font-accent" href="{PULSE_DATED}">Pulse dated headline</a></article>'
            + f'<article><a class="font-accent" href="{PULSE_OTHER}">Pulse other headline</a></article></body></html>')

    got = parse("ng_pulse", html)

    assert instant(got["Pulse dated headline"]) == datetime(2026, 9, 30, 7, 15, tzinfo=timezone.utc)
    assert got["Pulse other headline"] is None


def test_a_pulse_story_card_with_two_json_ld_readings_that_disagree_stays_undated():
    html = (ld_page(PULSE_DATED, "2026-09-30T08:15:00+01:00")
            + f'<script type="application/ld+json">{{"url":"{PULSE_DATED}","datePublished":"2026-09-30T05:00:00+01:00"}}'
            + f'</script><article><a class="font-accent" href="{PULSE_DATED}">Pulse dated headline</a></article>'
            + "</body></html>")

    assert parse("ng_pulse", html)["Pulse dated headline"] is None


CARD_DATED = "https://www.tuko.co.ke/123456-fixture-dated-story.html"
CARD_OTHER = "https://www.tuko.co.ke/123457-fixture-other-story.html"


@pytest.mark.parametrize("feed_id,base", [("ke_tuko", "https://www.tuko.co.ke"), ("ng_legit", "https://www.legit.ng")])
def test_an_article_card_headline_is_dated_from_the_json_ld_for_its_url(feed_id, base):
    dated, other = (f"{base}/123456-fixture-dated-story.html", f"{base}/123457-fixture-other-story.html")
    html = (ld_page(dated, "2026-09-30T08:15:00+01:00")
            + f'<div><a class="c-article-card-horizontal__headline" href="{dated}">Card dated headline</a></div>'
            + f'<div><a class="c-article-card-horizontal__headline" href="{other}">Card other headline</a></div>'
            + "</body></html>")

    got = parse(feed_id, html)

    assert instant(got["Card dated headline"]) == datetime(2026, 9, 30, 7, 15, tzinfo=timezone.utc)
    assert got["Card other headline"] is None


def test_an_article_card_headline_with_conflicting_json_ld_stays_undated():
    html = (ld_page(CARD_DATED, "2026-09-30T08:15:00+01:00")
            + f'<script type="application/ld+json">{{"url":"{CARD_DATED}","datePublished":"2026-09-30T05:00:00+01:00"}}'
            + f'</script><div><a class="c-article-card-horizontal__headline" href="{CARD_DATED}">Card dated headline'
            + "</a></div></body></html>")

    assert parse("ke_tuko", html)["Card dated headline"] is None


# A card whose own publish times disagree stays undated even when the page's JSON-LD names one time for it.


def test_a_card_with_conflicting_times_stays_undated_though_json_ld_names_one():
    url = "https://punchng.com/fixture-card-conflict/"
    html = (ld_page(url, "2026-09-30T08:00:00+01:00")
            + '<article><time datetime="2026-09-30T05:00:00+01:00" class="published">05:00</time>'
            + '<time datetime="2026-09-30T06:00:00+01:00" class="published">06:00</time>'
            + f'<h3 class="entry-title"><a href="{url}">Card conflict headline</a></h3></article></body></html>')

    assert parse("ng_punch", html)["Card conflict headline"] is None


def test_a_card_and_json_ld_that_agree_date_the_headline():
    url = "https://punchng.com/fixture-card-agree/"
    html = (ld_page(url, "2026-09-30T08:00:00+01:00")
            + '<article><time datetime="2026-09-30T07:00:00Z" class="published">07:00</time>'
            + f'<h3 class="entry-title"><a href="{url}">Card agree headline</a></h3></article></body></html>')

    assert instant(parse("ng_punch", html)["Card agree headline"]) == datetime(2026, 9, 30, 7, 0, tzinfo=timezone.utc)


# A page that ends inside an open article still dates the headline that card holds.


def test_an_article_left_open_at_the_end_of_the_page_still_dates_its_headline():
    html = ('<!doctype html><html><body><article class="article-tile">'
            '<time datetime="2026-09-30T08:00:00+02:00">08:00</time>'
            '<h3 class="heading"><a href="https://www.enca.com/news/fixture-open">Open card headline</a></h3>')

    assert parse("za_enca", html)["Open card headline"] == "2026-09-30T08:00:00+02:00"


def test_an_open_article_at_the_end_after_a_closed_one_dates_both():
    html = ('<!doctype html><html><body>'
            '<article><time datetime="2026-09-30T07:00:00+02:00">07:00</time>'
            '<h3 class="heading"><a href="https://www.enca.com/news/fixture-closed">Closed card headline</a></h3>'
            '</article>'
            '<article><time datetime="2026-09-30T08:00:00+02:00">08:00</time>'
            '<h3 class="heading"><a href="https://www.enca.com/news/fixture-open">Open card headline</a></h3>')

    got = parse("za_enca", html)

    assert got["Closed card headline"] == "2026-09-30T07:00:00+02:00"
    assert got["Open card headline"] == "2026-09-30T08:00:00+02:00"


def test_a_card_time_is_not_used_when_the_json_ld_for_its_url_disagrees_with_itself():
    url = "https://punchng.com/fixture-ld-conflict/"
    html = (ld_page(url, "2026-09-30T08:00:00+01:00")
            + f'<script type="application/ld+json">{{"url":"{url}","datePublished":"2026-09-30T05:00:00+01:00"}}</script>'
            + '<article><time datetime="2026-09-30T07:00:00Z" class="published">07:00</time>'
            + f'<h3 class="entry-title"><a href="{url}">Ld conflict headline</a></h3></article></body></html>')

    assert parse("ng_punch", html)["Ld conflict headline"] is None


def test_json_ld_that_names_a_different_instant_from_the_card_leaves_the_headline_undated():
    url = "https://punchng.com/fixture-differ/"
    html = (ld_page(url, "2026-09-30T09:00:00+01:00")
            + '<article><time datetime="2026-09-30T07:00:00Z" class="published">07:00</time>'
            + f'<h3 class="entry-title"><a href="{url}">Differ headline</a></h3></article></body></html>')

    assert parse("ng_punch", html)["Differ headline"] is None
