from datetime import date

import pytest

from core.understand import embed


DAY = date(2026, 10, 13)
COUNTS = {"embedded": 1, "failed": 0, "skipped": 0, "chars_sent": 12, "tokens": 4}


@pytest.mark.parametrize("key", embed.COUNTS)
@pytest.mark.parametrize("value", [None, -1, 1.5, True, "4"])
def test_malformed_paid_counts_keep_reservation_and_stop_later_windows(key, value):
    row = {**COUNTS, key: value}
    _refuses_paid_counts([row])


@pytest.mark.parametrize("key", embed.COUNTS)
def test_missing_paid_counts_keep_reservation_and_stop_later_windows(key):
    _refuses_paid_counts([{k: v for k, v in COUNTS.items() if k != key}])


def test_multiple_paid_counts_rows_keep_reservation():
    _refuses_paid_counts([COUNTS, COUNTS])


def _refuses_paid_counts(rows):
    calls, bookings = [], []

    def execute(sql, params):
        calls.append(sql)
        if sql == embed.load("embed"):
            return {"rows": rows}
        if sql == embed.load("embedding_count"):
            return [{"n": 1}]
        return []

    def book(amount, what):
        bookings.append((amount, what))
        return amount

    with pytest.raises(RuntimeError, match="counts row") as error:
        embed.run_embed(execute, run_date=DAY, days=14, slice_limit=10, book=book)
    ceiling = round(10 * embed.POST_CEILING_USD, 6)
    assert bookings == [(ceiling, "embed_ceiling")]
    assert calls.count(embed.load("embed")) == 1
    assert error.value.embed_counts["booked_usd"] == ceiling
    assert error.value.embed_counts["tokens"] == 0


def test_complete_zero_counts_can_release_an_unused_reservation():
    bookings = []

    def execute(sql, params):
        if sql == embed.load("embed"):
            return [dict.fromkeys(embed.COUNTS, 0)]
        if sql == embed.load("embedding_count"):
            return [{"n": 0}]
        return []

    result = embed.run_embed(execute, run_date=DAY, slice_limit=10,
                            book=lambda amount, what: bookings.append((amount, what)) or amount)
    assert result["booked_usd"] == 0
    assert bookings[-1][1] == "embed_correction" and bookings[-1][0] < 0
