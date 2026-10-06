"""The spike object an ask may carry (core/api/contract.md sections 6 and 14.1)."""
import pytest

from core.api.spike import check_spike

GOOD = {"item_id": "afe2bf26_x-1", "market": "ZA", "date": "2026-09-28", "series": "feed_tiktok"}


@pytest.mark.parametrize("value", [
    GOOD,
    {k: GOOD[k] for k in ("item_id", "market", "date")},
    {**GOOD, "series": None},
    {**GOOD, "market": "NG", "series": "s" * 80},
    {**GOOD, "market": "KE", "item_id": "x" * 128},
])
def test_a_good_spike_comes_back_as_sent(value):
    assert check_spike(value) == value


@pytest.mark.parametrize("value, words", [
    ("abc", "spike must be an object or null."),
    ([1], "spike must be an object or null."),
    ({**GOOD, "note": "anything"}, "spike takes only item_id, market, date and series, not note."),
    ({**GOOD, "b": 1, "a": 2}, "spike takes only item_id, market, date and series, not a, b."),
    ({"market": "ZA", "date": "2026-09-28"}, "spike needs item_id, market and date."),
    ({**GOOD, "item_id": "bad id!"}, "spike item_id must be an item id."),
    ({**GOOD, "item_id": "x" * 129}, "spike item_id must be an item id."),
    ({**GOOD, "item_id": "abc\n"}, "spike item_id must be an item id."),
    ({**GOOD, "item_id": 5}, "spike item_id must be an item id."),
    ({**GOOD, "market": "GH"}, "spike market must be ZA, NG or KE."),
    ({**GOOD, "market": "za"}, "spike market must be ZA, NG or KE."),
    ({**GOOD, "market": None}, "spike market must be ZA, NG or KE."),
    ({**GOOD, "date": "28 September"}, "spike date must be a real date written YYYY-MM-DD."),
    ({**GOOD, "date": "2026-02-30"}, "spike date must be a real date written YYYY-MM-DD."),
    ({**GOOD, "date": "20260928"}, "spike date must be a real date written YYYY-MM-DD."),
    ({**GOOD, "date": "2026-09-28\n"}, "spike date must be a real date written YYYY-MM-DD."),
    ({**GOOD, "date": None}, "spike date must be a real date written YYYY-MM-DD."),
    ({**GOOD, "series": "s" * 81}, "spike series must be 1 to 80 letters, digits or _, or left out."),
    ({**GOOD, "series": "feed-tiktok"}, "spike series must be 1 to 80 letters, digits or _, or left out."),
    ({**GOOD, "series": "feed_tiktók"}, "spike series must be 1 to 80 letters, digits or _, or left out."),
    ({**GOOD, "series": ""}, "spike series must be 1 to 80 letters, digits or _, or left out."),
    ({**GOOD, "series": 5}, "spike series must be 1 to 80 letters, digits or _, or left out."),
])
def test_a_bad_spike_is_refused_in_plain_words(value, words):
    with pytest.raises(ValueError) as caught:
        check_spike(value)
    assert str(caught.value) == words


def test_the_cleaned_spike_is_a_new_dict():
    value = dict(GOOD)
    cleaned = check_spike(value)
    cleaned["series"] = "changed"
    assert value == GOOD


def _collector_series():
    """Every series name the collector can write, read from its own definitions."""
    from datetime import date, timedelta

    from core.collect import job, local_sources, parse, public_feed_collect

    names = {entry[2] for entry in parse.ROUTES.values() if entry[2] is not None}
    names |= set(parse.SEARCH_LANES)  # a superset: a search or seeded post is filed as search, placebo or agent_live
    names.add(job.Call("23b", "web/scrape", {}, "ZA", None).series())
    names |= {s.series for s in local_sources.SCRAPE_SOURCES.values()}
    names |= set(local_sources.LOCAL_RANK_SERIES)
    start = date(2026, 9, 28)
    names |= {f.series for d in range(7) for f in local_sources.plan(start + timedelta(days=d))}
    names |= set(public_feed_collect.SERIES_BY_KIND.values())
    return sorted(names)


COLLECTOR_SERIES = _collector_series()


def test_the_collector_series_list_is_read_from_the_collector():
    assert {"feed_tiktok", "x_trends", "board_google_play", "panel_ig_gossip", "news_rss",
            "board_music_country", "agent_live"} <= set(COLLECTOR_SERIES)


@pytest.mark.parametrize("series", COLLECTOR_SERIES)
def test_every_collector_series_passes_the_spike_checks(series):
    from core.agent.skills import SERIES
    from core.api.spike import SERIES_RE

    assert SERIES_RE.fullmatch(series).group(0) == series
    assert SERIES.fullmatch(series).group(0) == series
    assert check_spike({**GOOD, "series": series}) == {**GOOD, "series": series}
