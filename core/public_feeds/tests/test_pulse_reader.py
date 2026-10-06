from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.public_feeds import reader
from core.public_feeds.catalog import confirmed_feeds


NOW = datetime(2026, 9, 30, 10, 30, tzinfo=timezone.utc)
SAMPLES = Path(__file__).resolve().parent / "samples"
PULSE_ARTICLES = (
    (
        "Breaking: Morocco makes history as Fatima Ezzahra El Mansouri becomes first woman prime minister and second woman to lead an Arab government",
        "https://www.pulse.ng/story/fatima-ezzahra-el-mansouri-morocco-prime-minister-2026093006452521389",
    ),
    (
        "Pastor sets minimum church offering at GH₵50 (about ₦6,000), says transport fare has gone up",
        "https://www.pulse.ng/story/prophet-sets-ghc50-minimum-church-offering-2026093008461619494",
    ),
    (
        "California just put two Muslim holidays on its official calendar \u2014 but it doesn’t mean everyone gets two days off",
        "https://www.pulse.ng/story/california-eid-holidays-state-calendar-what-it-means-2026093008342338767",
    ),
)


def test_pulse_native_article_cards_emit_the_three_exact_story_links():
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "ng_pulse")
    html = (SAMPLES / "ng_pulse_native_20260930.html").read_text(encoding="utf-8")

    entries = reader.parse_feed(feed, html, NOW)

    assert [(entry["text"], entry["url"]) for entry in entries] == list(PULSE_ARTICLES)
    assert all(entry["platform"] == "news" for entry in entries)
    assert all(entry["kind"] == "news" for entry in entries)
    assert all(entry["source_market"] == "NG" for entry in entries)
    assert all(entry["source_feed_id"] == "ng_pulse" for entry in entries)
    assert all(entry["source_url"] == "https://www.pulse.ng/" for entry in entries)
    assert all(entry["published_at"] is None for entry in entries)
    assert all(entry["observed_at"] == "2026-09-30T10:30:00+00:00" for entry in entries)
    assert all(
        not {"geo_market", "geo_confidence", "geo_source"} & entry.keys()
        for entry in entries
    )


def test_pulse_article_extraction_ignores_navigation_and_author_links():
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "ng_pulse")
    html = (SAMPLES / "ng_pulse_native_20260930.html").read_text(encoding="utf-8")
    html += (
        '<nav><a href="https://www.pulse.ng/category/politics">Politics</a></nav>'
        '<article><div class="author-byline">'
        '<a class="text-neutral-500" href="https://www.pulse.ng/author/pulse-staff">'
        'Pulse Staff</a></div></article>'
    )

    entries = reader.parse_feed(feed, html, NOW)

    assert [(entry["text"], entry["url"]) for entry in entries] == list(PULSE_ARTICLES)


@pytest.mark.parametrize(
    "href,title",
    (
        (
            "https://www.pulse.ng/story/signed-candidate?X-Amz-Signature=placeholder",
            "Signed URL Candidate",
        ),
        ("https://www.pulse.ng/story/query-candidate?id=123", "Query URL Candidate"),
        (
            "https://user:placeholder@www.pulse.ng/story/userinfo-candidate",
            "Userinfo URL Candidate",
        ),
    ),
    ids=["signed-query", "benign-query", "userinfo"],
)
def test_pulse_article_extraction_rejects_signed_query_and_userinfo_urls(href, title):
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == "ng_pulse")
    html = (SAMPLES / "ng_pulse_native_20260930.html").read_text(encoding="utf-8")
    html += f'<article><a class="font-accent" href="{href}">{title}</a></article>'

    entries = reader.parse_feed(feed, html, NOW)

    assert [(entry["text"], entry["url"]) for entry in entries] == list(PULSE_ARTICLES)
    assert all(title != entry["text"] for entry in entries)
    assert all("placeholder" not in str(entry).casefold() for entry in entries)


@pytest.mark.parametrize(("feed_id", "sample", "market", "expected"), [
    ("ke_tuko", "ke_tuko_native_20261002.html", "KE", [
        ("Ruto announces new investor for KSh33 billion Kitui cement factory years after Dangote’s failed bid",
         "https://www.tuko.co.ke/kenya/counties/641836-ruto-announces-investor-ksh33-billion-kitui-cement-factory-years-dangotes-failed-bid/"),
        ("UK Lists 10 Conditions for Bringing Elderly Parent or Grandparent to Country",
         "https://www.tuko.co.ke/people/family/641839-uk-lists-10-conditions-bringing-elderly-parent-grandparent-country/"),
        ("Linda Mwananchi Movement name reserved for 90 days, Sifuna reacts",
         "https://www.tuko.co.ke/politics/641841-linda-mwananchi-movement-reserved-90-days-sifuna-reacts/"),
    ]),
    ("ng_legit", "ng_legit_native_20261002.html", "NG", [
        ("Wike, Ribadu or Akpabio? Prophet names Tinubu’s ally who could become Nigeria's president",
         "https://www.legit.ng/politics/1733854-wike-ribadu-akpabio-prophet-mentions-tinubus-appointee-nigerias-president/"),
        ("“This one is for every woman”: Phyna speaks after boxing victory against Nkechi Blessing",
         "https://www.legit.ng/entertainment/celebrities/1733952-woman-phyna-speaks-boxing-victory-nkechi-blessing/"),
        ("New Zealand announces 2 parent visa options, names 1 offering permanent residence slots",
         "https://www.legit.ng/people/1733938-new-zealand-announces-2-parent-visa-options-2500-permanent-residence-slots-yearly/"),
    ]),
])
def test_legit_media_article_cards_emit_their_headline_links(feed_id, sample, market, expected):
    # Tuko and Legit (read 2 Oct 2026) put each headline in an article card link, not a heading, so the
    # heading reader found nothing. Only article links count: the footer logo link and section titles do not.
    feed = next(feed for feed in confirmed_feeds() if feed.feed_id == feed_id)
    html = (SAMPLES / sample).read_text(encoding="utf-8")

    entries = reader.parse_feed(feed, html, NOW)

    assert [(entry["text"], entry["url"]) for entry in entries] == expected
    assert all(entry["kind"] == "news" and entry["source_market"] == market for entry in entries)
    assert all(entry["source_feed_id"] == feed_id for entry in entries)
