from __future__ import annotations

import importlib
import importlib.util
from datetime import date, timedelta

import pytest


def history_module():
    name = "src.analysis.open_intelligence.history_normalizer"
    assert importlib.util.find_spec(name) is not None, "history normalizer module is missing"
    return importlib.import_module(name)


START = date(2026, 7, 1)


def row(
    module,
    day: int,
    value: float,
    *,
    vendor: str,
    regime: str,
    platform: str = "social",
    market: str = "za",
    mode: str = "search",
    discovery: str = "discovered",
    geo: float = 0.9,
    quality: float = 0.9,
):
    return module.HistoricalObservation(
        metric_date=START + timedelta(days=day),
        market=market,
        collection_vendor=vendor,
        platform=platform,
        collection_mode=mode,
        seeded_or_discovered=discovery,
        geo_confidence=geo,
        evidence_quality=quality,
        source_regime=regime,
        value=value,
    )


def rules(module, **overrides):
    values = {
        "minimum_overlap_days": 3,
        "minimum_geo_confidence": 0.7,
        "minimum_evidence_quality": 0.7,
        "baseline_window_days": 2,
        "maximum_overlap_ratio_deviation": 0.1,
    }
    values.update(overrides)
    return module.HistoryNormalizationRules(**values)


def test_vendor_change_does_not_look_like_cultural_collapse():
    module = history_module()
    rows = [
        *(row(module, day, 100, vendor="brand24", regime="brand24_legacy") for day in range(5)),
        *(
            row(module, day, 50, vendor="socialcrawl", regime="socialcrawl_v1")
            for day in range(2, 7)
        ),
    ]

    result = module.normalize_vendor_regimes(rows, rules(module))

    assert result.state == "ready"
    assert result.platform_adjusted_delta == pytest.approx(0.0)
    assert len(result.bridges) == 1
    assert result.bridges[0].from_regime == "brand24_legacy"
    assert result.bridges[0].to_regime == "socialcrawl_v1"
    assert result.bridges[0].overlap_days == 3
    assert result.bridges[0].scale_factor == pytest.approx(2.0)
    assert result.points[0].index_value == pytest.approx(100.0)
    assert result.points[-1].index_value == pytest.approx(100.0)


def test_real_change_survives_vendor_normalization():
    module = history_module()
    rows = [
        *(row(module, day, 100, vendor="brand24", regime="brand24_legacy") for day in range(5)),
        *(
            row(module, day, 50, vendor="socialcrawl", regime="socialcrawl_v1")
            for day in range(2, 5)
        ),
        row(module, 5, 75, vendor="socialcrawl", regime="socialcrawl_v1"),
        row(module, 6, 75, vendor="socialcrawl", regime="socialcrawl_v1"),
    ]

    result = module.normalize_vendor_regimes(rows, rules(module))

    assert result.state == "ready"
    assert result.platform_adjusted_delta == pytest.approx(0.5)
    assert result.points[-1].index_value == pytest.approx(150.0)


@pytest.mark.parametrize("overlap_days", [0, 2])
def test_missing_or_thin_overlap_fails_closed(overlap_days):
    module = history_module()
    old_days = range(5)
    new_start = 5 - overlap_days
    rows = [
        *(row(module, day, 100, vendor="old", regime="old") for day in old_days),
        *(row(module, day, 50, vendor="new", regime="new") for day in range(new_start, 8)),
    ]

    result = module.normalize_vendor_regimes(rows, rules(module))

    assert result.state == "unbridgeable"
    assert result.platform_adjusted_delta is None
    assert result.points == ()
    assert "insufficient_regime_overlap" in result.gaps


def test_low_quality_rows_do_not_create_a_bridge():
    module = history_module()
    rows = [
        row(module, 0, 100, vendor="old", regime="old", geo=0.4),
        row(module, 1, 50, vendor="new", regime="new", quality=0.4),
    ]

    result = module.normalize_vendor_regimes(rows, rules(module))

    assert result.state == "unchecked"
    assert result.points == ()
    assert result.excluded_rows == 2
    assert result.gaps == ("no_qualified_history",)


def test_each_platform_is_indexed_before_cross_platform_aggregation():
    module = history_module()
    rows = []
    for platform, old_value, new_value in (("tiktok", 1000, 1000), ("news", 10, 20)):
        rows.extend(
            row(module, day, old_value, vendor="stable", regime="stable", platform=platform)
            for day in range(2)
        )
        rows.extend(
            row(module, day, new_value, vendor="stable", regime="stable", platform=platform)
            for day in range(2, 4)
        )

    result = module.normalize_vendor_regimes(rows, rules(module, minimum_overlap_days=1))

    assert result.state == "ready"
    assert result.points[-1].index_value == pytest.approx(150.0)
    assert result.platform_adjusted_delta == pytest.approx(0.5)


