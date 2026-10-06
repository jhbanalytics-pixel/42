from datetime import datetime, timezone
from pathlib import Path

from core.collect.ids import post_id
from core.collect.local_sources import song_key
from core.collect.parse import COUNTER_COLUMNS, OBSERVATION_COLUMNS, POST_COLUMNS
from core.detect.items import canonical_key, item_id
from core.public_feeds import reader
from core.public_feeds.catalog import confirmed_feeds
from core.collect.public_feed_rows import feed_protocol, normalise


OBSERVED_AT = datetime(2026, 9, 30, 10, 30, tzinfo=timezone.utc)
SAMPLES = Path(__file__).resolve().parents[2] / "public_feeds" / "tests" / "samples"


def feed_by_id(feed_id):
    return next(feed for feed in confirmed_feeds() if feed.feed_id == feed_id)


def parsed_entries(feed_id, sample_name):
    feed = feed_by_id(feed_id)
    body = (SAMPLES / sample_name).read_text(encoding="utf-8")
    return feed, reader.parse_feed(feed, body, OBSERVED_AT)


def canonical_item_id(kind, raw, platform):
    return item_id(kind, canonical_key(kind, raw, platform))


def convert(feed, entries, item_id_fn=canonical_item_id):
    return normalise(feed, entries, observed_at=OBSERVED_AT, run_id="run-1", pull_seq=3,
                     item_id_fn=item_id_fn)


def test_news_reader_fixture_emits_citable_unlocated_post_and_feed_sighting():
    feed, entries = parsed_entries("za_sabc_news", "za_sabc_news_20260930.html")
    entry = next(entry for entry in entries if entry["text"].startswith("ActionSA calls for probe"))

    out = convert(feed, [entry])

    assert set(out) == {"posts", "observations", "counters", "items", "safe_entries", "dropped"}
    assert len(out["posts"]) == 1
    assert len(out["observations"]) == 1
    assert out["counters"] == []
    post = out["posts"][0]
    observation = out["observations"][0]
    assert tuple(post) == POST_COLUMNS
    assert tuple(observation) == OBSERVATION_COLUMNS
    assert post["post_id"] == post_id("news", url=entry["url"])
    assert post["platform"] == "news"
    assert post["url"] == entry["url"]
    assert post["published_at"] == "2026-09-30T10:16:00+00:00"
    assert post["post_date"] == "2026-09-30"
    assert post["vendor_labels"] == {"date_basis": "published_at"}
    assert post["vendor"] == post["source_regime"] == "public_feed"
    assert post["endpoint"] == "public_feed"
    assert all(post[field] is None for field in ("geo_market", "geo_confidence", "geo_source", "geo_scope"))
    assert observation["market"] == observation["source_market"] == feed.market
    assert observation["source_region"] is None
    assert observation["route"] == "public_feed"
    assert observation["series"] == "news_rss"
    assert observation["seed_key"] == feed.feed_id
    assert observation["protocol"] == feed_protocol(feed)
    assert observation["protocol"] == (
        "public_feed?feed_id=za_sabc_news&url=https%3A%2F%2Fwww.sabcnews.com%2Fsabcnews%2F"
    )
    assert observation["lane"] == "public_feed"
    assert observation["lane_class"] == "context"
    assert observation["pull_seq"] == 3
    assert out["safe_entries"][0]["source_feed_id"] == feed.feed_id
    assert out["safe_entries"][0]["source_market"] == feed.market


def test_news_duplicate_is_one_post_and_one_observation_for_its_feed():
    feed, entries = parsed_entries("za_sabc_news", "za_sabc_news_20260930.html")
    entry = entries[0]

    out = convert(feed, [entry, entry])

    assert len(out["posts"]) == 1
    assert len(out["observations"]) == 1
    assert out["observations"][0]["seed_key"] == feed.feed_id
    assert out["observations"][0]["protocol"] == feed_protocol(feed)
    assert out["dropped"].get("duplicate_post") == 1
    assert len(out["safe_entries"]) == 2


def test_mdundo_fixture_uses_catalog_platform_and_existing_sound_identity():
    feed, entries = parsed_entries("ke_mdundo_top_songs", "ke_mdundo_20260930.html")
    entry = next(entry for entry in entries if entry.get("rank", 0) > 0)

    out = convert(feed, [entry])

    assert out["posts"] == []
    assert out["observations"] == []
    counter = out["counters"][0]
    assert tuple(counter) == COUNTER_COLUMNS
    assert counter["platform"] == "mdundo"
    assert counter["series"] == "board_music_country"
    assert counter["route"] == "public_feed"
    assert counter["protocol"] == feed_protocol(feed)
    assert counter["unit"] == "rank"
    assert counter["is_board"] is True
    assert counter["lane_class"] == "unbiased_rank"
    assert counter["value"] == float(entry["rank"])
    raw = song_key({"artist": entry.get("artist", ""), "title": entry["text"]})
    assert out["items"][counter["item_id"]] == ("sound", raw, "mdundo", raw)
    assert out["safe_entries"][0]["post_id"] is None


def test_turntable_fixture_uses_catalog_platform_and_source_rank():
    feed, entries = parsed_entries("ng_turntable_top_100", "ng_turntable_ng_top_100_20260930.html")
    entry = next(entry for entry in entries if entry["text"] == "Volume")

    out = convert(feed, [entry])

    assert out["posts"] == []
    assert out["observations"] == []
    counter = out["counters"][0]
    assert counter["platform"] == "turntable"
    assert counter["series"] == "board_music_country"
    assert counter["value"] == 1.0
    assert out["items"][counter["item_id"]] == ("sound", "Seyi Vibez - Volume", "turntable",
                                                  "Seyi Vibez - Volume")


