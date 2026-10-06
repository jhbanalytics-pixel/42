from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.public_feeds import telegram

SAMPLES = Path(__file__).resolve().parent / "samples"
BRIEFLY = SAMPLES / "za_telegram_brieflycoza_20261004.html"      # t.me/s/brieflycoza as served, 4 Oct 2026
NAIROBBY = SAMPLES / "ke_telegram_nairobby_handwritten.html"     # hand-written to the same markup


def test_the_switch_is_off():
    assert telegram.TELEGRAM_READY is False


def test_the_vetted_list_is_the_researched_one():
    """42-inputs/data/telegram-channels-2026-10-04.md: 20 NG, 19 KE and 6 ZA verified channels."""
    assert Counter(c.market for c in telegram.CHANNELS) == {"NG": 20, "KE": 19, "ZA": 6}
    handles = [c.handle.casefold() for c in telegram.CHANNELS]
    assert len(handles) == len(set(handles))
    assert all(telegram.valid_handle(c.handle) for c in telegram.CHANNELS)
    assert all(c.url == f"https://t.me/s/{c.handle}" for c in telegram.CHANNELS)
    assert len(telegram.CHANNELS) <= telegram.MAX_CHANNELS_PER_RUN
    # Dropped in the research (adult content, betting, inactive) and never read.
    for dropped in ("Nairobigossipclubk", "Nairobitrend254", "The_Nairobian", "citizentvke", "justnaijach",
                    "truthseekerszar", "myDorpieNews"):
        assert telegram.channel_for(dropped) is None
    assert telegram.channel_for("BRIEFLYCOZA").market == "ZA"


@pytest.mark.parametrize("label, value", [
    ("12", 12), ("950", 950), ("1.2K", 1200), ("12.7K", 12700), ("3.4M", 3_400_000), ("1,234", 1234),
    ("", None), ("views", None), (None, None),
])
def test_rounded_view_labels(label, value):
    assert telegram.views_count(label) == value


def test_a_saved_preview_page_gives_every_post_with_id_date_text_and_views():
    entries, dropped = telegram.parse_preview(telegram.channel_for("brieflycoza"),
                                              BRIEFLY.read_text(encoding="utf-8"))

    assert dropped == {}
    assert len(entries) == 20
    ids = [e["message_id"] for e in entries]
    assert ids == sorted(ids, reverse=True) and ids[0] == 51370 and ids[-1] == 51351
    first = entries[0]
    assert first["native_id"] == "brieflycoza/51370"
    assert first["url"] == "https://t.me/brieflycoza/51370"
    assert first["published_at"] == datetime(2026, 10, 4, 16, 23, 11, tzinfo=timezone.utc)
    assert first["text"].startswith("Leo Bozell accuses Ronald Lamola")
    assert first["views"] == 1
    last = entries[-1]
    assert last["text"].splitlines()[0] == ("Shadrack Sibiya challenges solitary confinement at Kgosi Mampuru "
                                            "C-Max section")
    assert last["views"] == 12
    # The link preview's title and description are not the post's text.
    assert "Former Hawks head" not in last["text"]
    assert all(e["channel"] == "brieflycoza" and isinstance(e["views"], int) and e["text"] for e in entries)
    assert all(e["published_at"].tzinfo is not None for e in entries)


def test_the_markup_variants_a_gossip_channel_shows():
    entries, dropped = telegram.parse_preview(telegram.channel_for("Nairobby"),
                                              NAIROBBY.read_text(encoding="utf-8"))

    assert dropped == {"service_message": 1, "no_text": 1, "other_channel": 1, "missing_date": 1, "duplicate": 1}
    by_id = {e["message_id"]: e for e in entries}
    assert sorted(by_id) == [6990, 7001, 7002, 7003]
    assert by_id[7001]["text"] == "Matatu art show at the KICC this weekend 🔥\nWho is going? #NairobiVibes #Matwana"
    assert by_id[7001]["hashtags"] == ["NairobiVibes", "Matwana"]
    assert by_id[7001]["views"] == 1200
    assert by_id[7001]["native_id"] == "nairobby/7001"
    # The quoted reply is not part of the reply's own text.
    assert by_id[7002]["text"] == "Update: tickets sold out by noon"
    assert by_id[7002]["views"] == 3_400_000
    # A forward is kept and marked; the "Forwarded from" line is not text.
    assert by_id[7003]["forwarded"] is True and by_id[7003]["text"] == "Sheng word of the day: noma"
    assert not any(e["forwarded"] for e in entries if e["message_id"] != 7003)


def test_posts_per_channel_are_capped_newest_first(monkeypatch):
    monkeypatch.setattr(telegram, "MAX_POSTS_PER_CHANNEL", 5)
    entries, dropped = telegram.parse_preview(telegram.channel_for("brieflycoza"),
                                              BRIEFLY.read_text(encoding="utf-8"))

    assert [e["message_id"] for e in entries] == [51370, 51369, 51368, 51367, 51366]
    assert dropped == {"over_channel_cap": 15}


def test_a_page_with_no_messages_gives_nothing():
    assert telegram.parse_preview(telegram.channel_for("brieflycoza"), "<html><body>Join</body></html>") == ([], {})
