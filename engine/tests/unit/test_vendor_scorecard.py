"""Unit tests for the head-to-head vendor scorecard. No BigQuery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import scripts.vendor_scorecard as sc


def _bq_row(**kw):
    base = {
        "vendor": "socialcrawl",
        "rows_total": 2100,
        "days_seen": 7,
        "platforms": 6,
        "geo_verified": 420,
        "rows_usable": 1890,
        "freshness_hours_median": 12,
        "topic_coverage": 18,
        "corroborated_topics": 9,
        "scored_topics": 20,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _fetch(rows, days=7):
    client = MagicMock()
    client.query.return_value.result.return_value = rows
    fake_bq = MagicMock()
    fake_bq.Client.return_value = client
    fake_bq.ScalarQueryParameter = MagicMock()
    fake_bq.QueryJobConfig = MagicMock()
    with (
        patch.dict("sys.modules", {"google.cloud": MagicMock(bigquery=fake_bq)}),
        patch("google.cloud.bigquery", fake_bq, create=True),
    ):
        return sc.fetch(days)


def test_rows_per_day_divides_by_days_actually_seen():
    out = _fetch([_bq_row(rows_total=2100, days_seen=7)])
    assert out[0].rows_per_day == 300


def test_percentages_are_shares_not_counts():
    out = _fetch(
        [_bq_row(rows_total=2100, geo_verified=420, corroborated_topics=9, scored_topics=20)]
    )
    assert out[0].geo_precision_pct == 20.0
    assert out[0].corroboration_pct == 45.0


def test_row_integrity_is_the_share_of_usable_rows():
    """The quality measure. A row with no date, no author or no url is not
    usable, and must not be allowed to hide inside the volume number."""
    out = _fetch([_bq_row(rows_total=2100, rows_usable=1890)])
    assert out[0].row_integrity_pct == 90.0


def test_render_prints_integrity_directly_under_volume():
    """The two numbers are meaningless apart, so the layout keeps them together."""
    body = sc.render(_fetch([_bq_row()]), 7).splitlines()
    vol = next(i for i, ln in enumerate(body) if ln.strip().startswith("rows/day"))
    assert "of which usable" in body[vol + 1]


def test_zero_rows_does_not_divide_by_zero():
    out = _fetch([_bq_row(rows_total=0, geo_verified=0, scored_topics=0, days_seen=0)])
    assert out[0].rows_per_day == 0
    assert out[0].geo_precision_pct == 0.0
    assert out[0].corroboration_pct == 0.0
    assert out[0].cost_per_1k_rows is None


def test_cost_per_1k_only_computed_for_a_vendor_with_a_known_price():
    ens, scrawl = _fetch([_bq_row(vendor="EnsembleData", rows_total=7000, days_seen=7), _bq_row()])
    # 123 ZAR/day over 1000 rows/day
    assert ens.cost_per_1k_rows == 123.0
    assert scrawl.cost_per_1k_rows is None


def test_render_declares_the_reddit_attribution():
    """Reddit billed the EnsembleData ledger, so the reader must be told it
    counts against the old vendor rather than silently inflating it."""
    out = sc.render(_fetch([_bq_row()]), 7)
    assert "Reddit rows count as EnsembleData" in out


def test_render_survives_an_empty_window():
    assert "no rows" in sc.render([], 7)
