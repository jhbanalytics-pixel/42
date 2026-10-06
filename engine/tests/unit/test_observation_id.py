"""Deterministic observation ids: one collected item keeps one id across collections.

The producer gave every raw row a fresh uuid4, so the same post collected twice read
as two unknown observations. observation_id derives the id from the source identity
(source, platform, market) and a canonical content key: the vendor native id where
one is carried, else the canonical URL, else normalised text with author and
published time. A row with none of these has no stable key and gets no derived id.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from src.ingestion.observation_id import (
    OBSERVATION_ID_SCHEME,
    observation_id,
    observation_id_scheme,
)

_ITEM = {
    "source": "rss",
    "platform": "web",
    "market": "za",
    "author_handle": "desk",
    "title": "Load shedding returns",
    "text": "Stage two from six tonight.",
    "url": "https://news.example.test/a/1?utm_source=feed#",
    "published_at": datetime(2026, 9, 7, 6, tzinfo=UTC),
}


def test_recollecting_the_same_item_gives_the_same_id():
    again = {
        **_ITEM,
        "url": "HTTPS://News.Example.Test:443/a/1",
        "query_term": "another term",
        "views": 40,
        "collected_at": datetime(2026, 9, 8, tzinfo=UTC),
    }
    assert observation_id(_ITEM) == observation_id(again)
    assert observation_id(_ITEM).startswith(OBSERVATION_ID_SCHEME + "_")


def test_the_vendor_native_id_is_the_key_where_one_is_carried():
    first = {**_ITEM, "platform": "tiktok", "native_id": "7301", "url": "https://t.test/1"}
    edited = {**first, "text": "edited caption", "url": "https://t.test/share/1"}
    other = {**first, "native_id": "7302"}
    assert observation_id(first) == observation_id(edited)
    assert observation_id(first) != observation_id(other)


def test_text_author_and_published_time_key_a_row_without_a_url():
    row = {**_ITEM, "url": ""}
    spaced = {**row, "text": "  Stage two from\nsix tonight. "}
    assert observation_id(row) == observation_id(spaced)
    assert observation_id(row) != observation_id({**row, "author_handle": "other"})
    assert observation_id(row) != observation_id(
        {**row, "published_at": datetime(2026, 9, 8, 6, tzinfo=UTC)}
    )


def test_the_same_text_from_another_source_or_market_is_another_observation():
    row = {**_ITEM, "url": ""}
    ids = {
        observation_id(row),
        observation_id({**row, "source": "gdelt"}),
        observation_id({**row, "platform": "news"}),
        observation_id({**row, "market": "ng"}),
        observation_id({**_ITEM, "market": "ng"}),
    }
    assert len(ids) == 5


def test_the_same_source_with_other_content_is_another_observation():
    assert observation_id(_ITEM) != observation_id(
        {**_ITEM, "url": "https://news.example.test/a/2"}
    )
    row = {**_ITEM, "url": ""}
    assert observation_id(row) != observation_id({**row, "text": "Stage four from six."})


def test_a_row_with_no_stable_key_gets_no_derived_id():
    bare = {"source": "rss", "platform": "web", "market": "za", "url": "", "title": ""}
    assert observation_id(bare) is None
    assert observation_id({**bare, "url": "not a url"}) is None


def test_the_scheme_is_named_in_the_id_and_a_stored_uuid4_still_reads():
    derived = observation_id(_ITEM)
    assert observation_id_scheme(derived) == OBSERVATION_ID_SCHEME == "obs1"
    assert observation_id_scheme(str(uuid.uuid4())) == "uuid4"
    assert observation_id_scheme("obs2_" + derived.split("_", 1)[1]) is None
    assert observation_id_scheme("") is None


def test_a_stored_uuid4_and_a_derived_id_are_both_admitted_without_a_rewrite():
    from tests.unit.test_general_question_context_recollection import _POST, _admit

    legacy = str(uuid.uuid4())
    derived = observation_id({**_POST, "platform": "reddit"})
    first = {**_POST, "collected_at": "2026-09-07T01:00:00+00:00"}
    second = {
        **_POST,
        "url": "https://example.test/two",
        "collected_at": "2026-09-07T02:00:00+00:00",
    }
    result = _admit([(legacy, first, 1), (derived, second, 1)])
    assert len(result["receipts"]) == 2
    assert sorted(key[2] for key in result["observation_keys"]) == sorted([legacy, derived])


def test_query_parameter_order_does_not_change_the_id():
    first = {**_ITEM, "url": "https://news.example.test/a/1?a=1&b=2"}
    second = {**_ITEM, "url": "https://news.example.test/a/1?b=2&a=1"}
    assert observation_id(first) == observation_id(second)
    assert observation_id(first) != observation_id(
        {**_ITEM, "url": "https://news.example.test/a/1?a=2&b=1"}
    )


def test_a_native_id_is_a_string_or_an_integer_and_nothing_else():
    base = {**_ITEM, "platform": "tiktok"}
    assert observation_id({**base, "native_id": 7301}) == observation_id(
        {**base, "native_id": "7301"}
    )
    # Two 19 digit ids that round to one float must not meet as native keys.
    near, far = 7301000000000000001, 7301000000000000002
    assert float(near) == float(far)
    assert observation_id({**base, "native_id": near}) != observation_id({**base, "native_id": far})
    by_url = observation_id(base)
    for unusable in (float(near), 7301.0, True, False, b"7301", ["7301"], {"id": "7301"}):
        assert observation_id({**base, "native_id": unusable}) == by_url, unusable
    assert observation_id({**base, "native_id": float(near), "url": ""}) == observation_id(
        {**base, "native_id": float(far), "url": ""}
    )


def test_a_numeric_published_time_is_read_as_epoch_seconds_in_utc():
    row = {**_ITEM, "url": ""}
    instant = datetime(2026, 9, 7, 6, tzinfo=UTC)
    seconds = instant.timestamp()
    assert observation_id({**row, "published_at": int(seconds)}) == observation_id(row)
    assert observation_id({**row, "published_at": seconds}) == observation_id(row)
    assert observation_id({**row, "published_at": int(seconds)}) != observation_id(
        {**row, "published_at": int(seconds) + 3600}
    )
    assert observation_id({**row, "published_at": 1.5e9}) != observation_id(
        {**row, "published_at": 1.6e9}
    )
    assert observation_id({**row, "published_at": 1.5e9}) != observation_id(
        {**row, "published_at": None}
    )


def test_the_text_key_keeps_case_and_normalises_whitespace_only():
    row = {**_ITEM, "url": "", "title": "", "text": "US policy"}
    assert observation_id(row) != observation_id({**row, "text": "us policy"})
    assert observation_id(row) == observation_id({**row, "text": "  US \n policy\t"})
    assert observation_id({**row, "author_handle": "Desk"}) != observation_id(row)


def test_an_uppercase_uuid4_reads_as_a_stored_uuid4():
    value = str(uuid.uuid4())
    assert observation_id_scheme(value.upper()) == "uuid4"
    assert observation_id_scheme(value.replace("-", "")) is None
    assert observation_id_scheme("{" + value + "}") is None


def test_the_docstring_names_the_url_forms_that_are_not_merged():
    import src.ingestion.observation_id as kernel

    text = " ".join(kernel.__doc__.split())
    assert "NOT merged" in text
    for form in ("trailing slash", "www", "http or https", "percent-encoding"):
        assert form in text, form
    assert observation_id({**_ITEM, "url": "https://news.example.test/a/1/"}) != (
        observation_id(_ITEM)
    )
    assert observation_id({**_ITEM, "url": "https://www.news.example.test/a/1"}) != (
        observation_id(_ITEM)
    )
    assert observation_id({**_ITEM, "url": "http://news.example.test/a/1"}) != (
        observation_id(_ITEM)
    )
    assert observation_id({**_ITEM, "url": "https://news.example.test/a/%31"}) != (
        observation_id(_ITEM)
    )


def test_a_non_empty_fragment_is_kept_so_comments_on_one_post_stay_apart():
    from src.analysis.open_intelligence.observation_id_projection import (
        observation_id_projection,
    )

    post = "https://www.youtube.com/watch?v=abc"
    comments = [
        {**_ITEM, "id": f"u{n}", "platform": "youtube", "url": f"{post}#comment-{n}"}
        for n in (1, 2)
    ]
    assert observation_id(comments[0]) != observation_id(comments[1])
    assert observation_id({**_ITEM, "url": post + "#"}) == observation_id({**_ITEM, "url": post})
    value = observation_id_projection(comments)
    assert value["units"]["distinct_observation_ids"] == 2
    assert value["units"]["recollected_observation_ids"] == 0


def test_a_numpy_integer_or_an_integral_decimal_is_an_integer_native_id():
    import numpy

    base = {**_ITEM, "platform": "tiktok"}
    expected = observation_id({**base, "native_id": "7301000000000000001"})
    for value in (
        numpy.int64(7301000000000000001),
        numpy.uint64(7301000000000000001),
        Decimal("7301000000000000001"),
        Decimal("7301000000000000001.000"),
    ):
        assert observation_id({**base, "native_id": value}) == expected, value
    by_url = observation_id(base)
    for value in (Decimal("7301.5"), Decimal("NaN"), Decimal("Infinity"), numpy.float64(7301)):
        assert observation_id({**base, "native_id": value}) == by_url, value


def test_a_missing_published_time_of_any_kind_reads_as_absent():
    import numpy
    import pandas

    row = {**_ITEM, "url": ""}
    absent = observation_id({**row, "published_at": None})
    for value in (pandas.NaT, float("nan"), numpy.nan, numpy.float64("nan"), pandas.NA):
        assert observation_id({**row, "published_at": value}) == absent, value


def test_an_all_digit_published_string_is_epoch_seconds_and_13_digits_are_milliseconds():
    row = {**_ITEM, "url": ""}
    seconds = int(datetime(2026, 9, 7, 6, tzinfo=UTC).timestamp())
    expected = observation_id(row)
    assert observation_id({**row, "published_at": str(seconds)}) == expected
    assert observation_id({**row, "published_at": f" {seconds} "}) == expected
    millis = seconds * 1000
    assert len(str(millis)) == 13
    assert observation_id({**row, "published_at": millis}) == expected
    assert observation_id({**row, "published_at": str(millis)}) == expected
    assert observation_id({**row, "published_at": float(millis)}) == expected
    later = observation_id({**row, "published_at": millis + 250})
    assert later == observation_id({**row, "published_at": str(millis + 250)})
    assert later != expected


def test_the_author_key_names_whether_it_is_a_handle_or_a_name():
    row = {**_ITEM, "url": "", "author_handle": None, "author_name": None}
    assert observation_id({**row, "author_handle": "a"}) != observation_id(
        {**row, "author_name": "a"}
    )


def test_a_text_only_row_with_no_author_and_no_time_gets_no_derived_id():
    gm = {"source": "x", "platform": "x", "market": "za", "text": "gm", "url": ""}
    assert observation_id(gm) is None
    assert observation_id({**gm, "published_at": float("nan"), "author_handle": " "}) is None
    assert observation_id({**gm, "author_handle": "a"}) is not None
    assert observation_id({**gm, "published_at": "2026-09-07T06:00:00+00:00"}) is not None


def test_repeated_query_keys_keep_their_order_while_keys_are_sorted():
    def at(query):
        return observation_id({**_ITEM, "url": f"https://news.example.test/a/1?{query}"})

    assert at("b=1&a=2&a=1") == at("a=2&a=1&b=1")
    assert at("a=2&a=1") != at("a=1&a=2")


def test_only_10_and_13_digit_values_are_epoch_and_8_digits_are_a_date():
    row = {**_ITEM, "url": ""}

    def at(value):
        return observation_id({**row, "published_at": value})

    midnight = datetime(2026, 9, 7, tzinfo=UTC)
    assert at("20260907") == at(midnight)
    assert at("20260907") != at(20260907)
    assert at(20260907) != at(datetime.fromtimestamp(20260907, UTC))
    assert at("20261399") != at(datetime(2026, 12, 31, tzinfo=UTC))
    assert at("20261399") == at(20261399)
    seconds = int(midnight.timestamp())
    assert len(str(seconds)) == 10
    assert at(str(seconds)) == at(seconds) == at(midnight)
    assert at(str(seconds * 1000)) == at(seconds * 1000) == at(midnight)
    for digits in ("123", "123456789", "12345678901", "123456789012345"):
        assert at(digits) == at(int(digits)), digits
        assert at(digits) != at(None), digits
        assert at(digits) != at(datetime.fromtimestamp(0, UTC)), digits
    assert at("123456789") != at(datetime.fromtimestamp(123456789, UTC))