def test_duplicate_regime_date_identity_is_rejected():
    module = history_module()
    duplicate = row(module, 0, 10, vendor="vendor", regime="regime")

    with pytest.raises(ValueError, match="duplicate historical observation identity"):
        module.normalize_vendor_regimes((duplicate, duplicate), rules(module))


def test_markets_cannot_be_blended_inside_one_history_series():
    module = history_module()
    rows = (
        row(module, 0, 10, vendor="vendor", regime="regime", market="za"),
        row(module, 0, 10, vendor="vendor", regime="regime", market="ng"),
    )

    with pytest.raises(ValueError, match="history series must contain one market"):
        module.normalize_vendor_regimes(rows, rules(module))


def test_one_source_regime_cannot_alias_multiple_vendors():
    module = history_module()
    rows = (
        row(module, 0, 10, vendor="vendor_a", regime="shared_regime"),
        row(module, 1, 10, vendor="vendor_b", regime="shared_regime"),
    )

    with pytest.raises(ValueError, match="source regime must map to one vendor"):
        module.normalize_vendor_regimes(rows, rules(module))


def test_platform_entry_or_exit_cannot_create_a_cross_platform_delta():
    module = history_module()
    rows = (
        row(module, 0, 1000, vendor="a", regime="a", platform="tiktok"),
        row(module, 1, 1000, vendor="a", regime="a", platform="tiktok"),
        row(module, 2, 10, vendor="b", regime="b", platform="news"),
        row(module, 3, 10, vendor="b", regime="b", platform="news"),
    )

    result = module.normalize_vendor_regimes(rows, rules(module, minimum_overlap_days=1))

    assert result.state == "unbridgeable"
    assert result.points == ()
    assert result.platform_adjusted_delta is None
    assert result.gaps == ("platform_coverage_not_comparable",)


def test_asymmetric_zero_overlap_fails_closed():
    module = history_module()
    rows = [
        *(row(module, day, 100, vendor="old", regime="old") for day in range(4)),
        *(row(module, day, 50, vendor="new", regime="new") for day in range(1, 4)),
        row(module, 4, 0, vendor="old", regime="old"),
        row(module, 4, 50, vendor="new", regime="new"),
    ]

    result = module.normalize_vendor_regimes(rows, rules(module))

    assert result.state == "unbridgeable"
    assert result.points == ()
    assert result.gaps == ("asymmetric_zero_overlap",)


def test_unstable_overlap_ratios_fail_closed():
    module = history_module()
    rows = [
        *(row(module, day, 100, vendor="old", regime="old") for day in range(5)),
        row(module, 1, 40, vendor="new", regime="new"),
        row(module, 2, 50, vendor="new", regime="new"),
        row(module, 3, 60, vendor="new", regime="new"),
        row(module, 4, 60, vendor="new", regime="new"),
    ]

    result = module.normalize_vendor_regimes(rows, rules(module))

    assert result.state == "unbridgeable"
    assert result.platform_adjusted_delta is None
    assert result.gaps == ("unstable_regime_overlap",)


def test_partial_middle_only_platform_coverage_fails_closed():
    module = history_module()
    rows = [
        *(row(module, day, 100, vendor="a", regime="a", platform="tiktok") for day in range(6)),
        row(module, 2, 10, vendor="b", regime="b", platform="news"),
        row(module, 3, 10, vendor="b", regime="b", platform="news"),
    ]

    result = module.normalize_vendor_regimes(rows, rules(module, minimum_overlap_days=1))

    assert result.state == "unbridgeable"
    assert result.gaps == ("platform_coverage_not_comparable",)


def test_multiple_collection_modes_count_as_one_physical_platform():
    module = history_module()
    rows = []
    for mode in ("search", "profile"):
        rows.extend(
            row(
                module,
                day,
                100 if day < 2 else 200,
                vendor="socialcrawl",
                regime="socialcrawl_v1",
                platform="tiktok",
                mode=mode,
            )
            for day in range(4)
        )
    rows.extend(
        row(
            module,
            day,
            10,
            vendor="newswire",
            regime="newswire_v1",
            platform="news",
        )
        for day in range(4)
    )

    result = module.normalize_vendor_regimes(rows, rules(module, minimum_overlap_days=1))

    assert result.state == "ready"
    assert result.points[-1].contributing_platforms == 2
    assert result.points[-1].index_value == pytest.approx(150.0)
    assert result.platform_adjusted_delta == pytest.approx(0.5)


