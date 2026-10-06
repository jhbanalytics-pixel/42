"""Unit tests for scripts/engine_evolve.py.

Pure-function focus: capacity recalibration math, endpoint discovery
sort + group, coherence flag detection, YAML mutation. BQ pulls are
mocked at the rolling-medians / topic-source-distribution boundaries
so no live BQ needed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch


def _load_evolve_module():
    """Side-load scripts/engine_evolve.py with sys.modules registration
    so dataclasses introspection works under Python 3.11+."""
    if "engine_evolve" in sys.modules:
        return sys.modules["engine_evolve"]
    root = Path(__file__).resolve().parent.parent.parent
    spec = importlib.util.spec_from_file_location(
        "engine_evolve", root / "scripts" / "engine_evolve.py"
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["engine_evolve"] = mod
    spec.loader.exec_module(mod)
    return mod


evolve = _load_evolve_module()


# ----------------------------------------------------------------------
# Loop 1: capacity recalibration math


def test_recalibration_no_change_when_within_threshold():
    """Median within ±20% of current target → no proposal."""
    result = evolve._propose_recalibration(
        "ensembledata",
        current_expected=2600,
        observed_median=2700,  # +3.8% deviation
        per_cycle_bound_pct=20.0,
        threshold_pct=20.0,
    )
    assert result is None


def test_recalibration_propose_uptick_within_bound():
    """Median exceeds threshold but within bound → propose median directly."""
    result = evolve._propose_recalibration(
        "ensembledata",
        current_expected=2600,
        observed_median=3000,  # +15.4% deviation, but above threshold of 10%
        per_cycle_bound_pct=20.0,
        threshold_pct=10.0,
    )
    assert result is not None
    assert result.proposed_expected == 3000
    assert result.bounded is False
    assert result.within_bound is True


def test_recalibration_bounded_when_change_exceeds_bound():
    """Median above bound → proposal capped at +20% per cycle."""
    result = evolve._propose_recalibration(
        "ensembledata",
        current_expected=2600,
        observed_median=5000,  # +92% — way above bound
        per_cycle_bound_pct=20.0,
        threshold_pct=20.0,
    )
    assert result is not None
    assert result.bounded is True
    assert result.proposed_expected == int(2600 * 1.20)  # capped


def test_recalibration_bounded_downward():
    """Median far below bound → proposal capped at -20%."""
    result = evolve._propose_recalibration(
        "ensembledata",
        current_expected=2600,
        observed_median=100,  # massive drop
        per_cycle_bound_pct=20.0,
        threshold_pct=20.0,
    )
    assert result is not None
    assert result.bounded is True
    assert result.proposed_expected == int(2600 * 0.80)


def test_recalibration_bounded_downward_small_value_stays_within_bound():
    """Small current value where int() floor of -20% would breach the bound.

    current=7, -20% is 5.6. int() floor gives 5, a 28.6% drop, which would
    recompute within_bound False and silently skip the proposal the code
    itself bounded. The proposal must remain appliable.
    """
    result = evolve._propose_recalibration(
        "tinysource",
        current_expected=7,
        observed_median=1,
        per_cycle_bound_pct=20.0,
        threshold_pct=20.0,
    )
    assert result is not None
    assert result.bounded is True
    assert result.within_bound is True
    # round(7 * 0.8) == round(5.6) == 6, a 14.3% drop, inside the bound.
    assert result.proposed_expected == 6


def test_recalibration_zero_target_returns_none():
    """Connector with declared 0 expected (e.g. KE bigquery_trends) → no proposal."""
    result = evolve._propose_recalibration(
        "bigquery_trends_ke",
        current_expected=0,
        observed_median=10,
        per_cycle_bound_pct=20.0,
        threshold_pct=20.0,
    )
    assert result is None


def test_recalibration_zero_observed_returns_none():
    """Observed median of zero → no proposal (likely a dead connector)."""
    result = evolve._propose_recalibration(
        "spotify",
        current_expected=300,
        observed_median=0,
        per_cycle_bound_pct=20.0,
        threshold_pct=20.0,
    )
    assert result is None


def test_run_capacity_loop_skips_within_tolerance():
    """End-to-end loop drops connectors within tolerance."""
    capacity = {
        "connectors": {
            "ensembledata": {"expected_rows_per_day": 2600},
            "brand24": {"expected_rows_per_day": 275},
        }
    }
    medians = {"ensembledata": 2700, "brand24": 280}
    proposals = evolve._run_capacity_loop(
        capacity, medians, per_cycle_bound_pct=20.0, threshold_pct=20.0
    )
    assert proposals == []


def test_run_capacity_loop_proposes_both_directions():
    """Two connectors: one needs uptick, one needs downtick."""
    capacity = {
        "connectors": {
            "ensembledata": {"expected_rows_per_day": 2600},
            "brand24": {"expected_rows_per_day": 275},
        }
    }
    medians = {"ensembledata": 3500, "brand24": 100}  # +35% and -64%
    proposals = evolve._run_capacity_loop(
        capacity, medians, per_cycle_bound_pct=20.0, threshold_pct=20.0
    )
    assert len(proposals) == 2
    by_name = {p.connector: p for p in proposals}
    assert by_name["ensembledata"].change_pct > 0
    assert by_name["brand24"].change_pct < 0


# ----------------------------------------------------------------------
# Loop 2: endpoint discovery


def test_discovery_loop_returns_all_non_in_use():
    catalog = {
        "ensembledata": {
            "endpoints": [
                {"path": "/tt/keyword/search", "status": "in_use"},
                {"path": "/twitter/keyword/search", "status": "dark_shipped", "ship_wave": 1},
                {"path": "/twitter/list/posts", "status": "not_shipped", "ship_wave": 3},
            ]
        }
    }
    gaps = evolve._run_discovery_loop(catalog)
    paths = {g.path for g in gaps}
    assert "/tt/keyword/search" not in paths
    assert "/twitter/keyword/search" in paths
    assert "/twitter/list/posts" in paths


def test_discovery_loop_sorts_by_status_then_wave():
    """code_landed_flag_off > dark_shipped > not_shipped, ties broken by ship_wave."""
    catalog = {
        "vendor_a": {
            "endpoints": [
                {"path": "/not-shipped-wave3", "status": "not_shipped", "ship_wave": 3},
                {"path": "/dark-shipped-wave1", "status": "dark_shipped", "ship_wave": 1},
                {"path": "/flag-off", "status": "code_landed_flag_off"},
                {"path": "/dark-shipped-wave2", "status": "dark_shipped", "ship_wave": 2},
            ]
        }
    }
    gaps = evolve._run_discovery_loop(catalog)
    paths = [g.path for g in gaps]
    assert paths.index("/flag-off") < paths.index("/dark-shipped-wave1")
    assert paths.index("/dark-shipped-wave1") < paths.index("/dark-shipped-wave2")
    assert paths.index("/dark-shipped-wave2") < paths.index("/not-shipped-wave3")


def test_discovery_loop_empty_catalog_returns_empty():
    assert evolve._run_discovery_loop({}) == []


def test_discovery_loop_excludes_never_shippable_statuses():
    """Statuses that can never ship must not surface as opportunities."""
    catalog = {
        "v": {
            "endpoints": [
                {"path": "/in-use", "status": "in_use"},
                {"path": "/vendor-not-published", "status": "vendor_not_published"},
                {"path": "/permanently-disabled", "status": "permanently_disabled"},
                {"path": "/removed-dead", "status": "removed_dead"},
                {"path": "/removed-from-code", "status": "removed_from_code"},
                {"path": "/dark", "status": "dark_shipped", "ship_wave": 1},
                {"path": "/not-shipped", "status": "not_shipped", "ship_wave": 2},
            ]
        }
    }
    gaps = evolve._run_discovery_loop(catalog)
    paths = {g.path for g in gaps}
    assert paths == {"/dark", "/not-shipped"}


def test_discovery_loop_preserves_notes():
    catalog = {
        "v": {
            "endpoints": [
                {
                    "path": "/foo",
                    "status": "not_shipped",
                    "ship_wave": 2,
                    "notes": "Adds X signal",
                }
            ]
        }
    }
    gaps = evolve._run_discovery_loop(catalog)
    assert gaps[0].notes == "Adds X signal"


# ----------------------------------------------------------------------
# Loop 3: coherence


def test_detect_source_dominance_flags_above_threshold():
    distribution = [
        {"market": "za", "topic_group": "music_amapiano", "source": "ensembledata", "rows": 900},
        {"market": "za", "topic_group": "music_amapiano", "source": "brand24", "rows": 50},
        {"market": "za", "topic_group": "music_amapiano", "source": "rss", "rows": 50},
    ]
    flags = evolve._detect_source_dominance(distribution, dominance_threshold_pct=80.0)
    assert len(flags) == 1
    assert flags[0].topic_group == "music_amapiano"
    assert flags[0].dominant_source == "ensembledata"
    assert flags[0].dominant_share_pct == 90.0


def test_detect_source_dominance_no_flag_when_balanced():
    distribution = [
        {"market": "za", "topic_group": "music_amapiano", "source": "ensembledata", "rows": 400},
        {"market": "za", "topic_group": "music_amapiano", "source": "brand24", "rows": 300},
        {"market": "za", "topic_group": "music_amapiano", "source": "rss", "rows": 300},
    ]
    flags = evolve._detect_source_dominance(distribution, dominance_threshold_pct=80.0)
    assert flags == []


def test_detect_source_dominance_skips_thin_topics():
    """Below 30 rows total → too thin to interpret, no flag."""
    distribution = [
        {"market": "za", "topic_group": "rare", "source": "ensembledata", "rows": 20},
        {"market": "za", "topic_group": "rare", "source": "rss", "rows": 1},
    ]
    flags = evolve._detect_source_dominance(distribution, dominance_threshold_pct=80.0)
    assert flags == []


# ----------------------------------------------------------------------
# YAML mutation


def test_apply_recalibration_writes_proposed_values_within_bound():
    capacity = {
        "connectors": {
            "ensembledata": {"expected_rows_per_day": 2600, "utilisation_warn_pct": 60},
        }
    }
    proposals = [
        evolve.CapacityProposal(
            connector="ensembledata",
            current_expected=2600,
            observed_median=3000,
            proposed_expected=3000,
            change_pct=15.4,
            bounded=False,
            within_bound=True,
            rationale="test",
        )
    ]
    out = evolve._apply_recalibration(capacity, proposals)
    assert out["connectors"]["ensembledata"]["expected_rows_per_day"] == 3000
    assert "_last_recalibrated" in out["connectors"]["ensembledata"]
    assert out["connectors"]["ensembledata"]["_last_recalibration_observed_median"] == 3000


def test_apply_recalibration_skips_out_of_bound_proposals():
    capacity = {"connectors": {"ensembledata": {"expected_rows_per_day": 2600}}}
    proposals = [
        evolve.CapacityProposal(
            connector="ensembledata",
            current_expected=2600,
            observed_median=10000,
            proposed_expected=3120,
            change_pct=100.0,
            bounded=True,
            within_bound=False,  # NOT applied
            rationale="test",
        )
    ]
    out = evolve._apply_recalibration(capacity, proposals)
    assert out["connectors"]["ensembledata"]["expected_rows_per_day"] == 2600  # unchanged
    assert "_last_recalibrated" not in out["connectors"]["ensembledata"]


# ----------------------------------------------------------------------
# History audit log


def test_append_history_creates_file_if_missing(tmp_path, monkeypatch):
    history_path = tmp_path / "engine_evolution_history.json"
    monkeypatch.setattr(evolve, "HISTORY_FILE", history_path)
    monkeypatch.setattr(evolve, "HISTORY_DIR", tmp_path)
    evolve._append_history({"event": "test", "value": 1})
    data = json.loads(history_path.read_text())
    assert len(data) == 1
    assert data[0]["event"] == "test"


def test_append_history_appends_to_existing(tmp_path, monkeypatch):
    history_path = tmp_path / "engine_evolution_history.json"
    history_path.write_text('[{"event": "first"}]')
    monkeypatch.setattr(evolve, "HISTORY_FILE", history_path)
    monkeypatch.setattr(evolve, "HISTORY_DIR", tmp_path)
    evolve._append_history({"event": "second"})
    data = json.loads(history_path.read_text())
    assert len(data) == 2
    assert data[0]["event"] == "first"
    assert data[1]["event"] == "second"


# ----------------------------------------------------------------------
# Smoke: build_report with mocked BQ


def test_build_report_smoke_with_mocked_bq():
    """build_report wires capacity + catalog + BQ into a sensible report."""
    fake_series = {
        "rss": [310] * 14,
        # socialcrawl replaced ensembledata as the primary social vendor on
        # 23 Jul 2026; ensembledata's declared capacity is now zero, so the
        # recalibration case rides on socialcrawl instead.
        "socialcrawl": [4050] * 14,  # ~35% above declared 3000
        "brand24": [275] * 14,
        "youtube": [420] * 14,
        "gdelt": [550] * 14,
        "bigquery_trends": [200] * 14,
    }
    with (
        patch.object(evolve, "_bq_rolling_medians", return_value=fake_series),
        patch.object(evolve, "_bq_topic_source_distribution", return_value=[]),
    ):
        report = evolve.build_report("2026-05-28", window_days=14)

    assert isinstance(report, evolve.EvolutionReport)
    assert report.trend_date == "2026-05-28"
    # socialcrawl should propose an uptick
    socialcrawl_proposals = [p for p in report.proposals if p.connector == "socialcrawl"]
    assert len(socialcrawl_proposals) == 1
    assert socialcrawl_proposals[0].observed_median == 4050
    # Endpoint gaps include the dark-shipped + not-shipped entries from
    # the real catalog file.
    assert len(report.endpoint_gaps) > 0
    # Coherence empty (we passed empty distribution).
    assert report.coherence_flags == []


def test_render_text_contains_all_blocks():
    report = evolve.EvolutionReport(trend_date="2026-05-28", rolling_window_days=14)
    report.proposals = [
        evolve.CapacityProposal(
            connector="ensembledata",
            current_expected=2600,
            observed_median=3000,
            proposed_expected=3000,
            change_pct=15.4,
            bounded=False,
            within_bound=True,
            rationale="test rationale",
        )
    ]
    report.endpoint_gaps = [
        evolve.EndpointGap(
            vendor="ensembledata",
            path="/twitter/keyword/search",
            ship_wave=1,
            status="dark_shipped",
            notes="ready to flip",
        )
    ]
    report.coherence_flags = [
        evolve.CoherenceFlag(
            topic_group="music_amapiano",
            market="za",
            flag_type="source_dominance",
            dominant_source="ensembledata",
            dominant_share_pct=90.0,
            sources_contributing={"ensembledata": 900, "brand24": 100},
            notes="ensembledata dominates",
        )
    ]
    out = evolve.render_text(report)
    assert "WEEKLY ENGINE REVIEW 2026-05-28" in out
    assert "PROPOSED CAPACITY RECALIBRATIONS" in out
    assert "ensembledata" in out
    assert "DISCOVERED OPPORTUNITIES" in out
    assert "/twitter/keyword/search" in out
    assert "COHERENCE FLAGS" in out
    assert "music_amapiano" in out
    assert "Dry-run" in out


def test_render_text_states_applied_when_report_applied():
    report = evolve.EvolutionReport(trend_date="2026-05-28", rolling_window_days=14)
    report.applied = True
    out = evolve.render_text(report)
    assert "Capacity YAML mutated" in out


def test_render_text_surfaces_notes_when_coherence_loop_skipped():
    """A swallowed coherence failure recorded in notes must be visible."""
    report = evolve.EvolutionReport(trend_date="2026-05-28", rolling_window_days=14)
    report.notes = "coherence loop skipped: BQ timeout"
    out = evolve.render_text(report)
    assert "coherence loop skipped: BQ timeout" in out
    # The clean-coherence line must NOT claim a clean check when the loop was skipped.
    assert "no topics show single-source dominance" not in out


def test_render_text_clean_coherence_when_no_notes_and_no_flags():
    """No notes and no flags is a genuine clean coherence check."""
    report = evolve.EvolutionReport(trend_date="2026-05-28", rolling_window_days=14)
    out = evolve.render_text(report)
    assert "no topics show single-source dominance" in out


# ----------------------------------------------------------------------
# YAML round-trip


def test_capacity_yaml_loads():
    capacity = evolve._load_capacity()
    assert "connectors" in capacity


def test_catalog_yaml_loads():
    catalog = evolve._load_catalog()
    # At least the 6 known vendor blocks should be present.
    for vendor in ("ensembledata", "brand24", "gdelt", "youtube", "spotify", "bigquery_trends"):
        assert vendor in catalog, f"missing {vendor} in vendor_endpoint_catalog.yaml"


def test_bq_rolling_medians_query_includes_apple_music_column():
    """Day-1 guard: rolling-medians SQL must SELECT apple_music_rows.

    Without it, the weekly evolve loop never sees Apple Music traffic and
    capacity stays uncalibrated even after the connector lands real volume.
    """
    import inspect

    src = inspect.getsource(evolve._bq_rolling_medians)
    assert "apple_music_rows" in src, "missing apple_music_rows in rolling-medians SELECT"
    assert '"apple_music"' in src, "apple_music key missing from series dict"


def test_topic_source_distribution_sql_avoids_reserved_rows_alias():
    """COUNT(*) AS rows uses the BigQuery reserved keyword ROWS, which 400s the
    query so the coherence loop (loop 3) never produced a flag in production.
    The alias must be a non-reserved name."""
    import inspect

    src = inspect.getsource(evolve._bq_topic_source_distribution)
    assert "AS rows" not in src
    assert "AS row_count" in src


def test_write_capacity_preserves_comments(tmp_path, monkeypatch):
    """_write_capacity must keep YAML comments; safe_dump stripped them, so a
    --apply run used to destroy the ~60 comment lines in engine_capacity.yaml."""
    src = (
        "# Engine Capacity Matrix header comment\n"
        "connectors:\n"
        "  ensembledata:\n"
        "    expected_rows_per_day: 2600  # inline note\n"
        "  spotify:\n"
        "    expected_rows_per_day: 0  # PERMANENTLY DISABLED\n"
    )
    f = tmp_path / "engine_capacity.yaml"
    f.write_text(src, encoding="utf-8")
    monkeypatch.setattr(evolve, "CAPACITY_FILE", f)

    cap = evolve._load_capacity()
    cap["connectors"]["ensembledata"]["expected_rows_per_day"] = 3000
    evolve._write_capacity(cap)

    out = f.read_text(encoding="utf-8")
    assert "expected_rows_per_day: 3000" in out  # value applied
    assert "# Engine Capacity Matrix header comment" in out  # header survived
    assert "# PERMANENTLY DISABLED" in out  # inline comment survived