def test_playlist_fixture_is_nonranked_radio_presence_without_posts():
    feed, entries = parsed_entries("ke_kbc_english_playlist", "ke_kbc_english_service_playlist_20260930.html")
    entry = entries[0]

    out = convert(feed, [entry])

    assert out["posts"] == []
    assert out["observations"] == []
    counter = out["counters"][0]
    assert tuple(counter) == COUNTER_COLUMNS
    assert counter["platform"] == "radio"
    assert counter["series"] == "radio_playlist"
    assert counter["route"] == "public_feed"
    assert counter["unit"] == "appearances"
    assert counter["is_board"] is False
    assert counter["lane_class"] == "context"
    assert counter["value"] == 1.0
    assert out["safe_entries"][0]["post_id"] is None
    assert out["safe_entries"][0].get("time_text") == entry.get("time_text")


def test_news_without_source_time_keeps_null_timestamp_and_uses_observed_day():
    feed, entries = parsed_entries("ng_pulse", "ng_pulse_native_20260930.html")
    entry = entries[0]
    assert entry["published_at"] is None

    out = convert(feed, [entry])

    post = out["posts"][0]
    assert post["published_at"] is None
    assert post["post_date"] == "2026-09-30"
    assert post["vendor_labels"] == {"date_basis": "observed_at"}


def test_entry_provenance_is_catalog_owned_and_unsafe_context_is_scrubbed():
    feed, entries = parsed_entries("za_sabc_news", "za_sabc_news_20260930.html")
    spoofed = {
        **entries[0],
        "platform": "spoofed",
        "source_market": "KE",
        "source_feed_id": "fake_feed",
        "source_url": "https://evil.example/",
    }
    entry = {
        **entries[0],
        "url": "https://user:secret@example.org/story?access_token=secret",
        "text": "<script>secret</script><b>Clean headline</b> authorization=secret",
        "headers": {"Authorization": "Bearer secret"},
        "access_token": "secret",
    }

    out = convert(feed, [spoofed, None, entry])

    assert len(out["posts"]) == 1
    assert len(out["observations"]) == 1
    assert out["posts"][0]["platform"] == "news"
    assert out["observations"][0]["market"] == "ZA"
    assert out["observations"][0]["source_market"] == "ZA"
    assert out["observations"][0]["seed_key"] == feed.feed_id
    assert out["dropped"].get("malformed_entry") == 1
    assert out["dropped"].get("unsafe_url") == 1
    accepted = out["safe_entries"][0]
    assert accepted["source_market"] == "ZA"
    assert accepted["source_feed_id"] == feed.feed_id
    assert accepted["source_url"] == feed.url
    assert accepted["platform"] == "news"
    assert accepted["reason"] is None
    safe = out["safe_entries"][1]
    assert safe["source_market"] == "ZA"
    assert safe["source_feed_id"] == feed.feed_id
    assert safe["source_url"] == feed.url
    assert safe["platform"] == "news"
    assert safe["url"] is None
    assert safe["text"] == "Clean headline [redacted]"
    assert safe["reason"] == "unsafe_url"
    assert not {"headers", "access_token"} & safe.keys()
    assert "secret" not in str(safe).casefold()
    assert "<" not in safe["text"]


def test_music_without_safe_identity_is_context_only_and_never_a_post():
    feed, entries = parsed_entries("ke_mdundo_top_songs", "ke_mdundo_20260930.html")
    entry = {**entries[0], "text": "", "artist": None, "item_key": "spoofed identity"}

    out = convert(feed, [entry])

    assert out["posts"] == []
    assert out["observations"] == []
    assert out["counters"] == []
    assert out["items"] == {}
    assert out["safe_entries"][0]["post_id"] is None
    assert out["safe_entries"][0]["reason"] == "missing_song_identity"
    assert out["dropped"].get("missing_song_identity") == 1


def test_unranked_chart_entry_is_held_without_synthetic_rank():
    feed, entries = parsed_entries("ng_turntable_top_100", "ng_turntable_ng_top_100_20260930.html")
    entry = {**entries[0], "rank": None}

    out = convert(feed, [entry])

    assert out["counters"] == []
    assert out["items"] == {}
    assert out["safe_entries"][0]["rank"] is None
    assert out["safe_entries"][0]["reason"] == "missing_rank"
    assert out["dropped"].get("missing_rank") == 1


def test_duplicate_ranked_song_keeps_best_rank_once():
    feed, entries = parsed_entries("ng_turntable_top_100", "ng_turntable_ng_top_100_20260930.html")
    entry = next(entry for entry in entries if entry["rank"] == 1)
    duplicate = {**entry, "rank": 4}

    out = convert(feed, [duplicate, entry])

    assert len(out["counters"]) == 1
    assert out["counters"][0]["value"] == 1.0
    assert len(out["items"]) == 1
    assert out["dropped"].get("duplicate_counter") == 1


def test_item_id_rejection_holds_song_without_counter_or_item():
    feed, entries = parsed_entries("ng_turntable_top_100", "ng_turntable_ng_top_100_20260930.html")
    entry = entries[0]

    def reject_item(kind, raw, platform):
        raise ValueError("item rejected")

    out = convert(feed, [entry], item_id_fn=reject_item)

    assert out["counters"] == []
    assert out["items"] == {}
    assert out["safe_entries"][0]["reason"] == "item_identity_rejected"
    assert out["dropped"].get("item_identity_rejected") == 1