def test_extreme_finite_values_cannot_create_nonfinite_ready_output():
    module = history_module()
    rows = [
        *(row(module, day, 1e308, vendor="old", regime="old") for day in range(4)),
        *(row(module, day, 5e-324, vendor="new", regime="new") for day in range(1, 4)),
    ]

    result = module.normalize_vendor_regimes(rows, rules(module))

    assert result.state == "unbridgeable"
    assert result.points == ()
    assert result.gaps == ("nonfinite_regime_adjustment",)


def test_equal_regime_start_dates_are_ambiguous():
    module = history_module()
    rows = [
        *(row(module, day, 100, vendor="a", regime="a") for day in range(3)),
        *(row(module, day, 50, vendor="b", regime="b") for day in range(3)),
    ]

    result = module.normalize_vendor_regimes(rows, rules(module))

    assert result.state == "unbridgeable"
    assert result.gaps == ("ambiguous_regime_order",)


def test_chained_regime_bridges_carry_cumulative_scale():
    module = history_module()
    rows = [
        *(row(module, day, 100, vendor="a", regime="a") for day in range(5)),
        *(row(module, day, 50, vendor="b", regime="b") for day in range(2, 7)),
        *(row(module, day, 25, vendor="c", regime="c") for day in range(4, 9)),
    ]

    result = module.normalize_vendor_regimes(rows, rules(module))

    assert result.state == "ready"
    assert [bridge.scale_factor for bridge in result.bridges] == pytest.approx([2.0, 4.0])
    assert result.platform_adjusted_delta == pytest.approx(0.0)
    assert all(point.index_value == pytest.approx(100.0) for point in result.points)


def test_nonadjacent_regime_asymmetric_zero_fails_closed():
    module = history_module()
    rows = [
        *(row(module, day, 100, vendor="a", regime="a") for day in range(5)),
        row(module, 7, 0, vendor="a", regime="a"),
        *(row(module, day, 50, vendor="b", regime="b") for day in range(2, 7)),
        *(row(module, day, 25, vendor="c", regime="c") for day in range(4, 9)),
    ]

    result = module.normalize_vendor_regimes(rows, rules(module))

    assert result.state == "unbridgeable"
    assert result.points == ()
    assert result.gaps == ("asymmetric_zero_overlap",)


def test_nonadjacent_regime_scaled_residual_breach_fails_closed():
    module = history_module()
    rows = [
        *(row(module, day, 100, vendor="a", regime="a") for day in range(5)),
        row(module, 7, 100, vendor="a", regime="a"),
        *(row(module, day, 50, vendor="b", regime="b") for day in range(2, 7)),
        *(row(module, day, 25, vendor="c", regime="c") for day in range(4, 7)),
        row(module, 7, 50, vendor="c", regime="c"),
        row(module, 8, 50, vendor="c", regime="c"),
    ]

    result = module.normalize_vendor_regimes(rows, rules(module))

    assert result.state == "unbridgeable"
    assert result.points == ()
    assert result.gaps == ("unstable_regime_overlap",)


def test_published_as_of_excludes_evidence_unavailable_then():
    from dataclasses import replace
    from datetime import UTC, datetime

    module = history_module()
    as_of = datetime(2026, 7, 10, tzinfo=UTC)

    def available(day, value):
        item = row(module, day, value, vendor="a", regime="a")
        return replace(item, available_at=datetime(2026, 7, 2 + day, tzinfo=UTC))

    series = tuple(available(day, 10.0) for day in range(6))
    late = replace(
        row(module, 3, 1000.0, vendor="a", regime="a"),
        available_at=datetime(2026, 7, 20, tzinfo=UTC),
    )

    published = module.normalize_vendor_regimes((*series, late), rules(module), as_of=as_of)
    reference = module.normalize_vendor_regimes(series, rules(module), as_of=as_of)

    assert published.state == "ready"
    assert published.points == reference.points
    assert published.excluded_rows == reference.excluded_rows == 0
    assert published.unavailable_rows == 1
    assert reference.unavailable_rows == 0
    with pytest.raises(ValueError, match="duplicate historical observation identity"):
        module.normalize_vendor_regimes((*series, late), rules(module))
    with pytest.raises(ValueError, match="availability is required"):
        module.normalize_vendor_regimes(
            (*series, row(module, 6, 10.0, vendor="a", regime="a")), rules(module), as_of=as_of
        )
    with pytest.raises(ValueError, match="as_of must be timezone aware"):
        module.normalize_vendor_regimes(series, rules(module), as_of=datetime(2026, 7, 10))
