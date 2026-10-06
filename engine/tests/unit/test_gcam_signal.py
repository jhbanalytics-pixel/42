"""Unit tests for the GCAM-reader signal parser (enrichment._parse_gcam_intensity).

Proves the v2gcam code:value string is turned into a 0..1 emotional-intensity
float: only the Lexicoder emotion dims (v19.1, v19.9) count, each is min-max
normalised against its calibrated bounds and clamped, the result is the mean of
the dims actually present, and anything empty / unparseable / dimless is NaN so
aggregation excludes it.
"""

from __future__ import annotations

import math

from src.ingestion.enrichment import _GCAM_INTENSITY_BOUNDS, _parse_gcam_intensity


def test_empty_or_none_returns_nan():
    assert math.isnan(_parse_gcam_intensity(""))
    assert math.isnan(_parse_gcam_intensity(None))
    assert math.isnan(_parse_gcam_intensity("   "))


def test_no_target_dim_returns_nan():
    # Only non-target dims present (v10.1 happiness, v20.1 polarity are excluded).
    assert math.isnan(_parse_gcam_intensity("v10.1:0.3,v20.1:0.5"))


def test_single_dim_uses_only_that_dim():
    # v19.1 bounds (3.713, 6.536). Midpoint value -> ~0.5, mean of one dim = itself.
    lo, hi = _GCAM_INTENSITY_BOUNDS["v19.1"]
    mid = lo + (hi - lo) * 0.5
    assert _parse_gcam_intensity(f"v19.1:{mid}") == 0.5


def test_value_at_low_bound_is_zero():
    lo, _ = _GCAM_INTENSITY_BOUNDS["v19.1"]
    assert _parse_gcam_intensity(f"v19.1:{lo}") == 0.0


def test_value_above_high_bound_clamps_to_one():
    _, hi = _GCAM_INTENSITY_BOUNDS["v19.9"]
    assert _parse_gcam_intensity(f"v19.9:{hi + 100.0}") == 1.0


def test_value_below_low_bound_clamps_to_zero():
    lo, _ = _GCAM_INTENSITY_BOUNDS["v19.1"]
    assert _parse_gcam_intensity(f"v19.1:{lo - 100.0}") == 0.0


def test_both_dims_averaged():
    # v19.1 at its low bound (norm 0.0), v19.9 at its high bound (norm 1.0) -> 0.5.
    lo1, _ = _GCAM_INTENSITY_BOUNDS["v19.1"]
    _, hi9 = _GCAM_INTENSITY_BOUNDS["v19.9"]
    assert _parse_gcam_intensity(f"v19.1:{lo1},v19.9:{hi9}") == 0.5


def test_real_world_string_in_range():
    # A real persisted string from the 2026-06-25 cron. Both dims near p50 ->
    # an intensity comfortably inside 0..1.
    val = _parse_gcam_intensity("v10.1:0.263,v10.2:0.262,v19.1:5.776,v19.9:5.196,v20.1:0.47")
    assert 0.0 < val < 1.0


def test_malformed_tokens_skipped():
    # A junk token next to one valid dim: the valid dim still scores.
    lo, hi = _GCAM_INTENSITY_BOUNDS["v19.1"]
    mid = lo + (hi - lo) * 0.5
    assert _parse_gcam_intensity(f"garbage,v19.1:{mid},v19.9:notanumber") == 0.5
