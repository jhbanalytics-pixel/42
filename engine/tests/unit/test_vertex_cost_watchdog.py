"""Unit tests for scripts/vertex_cost_watchdog.py pure helpers.

The BQ boundary is replaced with deterministic captures, so the real query
builders are exercised without a network call.

The watchdog now costs off ONE basis, the gemini_usage ledger, and checks its
own total against the billing export. There is no estimated consumer left.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import scripts.vertex_cost_watchdog as w
from scripts.vertex_cost_watchdog import (
    ALERT_THRESHOLD_USD_PER_MONTH,
    BILLING_LAG_DAYS,
    KNOWN_UNMETERED_VERTEX_BILLERS,
    WATCH_THRESHOLD_USD_PER_MONTH,
    ConsumerSpend,
    CostReport,
    closed_window,
    cost_usd,
    discrepancy_notes,
    ledger_cost_by_day,
    missing_ledger_consumers,
    price_for,
    reconcile_against_billing,
    usage_ledger_spends,
    verdict,
)
from src.utils.gemini_usage import USAGE_CONSUMERS

# --- pricing ---------------------------------------------------------------


def test_price_for_known_and_unknown():
    assert price_for("gemini-3.5-flash") == (1.50, 9.00)
    assert price_for("gemini-2.5-flash") == (0.30, 2.50)
    # Unknown or NULL id falls back to 3.5-flash so the ledger never
    # under-reports.
    assert price_for("some-future-model") == (1.50, 9.00)
    assert price_for(None) == (1.50, 9.00)


def test_cost_usd_35_flash():
    # 1M in + 1M out at 3.5-flash = $1.50 + $9.00.
    assert cost_usd("gemini-3.5-flash", 1_000_000, 1_000_000) == 10.50


# --- the consumer set ------------------------------------------------------


def test_watchdog_expects_every_declared_consumer():
    """The watchdog's expected set IS the writers' declared set, not a copy.

    A hand-maintained second list is how a stage ends up billing and never
    being counted, which is the failure this whole file exists to prevent.
    """
    assert w.EXPECTED_CONSUMERS is USAGE_CONSUMERS
    assert "reconcile" in w.EXPECTED_CONSUMERS
    assert "trend_analysis" in w.EXPECTED_CONSUMERS


def test_no_consumer_is_estimated_any_more():
    # reconcile used to be a hardcoded per-call token guess. Every consumer is
    # measured off the ledger now, so the estimate machinery is gone.
    assert not hasattr(w, "reconcile_spend")
    assert not hasattr(w, "RECONCILE_EST_PROMPT_TOKENS")
    assert "estimated" not in ConsumerSpend.__dataclass_fields__


# --- ledger grouping -------------------------------------------------------


def _ledger_rows():
    """gemini_usage rows as the BQ read hands them over: one per
    (consumer, model, day)."""
    return [
        {
            "consumer": "comment_sentiment",
            "model": "gemini-3.5-flash",
            "day": "2026-08-18",
            "calls": 22,
            "prompt_in": 400_000,
            "prompt_out": 40_000,
        },
        {
            "consumer": "comment_sentiment",
            "model": "gemini-3.5-flash",
            "day": "2026-08-19",
            "calls": 21,
            "prompt_in": 380_000,
            "prompt_out": 38_000,
        },
        {
            "consumer": "driving_hashtags",
            "model": "gemini-3.5-flash",
            "day": "2026-08-19",
            "calls": 90,
            "prompt_in": 900_000,
            "prompt_out": 45_000,
        },
    ]


def test_usage_ledger_groups_per_consumer_and_prices_real_tokens():
    spends = usage_ledger_spends(_ledger_rows())
    assert [c.name for c in spends] == ["comment_sentiment", "driving_hashtags"]
    room, tags = spends
    # Two ledger rows for the room card fold into one consumer line.
    assert (room.calls, room.active_days) == (43, 2)
    assert (room.prompt_tokens, room.completion_tokens) == (780_000, 78_000)
    assert room.window_cost == pytest.approx(cost_usd("gemini-3.5-flash", 780_000, 78_000))
    # The per-tag stage fires the most calls of the two.
    assert tags.calls > room.calls


def test_usage_ledger_prices_each_model_at_its_own_rate():
    rows = [
        {
            "consumer": "reconcile",
            "model": "gemini-3.5-flash",
            "day": "d1",
            "calls": 1,
            "prompt_in": 1_000_000,
            "prompt_out": 0,
        },
        {
            "consumer": "reconcile",
            "model": "gemini-2.5-flash",
            "day": "d1",
            "calls": 1,
            "prompt_in": 1_000_000,
            "prompt_out": 0,
        },
    ]
    (spend,) = usage_ledger_spends(rows)
    # The reconcile shadow resolves on the cheaper model, so a window spanning
    # a cutover must not price both sides at one rate.
    assert spend.window_cost == pytest.approx(1.50 + 0.30)


def test_usage_ledger_survives_null_token_columns():
    # prompt_tokens is nullable and a SUM() over an all-NULL group arrives as
    # NaN through pandas. int(nan or 0) raises, because NaN is truthy, so the
    # aggregator must not use that idiom.
    rows = [
        {
            "consumer": "comment_sentiment",
            "model": None,
            "day": "2026-08-19",
            "calls": float("nan"),
            "prompt_in": None,
            "prompt_out": float("nan"),
        }
    ]
    (spend,) = usage_ledger_spends(rows)
    assert (spend.calls, spend.prompt_tokens, spend.completion_tokens) == (0, 0, 0)
    assert spend.window_cost == 0.0


def test_usage_ledger_surfaces_an_unknown_consumer():
    rows = [
        *_ledger_rows(),
        {
            "consumer": "some_new_stage",
            "model": "gemini-3.5-flash",
            "day": "2026-08-19",
            "calls": 4,
            "prompt_in": 10_000,
            "prompt_out": 1_000,
        },
    ]
    # A writer nobody declared still shows up, rather than being dropped.
    assert [c.name for c in usage_ledger_spends(rows)][-1] == "some_new_stage"


def test_missing_ledger_consumer_is_reported_not_read_as_zero():
    spends = usage_ledger_spends(_ledger_rows())
    missing = missing_ledger_consumers(spends)
    # These declared consumers wrote nothing in this fixture window.
    assert missing == [
        "trend_analysis",
        "daily_summary",
        "seed_insights",
        "reconcile",
        "dynamic_signal_summary",
        "open_question_answer",
    ]
    assert missing_ledger_consumers([]) == list(USAGE_CONSUMERS)


def test_consumer_projection_off_active_days():
    # A consumer that fired only 4 of the last 7 days must project off its 4
    # active days, not the window length, so a ramping consumer is not diluted.
    c = ConsumerSpend("reconcile", 12, 4, 0, 0, 4.0)
    assert c.monthly_cost == (4.0 / 4) * 30
    # Zero active days must not divide by zero.
    assert ConsumerSpend("x", 0, 0, 0, 0, 0.0).monthly_cost == 0.0


# --- the closed-day comparison window --------------------------------------


def test_closed_window_excludes_the_days_billing_has_not_settled():
    start, end = closed_window(7, date(2026, 8, 24))
    assert end == date(2026, 8, 24 - BILLING_LAG_DAYS)
    assert start == date(2026, 8, 17)
    # Comparing a lagging export against a live ledger on today's date reads a
    # settled-but-empty billing day as a discrepancy that is not real.
    assert end < date(2026, 8, 24)


def test_ledger_cost_by_day_sums_every_consumer_at_its_own_model_rate():
    by_day = ledger_cost_by_day(_ledger_rows())
    assert set(by_day) == {"2026-08-18", "2026-08-19"}
    assert by_day["2026-08-18"] == pytest.approx(cost_usd("gemini-3.5-flash", 400_000, 40_000))
    # Two consumers share 08-19; the day total is both of them.
    assert by_day["2026-08-19"] == pytest.approx(
        cost_usd("gemini-3.5-flash", 380_000, 38_000)
        + cost_usd("gemini-3.5-flash", 900_000, 45_000)
    )


def test_ledger_window_covers_exactly_the_days_it_claims():
    """A 7-day window must read 7 dates, not 8.

    `trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)` with an inclusive
    upper bound spans EIGHT dates over a DATE column, which inflates the window
    total against its own label while leaving the per-consumer projection
    untouched, so it reads as a discrepancy with no cause.
    """
    import re

    sqls = []

    class Frame:
        empty = False

        def __init__(self, records):
            self.records = records

        def to_dict(self, orient):
            assert orient == "records"
            return self.records

    def capture(q):
        sqls.append(q)
        if len(sqls) == 1:
            return Frame(
                [
                    {"column_name": "run_id"},
                    {"column_name": "stage"},
                    {"column_name": "call_index"},
                ]
            )
        return Frame([])

    original = w._run_query
    w._run_query = capture
    try:
        w.fetch_usage_ledger_rows(7, "p.d")
    finally:
        w._run_query = original
    usage_sql = sqls[1]
    assert re.search(r"trend_date > DATE_SUB\(CURRENT_DATE\(\), INTERVAL 7 DAY\)", usage_sql)
    assert "trend_date >= DATE_SUB" not in usage_sql
    assert "LIMIT" not in usage_sql.upper()


# --- the billing reconciliation -------------------------------------------


def test_reconcile_compares_only_days_both_sides_have():
    ledger = {"d1": 1.00, "d2": 1.00, "d3": 1.00}
    billing = {"d1": 1.05, "d2": 1.05}
    d = reconcile_against_billing(ledger, billing)
    # d3 has no billing row yet, so including it would invent a phantom gap.
    assert d.days_compared == 2
    assert d.ledger_cost == pytest.approx(2.00)
    assert d.billing_cost == pytest.approx(2.10)
    assert d.within_tolerance is True


def test_reconcile_flags_a_real_metering_gap():
    # The measured 2026-08-24 shape: the ledger sees roughly two thirds of what
    # Vertex actually billed.
    d = reconcile_against_billing({"d1": 4.73}, {"d1": 7.00})
    assert d.gap_usd == pytest.approx(2.27)
    assert d.gap_pct == pytest.approx(2.27 / 7.00)
    assert d.within_tolerance is False


def test_reconcile_ignores_a_ledger_total_above_billing():
    # The ledger costing MORE than billing is not a metering gap; it means the
    # rate table is stale or a credit landed. Flagging it as an under-count
    # would send the reader hunting for a consumer that is not missing.
    d = reconcile_against_billing({"d1": 9.00}, {"d1": 7.00})
    assert d.gap_usd == pytest.approx(0.0)
    assert d.within_tolerance is True


def test_reconcile_with_no_overlapping_days_is_not_a_verdict():
    d = reconcile_against_billing({"d1": 4.0}, {})
    assert d.days_compared == 0
    # Nothing was compared, so nothing is claimed either way.
    assert d.within_tolerance is None


def test_discrepancy_note_names_who_could_be_responsible():
    d = reconcile_against_billing({"d1": 4.73}, {"d1": 7.00})
    notes = discrepancy_notes(d, missing=["comment_sentiment"])
    text = " ".join(notes)
    # The consumer that wrote nothing, and the standing unmetered biller.
    assert "comment_sentiment" in text
    assert "embedding_classifier" in text
    assert "embedding_classifier" in {n for n, _ in KNOWN_UNMETERED_VERTEX_BILLERS}
    assert "$2.27" in text or "2.27" in text


def test_no_discrepancy_note_when_the_ledger_matches_billing():
    d = reconcile_against_billing({"d1": 6.95}, {"d1": 7.00})
    assert discrepancy_notes(d, missing=[]) == []


# --- the report ------------------------------------------------------------


def _six_consumers():
    return [
        ConsumerSpend("trend_analysis", 168, 7, 0, 0, 3.00),
        ConsumerSpend("daily_summary", 7, 7, 0, 0, 0.22),
        ConsumerSpend("seed_insights", 7, 7, 0, 0, 0.15),
        # The room card wrote nothing over the window, which is the shape the
        # live ledger was in on 2026-08-24.
        ConsumerSpend("comment_sentiment", 0, 0, 0, 0, 0.0),
        ConsumerSpend("driving_hashtags", 265, 5, 0, 0, 0.67),
        ConsumerSpend("reconcile", 21, 7, 0, 0, 0.30),
    ]


def test_report_sums_every_consumer_from_one_basis():
    report = CostReport(7, "s", "e", _six_consumers())
    assert len(report.consumers) == 6
    assert report.window_cost == pytest.approx(4.34)
    # Every consumer projects off its OWN active days, so the total is more
    # than the briefs line alone.
    assert report.monthly_cost > (3.00 / 7 * 30)
    d = report.to_dict()
    assert d["projected_monthly"] == round(report.monthly_cost, 2)
    assert "has_estimate" not in d


def test_a_metering_gap_floors_the_status_at_yellow():
    # A GREEN-looking projection that cannot account for what Vertex billed is
    # not GREEN. The number is incomplete, and the report must say so rather
    # than let a quiet under-count read as healthy.
    report = CostReport(7, "s", "e", _six_consumers())
    assert report.status == "GREEN"
    report.discrepancy = reconcile_against_billing({"d1": 4.73}, {"d1": 7.00})
    assert report.status == "YELLOW"


def test_a_real_alert_still_outranks_the_gap_floor():
    report = CostReport(7, "s", "e", [ConsumerSpend("trend_analysis", 1, 1, 0, 0, 40.0)])
    report.discrepancy = reconcile_against_billing({"d1": 4.73}, {"d1": 7.00})
    assert report.status == "RED"


def test_coverage_notes_render_when_a_consumer_cannot_be_counted():
    report = CostReport(7, "s", "e", [ConsumerSpend("trend_analysis", 1, 1, 0, 0, 1.0)])
    assert report.to_dict()["coverage_notes"] == []
    assert "COVERAGE GAP" not in w.render_text(report)
    report.notes.append("gemini_usage unreadable; NOT counted: comment_sentiment")
    assert report.to_dict()["coverage_notes"] == report.notes
    rendered = w.render_text(report)
    assert "COVERAGE GAP" in rendered
    assert "comment_sentiment" in rendered


def test_render_shows_the_billing_comparison():
    report = CostReport(7, "s", "e", _six_consumers())
    report.discrepancy = reconcile_against_billing({"d1": 4.73}, {"d1": 7.00})
    rendered = w.render_text(report)
    assert "BILLING" in rendered
    assert "7.00" in rendered


def test_verdict_thresholds():
    assert verdict(WATCH_THRESHOLD_USD_PER_MONTH - 0.01) == "GREEN"
    assert verdict(WATCH_THRESHOLD_USD_PER_MONTH) == "YELLOW"
    assert verdict(ALERT_THRESHOLD_USD_PER_MONTH - 0.01) == "YELLOW"
    assert verdict(ALERT_THRESHOLD_USD_PER_MONTH) == "RED"
    # The billing export's own 2026-08-24 projection for the Vertex line was
    # $32.89, which reads RED under the unchanged policy threshold. That is the
    # correct outcome, not a threshold to move.
    assert verdict(32.89) == "RED"
