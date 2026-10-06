"""Tests for the product family coverage matrix evaluated over a window readback."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from src.analysis.open_intelligence.coverage_matrix import (
    FAMILIES,
    FAMILY_STATES,
    PRODUCT_FAMILIES_PATH,
    READBACK_ROW_COLUMNS,
    evaluate_coverage_matrix,
    load_product_families,
    validate_product_families,
    validate_readback,
)

ENGINE = Path(__file__).resolve().parents[2]
WINDOW_END = datetime(2026, 9, 24, 0, tzinfo=UTC)
FRESH = "2026-09-23T12:00:00+00:00"
OLD = "2026-09-20T00:00:00+00:00"
DIGEST = "a" * 64

FAMILIES_UNDER_TEST = {
    "news": {"routes": ["rss", "gdelt"]},
    "search": {"routes": ["google_trends_rss"]},
    "social_conversation": {"routes": ["socialcrawl", "reddit"]},
    "comments": {"measured_by": "not_in_collection_window", "reason": "shares sources"},
    "music_sounds": {"routes": ["apple_music"]},
    "apps": {"routes": ["app_charts"]},
    "creators": {"measured_by": "not_in_collection_window", "reason": "shares sources"},
    "events": {"measured_by": "not_in_collection_window", "reason": "no source"},
    "phrases": {"measured_by": "not_in_collection_window", "reason": "no source"},
    "unseeded_discovery": {
        "measured_by": "not_in_collection_window",
        "reason": "lives in signal candidates",
    },
}
UNASSIGNED_UNDER_TEST = {"wikipedia": "own channel family"}
CONFIG_UNDER_TEST = {"families": FAMILIES_UNDER_TEST, "unassigned_routes": UNASSIGNED_UNDER_TEST}


def row(route, market="ng", **overrides):
    fields = {
        "market": market,
        "route": route,
        "expected": True,
        "physical_rows": 20,
        "distinct_native_observations": 0,
        "inferred_identity_observations": 20,
        "rows_without_identity": 0,
        "qualified_observations": 10,
        "classified_observations": 8,
        "unique_qualified_observations": 4,
        "reassigned_market_rows": 0,
        "rows_with_geo_method": 20,
        "newest_published_at": FRESH,
        "newest_collected_at": FRESH,
        "rows_without_published_at": 0,
        "recorded_failures": 0,
        "pipeline_reported_rows": 200,
        "pipeline_route_rows": None,
        "control_state": "emitting",
        "control_reason": None,
        "unresolved_source_values": [],
    }
    fields.update(overrides)
    return fields


def healthy_rows(market="ng"):
    return [
        row(source, market)
        for source in (
            "rss",
            "gdelt",
            "google_trends_rss",
            "socialcrawl",
            "reddit",
            "apple_music",
            "app_charts",
        )
    ]


def summary_for(rows):
    out = {}
    for item in rows:
        counts = out.setdefault(
            item["market"],
            dict.fromkeys(
                (
                    "emitting",
                    "failed",
                    "known_quiet",
                    "unexplained_zero",
                    "reader_mismatch",
                    "unresolved_route",
                ),
                0,
            ),
        )
        counts[item["control_state"]] += 1
    return out


def readback(rows=None, *, kind="native_bigquery", strata=("ng",), limit=100):
    rows = healthy_rows() if rows is None else rows
    source = (
        {"kind": "native_bigquery", "job_id": "job-1", "output_sha256": DIGEST}
        if kind == "native_bigquery"
        else {"kind": kind}
    )
    rb = {
        "window": {"start": "2026-09-23T00:00:00+00:00", "end": "2026-09-24T00:00:00+00:00"},
        "market_strata": list(strata),
        "query_limit": limit,
        "rows": rows,
        "source": source,
        "controls": summary_for(rows),
        "zero_hides_nothing": not any(
            r["control_state"] in {"unexplained_zero", "reader_mismatch"} for r in rows
        ),
        "expected_sources": [[r["market"], r["route"]] for r in rows if r["expected"]],
        "known_quiet": [
            [r["market"], r["route"]] for r in rows if r["control_state"] == "known_quiet"
        ],
    }
    raw_rows(rb)
    return rb


def raw_rows(rb):
    """Return the raw rows file a native read saves and bind its digest into ``rb``."""
    content = (json.dumps(rb["rows"], indent=2) + "\n").encode("utf-8")
    if rb["source"].get("kind") == "native_bigquery":
        rb["source"]["output_sha256"] = hashlib.sha256(content).hexdigest()
    return content


def evaluate(rb, **kwargs):
    if "rows_bytes" not in kwargs:
        kwargs["rows_bytes"] = raw_rows(rb)
    return evaluate_coverage_matrix(rb, CONFIG_UNDER_TEST, window_end=WINDOW_END, **kwargs)


def cell(result, family, market="ng"):
    return result["markets"][market]["families"][family]


def with_row(route, **overrides):
    rows = [r for r in healthy_rows() if r["route"] != route]
    rows.append(row(route, **overrides))
    return rows


# Family states, each with a counterexample.


def test_all_family_states_are_the_fixed_set():
    assert FAMILY_STATES == (
        "covered",
        "thin",
        "stale",
        "leaking",
        "missing",
        "unmeasured",
    )
    assert FAMILIES == (
        "news",
        "search",
        "social_conversation",
        "comments",
        "music_sounds",
        "apps",
        "creators",
        "events",
        "phrases",
        "unseeded_discovery",
    )


def test_healthy_family_is_covered_and_a_thin_one_is_not():
    result = evaluate(readback())
    assert cell(result, "apps")["state"] == "covered"
    thin = evaluate(readback(with_row("app_charts", unique_qualified_observations=0)))
    assert cell(thin, "apps")["state"] == "thin"
    assert cell(thin, "apps")["qualified_observations"] == 10
    assert cell(thin, "apps")["unique_qualified_contribution"] == 0


def test_stale_family_is_stale_and_a_fresh_one_is_not():
    stale = evaluate(readback(with_row("apple_music", newest_published_at=OLD)))
    assert cell(stale, "music_sounds")["state"] == "stale"
    assert cell(stale, "music_sounds")["freshness_hours"] == 96.0
    wide = evaluate(readback(with_row("apple_music", newest_published_at=OLD)), freshness_hours=100)
    assert cell(wide, "music_sounds")["state"] == "covered"


def test_unknown_freshness_is_never_covered():
    result = evaluate(readback(with_row("apple_music", newest_published_at=None)))
    assert cell(result, "music_sounds")["freshness_hours"] is None
    assert cell(result, "music_sounds")["state"] == "stale"


def test_leaking_family_is_leaking_and_a_tolerated_leak_is_not():
    rows = [*with_row("app_charts", reassigned_market_rows=5), row("app_charts", "ke")]
    result = evaluate(readback(rows, strata=("ng", "ke")))
    assert cell(result, "apps")["foreign_market_leakage"] == 0.25
    assert cell(result, "apps")["state"] == "leaking"
    tolerated = evaluate(readback(rows, strata=("ng", "ke")), leakage_limit=0.3)
    assert cell(tolerated, "apps")["state"] == "covered"


def test_missing_family_has_no_qualified_observations():
    rows = with_row(
        "app_charts",
        qualified_observations=0,
        classified_observations=0,
        unique_qualified_observations=0,
    )
    result = evaluate(readback(rows))
    assert cell(result, "apps")["state"] == "missing"
    absent = evaluate(readback([r for r in healthy_rows() if r["route"] != "app_charts"]))
    assert cell(absent, "apps")["state"] == "missing"
    assert cell(absent, "apps")["foreign_market_leakage"] is None
    assert cell(absent, "apps")["absent_routes"] == ["app_charts"]


def test_unmeasurable_family_and_unreadable_routes_are_unmeasured():
    result = evaluate(readback())
    unseeded = cell(result, "unseeded_discovery")
    assert unseeded["state"] == "unmeasured"
    assert unseeded["reason"] == "lives in signal candidates"
    assert unseeded["unique_qualified_contribution"] is None
    unexplained = with_row(
        "reddit",
        physical_rows=0,
        inferred_identity_observations=0,
        qualified_observations=0,
        classified_observations=0,
        unique_qualified_observations=0,
        rows_with_geo_method=0,
        newest_published_at=None,
        newest_collected_at=None,
        control_state="unexplained_zero",
        control_reason="zero_without_decision_record",
    )
    social = cell(evaluate(readback(unexplained)), "social_conversation")
    assert social["state"] == "unmeasured"
    assert social["unmeasured_routes"] == [{"route": "reddit", "control_state": "unexplained_zero"}]


def test_reader_mismatch_makes_every_family_in_the_market_unmeasured():
    rows = [
        row(
            source,
            physical_rows=0,
            inferred_identity_observations=0,
            qualified_observations=0,
            classified_observations=0,
            unique_qualified_observations=0,
            rows_with_geo_method=0,
            newest_published_at=None,
            newest_collected_at=None,
            control_state="reader_mismatch",
            control_reason="pipeline_rows_not_readable",
        )
        for source in ("rss", "app_charts")
    ]
    result = evaluate(readback(rows))
    assert cell(result, "apps")["state"] == "unmeasured"
    assert cell(result, "news")["state"] == "unmeasured"
    assert cell(result, "search")["state"] == "unmeasured"
    assert cell(result, "search")["reason"] == "market_reader_mismatch"


# Acceptance.


def test_fixture_readback_never_yields_met():
    fixture = evaluate(readback(kind="fixture"))
    assert fixture["family_live_acceptance"] == "open"
    assert "readback_not_native" in fixture["acceptance_open_reasons"]


def test_native_readback_with_any_uncovered_family_is_open():
    result = evaluate(readback())
    assert result["family_live_acceptance"] == "open"
    assert "family_not_covered" in result["acceptance_open_reasons"]


def test_native_readback_with_every_family_covered_is_met():
    measured_only = {
        name: entry for name, entry in FAMILIES_UNDER_TEST.items() if "routes" in entry
    }
    families = {
        name: FAMILIES_UNDER_TEST[name] if name in measured_only else {"routes": [name]}
        for name in FAMILIES
    }
    rows = [
        {**r, "reassigned_market_rows": 1}
        for market in ("ng", "ke")
        for r in healthy_rows(market)
        + [row(name, market) for name in FAMILIES if name not in measured_only]
    ]
    config = {"families": families, "unassigned_routes": {}}
    native = readback(rows, strata=("ng", "ke"))
    result = evaluate_coverage_matrix(
        native, config, window_end=WINDOW_END, leakage_limit=0.1, rows_bytes=raw_rows(native)
    )
    assert result["family_live_acceptance"] == "met"
    assert result["acceptance_open_reasons"] == []
    fixture = evaluate_coverage_matrix(
        readback(rows, kind="fixture", strata=("ng", "ke")),
        config,
        window_end=WINDOW_END,
        leakage_limit=0.1,
    )
    assert fixture["family_live_acceptance"] == "open"


def test_failed_only_route_is_missing_with_the_failure_listed():
    rows = with_row(
        "app_charts",
        physical_rows=0,
        inferred_identity_observations=0,
        qualified_observations=0,
        classified_observations=0,
        unique_qualified_observations=0,
        rows_with_geo_method=0,
        newest_published_at=None,
        newest_collected_at=None,
        recorded_failures=2,
        control_state="failed",
        control_reason="recorded_failure",
    )
    result = evaluate(readback(rows))
    apps = cell(result, "apps")
    assert apps["state"] == "missing"
    assert apps["missingness"] == [
        {"route": "app_charts", "control_state": "failed", "recorded_failures": 2}
    ]
    failures = result["markets"]["ng"]["failures"]
    assert {
        "family": "apps",
        "state": "missing",
        "missingness": apps["missingness"],
    } in [{key: item[key] for key in ("family", "state", "missingness")} for item in failures]


def test_failures_list_every_uncovered_family_completely():
    rows = with_row("app_charts", unique_qualified_observations=0)
    result = evaluate(readback(rows))
    listed = sorted(item["family"] for item in result["markets"]["ng"]["failures"])
    expected = sorted(
        family
        for family, value in result["markets"]["ng"]["families"].items()
        if value["state"] != "covered"
    )
    assert listed == expected
    assert "apps" in listed
    assert "unseeded_discovery" in listed


def test_unique_contribution_comes_from_the_query_column():
    rows = with_row("reddit", qualified_observations=30, unique_qualified_observations=7)
    social = cell(evaluate(readback(rows)), "social_conversation")
    assert social["unique_qualified_contribution"] == 11
    assert social["qualified_observations"] == 40


def test_topic_group_classification_is_not_breadth():
    rows = with_row(
        "app_charts",
        qualified_observations=50,
        classified_observations=50,
        unique_qualified_observations=0,
    )
    assert cell(evaluate(readback(rows)), "apps")["state"] == "thin"
    counted = readback()
    counted["rows"][0]["topic_group_count"] = 12
    with pytest.raises(ValueError, match="readback_row_columns_mismatch"):
        evaluate(counted)


def test_supported_question_contribution_needs_question_evidence():
    apps = cell(evaluate(readback()), "apps")
    assert apps["supported_question_contribution"] is None
    assert apps["supported_question_reason"] == "needs_question_evidence"


def test_every_stratum_is_evaluated_even_without_rows():
    result = evaluate(readback(strata=("ng", "ke")))
    assert set(result["markets"]) == {"ng", "ke"}
    assert cell(result, "apps", "ke")["state"] == "missing"


# Readback refusals.


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda rb: rb.pop("window"), "readback_field_missing:window"),
        (lambda rb: rb.pop("rows"), "readback_field_missing:rows"),
        (lambda rb: rb.pop("controls"), "readback_field_missing:controls"),
        (lambda rb: rb.update(window={"start": "x"}), "readback_window_invalid"),
        (lambda rb: rb.update(market_strata=[]), "readback_strata_invalid"),
        (lambda rb: rb.update(query_limit=0), "readback_query_limit_invalid"),
        (lambda rb: rb.update(query_limit=True), "readback_query_limit_invalid"),
        (lambda rb: rb.update(query_limit=3), "readback_rows_exceed_limit"),
        (lambda rb: rb.update(source={"kind": "sheet"}), "readback_source_kind_unknown"),
        (
            lambda rb: rb.update(source={"kind": "native_bigquery", "output_sha256": DIGEST}),
            "readback_native_job_invalid",
        ),
        (
            lambda rb: rb.update(
                source={"kind": "native_bigquery", "job_id": "j", "output_sha256": "abc"}
            ),
            "readback_native_job_invalid",
        ),
        (lambda rb: rb["rows"][0].pop("control_state"), "readback_row_columns_mismatch"),
        (lambda rb: rb["rows"][0].update(market="za"), "readback_market_not_in_strata"),
        (lambda rb: rb["rows"][0].update(physical_rows=-1), "readback_count_invalid"),
        (lambda rb: rb["rows"][0].update(physical_rows="20"), "readback_count_invalid"),
        (
            lambda rb: rb["rows"][0].update(unique_qualified_observations=11),
            "readback_identity_arithmetic",
        ),
        (
            lambda rb: rb["rows"][0].update(classified_observations=11),
            "readback_identity_arithmetic",
        ),
        (lambda rb: rb["rows"][0].update(control_state="quiet"), "readback_control_state_unknown"),
        (lambda rb: rb["rows"][0].update(expected="yes"), "readback_expected_invalid"),
        (lambda rb: rb["rows"].append(dict(rb["rows"][0])), "readback_duplicate_route"),
        (
            lambda rb: rb["rows"][0].update(newest_published_at="yesterday"),
            "readback_timestamp_invalid",
        ),
        (
            lambda rb: rb["rows"][0].update(newest_published_at="2026-09-23T12:00:00"),
            "readback_timestamp_invalid",
        ),
        (
            lambda rb: rb["controls"]["ng"].update(emitting=1),
            "readback_controls_mismatch",
        ),
    ],
)
def test_malformed_readback_is_refused(mutate, code):
    rb = readback(limit=100)
    mutate(rb)
    with pytest.raises(ValueError, match=f"^{re.escape(code)}$"):
        validate_readback(rb)


def test_readback_not_a_mapping_is_refused():
    with pytest.raises(ValueError, match=r"^readback_not_mapping$"):
        validate_readback([])


def test_validation_does_not_mutate_the_readback():
    rb = readback()
    before = copy.deepcopy(rb)
    evaluate(rb)
    assert rb == before


def test_window_end_must_be_timezone_aware():
    with pytest.raises(ValueError, match=r"^window_end_invalid$"):
        evaluate_coverage_matrix(readback(), CONFIG_UNDER_TEST, window_end=datetime(2026, 9, 24))


def test_row_columns_are_the_window_contract_columns():
    assert READBACK_ROW_COLUMNS == (
        "market",
        "route",
        "expected",
        "physical_rows",
        "distinct_native_observations",
        "inferred_identity_observations",
        "rows_without_identity",
        "qualified_observations",
        "classified_observations",
        "unique_qualified_observations",
        "reassigned_market_rows",
        "rows_with_geo_method",
        "newest_published_at",
        "newest_collected_at",
        "rows_without_published_at",
        "recorded_failures",
        "pipeline_reported_rows",
        "pipeline_route_rows",
        "control_state",
        "control_reason",
        "unresolved_source_values",
    )


# Family config refusals.


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda f: f.pop("events"), "families_mismatch"),
        (lambda f: f.update(extra={"routes": ["x"]}), "families_mismatch"),
        (lambda f: f.update(apps={"routes": []}), "family_routes_invalid:apps"),
        (lambda f: f.update(apps={"routes": ["a", "a"]}), "family_routes_invalid:apps"),
        (
            lambda f: f.update(apps={"routes": ["app_charts"], "reason": "x"}),
            "family_entry_invalid:apps",
        ),
        (
            lambda f: f.update(events={"measured_by": "guess", "reason": "x"}),
            "family_entry_invalid:events",
        ),
        (
            lambda f: f.update(events={"measured_by": "not_in_collection_window"}),
            "family_entry_invalid:events",
        ),
        (
            lambda f: f.update(apps={"routes": ["gdelt"]}),
            "family_route_shared:gdelt",
        ),
    ],
)
def test_malformed_family_config_is_refused(mutate, code):
    config = copy.deepcopy(CONFIG_UNDER_TEST)
    mutate(config["families"])
    with pytest.raises(ValueError, match=f"^{re.escape(code)}$"):
        validate_product_families(config)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda c: c.pop("unassigned_routes"), "product_families_config_invalid"),
        (lambda c: c.update(extra=1), "product_families_config_invalid"),
        (
            lambda c: c["unassigned_routes"].update(wikipedia=""),
            "unassigned_route_invalid:wikipedia",
        ),
        (
            lambda c: c["unassigned_routes"].update(gdelt="x"),
            "family_route_unassigned_overlap:gdelt",
        ),
        (
            lambda c: c["unassigned_routes"].update(unresolved="x"),
            "route_reserved:unresolved",
        ),
        (
            lambda c: c["families"].update(apps={"routes": ["unresolved"]}),
            "route_reserved:unresolved",
        ),
    ],
)
def test_malformed_unassigned_routes_are_refused(mutate, code):
    config = copy.deepcopy(CONFIG_UNDER_TEST)
    mutate(config)
    with pytest.raises(ValueError, match=f"^{re.escape(code)}$"):
        validate_product_families(config)


# Routes: unmapped, unassigned and unresolved rows.


def unresolved_row(market="ng", physical_rows=7):
    return row(
        "unresolved",
        market,
        expected=False,
        physical_rows=physical_rows,
        inferred_identity_observations=physical_rows,
        qualified_observations=0,
        classified_observations=0,
        unique_qualified_observations=0,
        rows_with_geo_method=physical_rows,
        control_state="unresolved_route",
        control_reason="route_not_resolved",
        unresolved_source_values=["Mystery Feed"],
    )


def test_unmapped_route_is_reported_never_dropped():
    rows = [*healthy_rows(), row("youtube")]
    result = evaluate(readback(rows))
    assert result["markets"]["ng"]["unmapped_routes"] == ["youtube"]
    assert result["family_live_acceptance"] == "open"
    assert "unmapped_routes_present" in result["acceptance_open_reasons"]


def test_unassigned_route_is_listed_apart_from_unmapped():
    rows = [*healthy_rows(), row("wikipedia")]
    result = evaluate(readback(rows))
    assert result["markets"]["ng"]["unmapped_routes"] == []
    assert result["markets"]["ng"]["unassigned_routes"] == [
        {"route": "wikipedia", "reason": "own channel family"}
    ]
    assert "unmapped_routes_present" not in result["acceptance_open_reasons"]


def test_unresolved_rows_are_counted_without_unmeasuring_every_family():
    rows = [*healthy_rows(), unresolved_row()]
    result = evaluate(readback(rows))
    market = result["markets"]["ng"]
    assert market["unresolved_rows_present"] is True
    assert market["unresolved_rows"] == 7
    assert market["unmapped_routes"] == []
    assert cell(result, "apps")["state"] == "covered"
    assert result["family_live_acceptance"] == "open"
    assert "unresolved_rows_present" in result["acceptance_open_reasons"]
    clean = evaluate(readback())["markets"]["ng"]
    assert clean["unresolved_rows_present"] is False
    assert clean["unresolved_rows"] == 0


def test_unreadable_route_unmeasures_only_its_own_family():
    rows = with_row(
        "apple_music",
        physical_rows=0,
        inferred_identity_observations=0,
        qualified_observations=0,
        classified_observations=0,
        unique_qualified_observations=0,
        rows_with_geo_method=0,
        newest_published_at=None,
        newest_collected_at=None,
        control_state="unexplained_zero",
        control_reason="zero_without_decision_record",
    )
    result = evaluate(readback(rows))
    assert cell(result, "music_sounds")["state"] == "unmeasured"
    assert cell(result, "apps")["state"] == "covered"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rb: rb["rows"].append(
            row("unresolved", control_state="emitting", control_reason=None)
        ),
        lambda rb: rb["rows"].append(
            row(
                "gdelt2",
                physical_rows=0,
                inferred_identity_observations=0,
                qualified_observations=0,
                classified_observations=0,
                unique_qualified_observations=0,
                control_state="unresolved_route",
            )
        ),
    ],
)
def test_unresolved_route_and_state_must_travel_together(mutate):
    rb = readback()
    mutate(rb)
    rb["controls"] = summary_for(rb["rows"])
    with pytest.raises(ValueError, match=r"^readback_unresolved_route_invalid$"):
        validate_readback(rb)


# The shipped family config.


def _connector_keys():
    text = (ENGINE / "scripts" / "run_rss_now.py").read_text(encoding="utf-8")
    block = text[text.index("CONNECTORS = [") : text.index("]", text.index("CONNECTORS = ["))]
    return re.findall(r'^\s*\("([a-z_]+)",', block, flags=re.MULTILINE)


def test_shipped_family_config_is_valid_and_lives_with_engine_configs():
    assert PRODUCT_FAMILIES_PATH == ENGINE / "configs" / "coverage_product_families.json"
    config = load_product_families()
    assert tuple(config["families"]) == FAMILIES
    assert json.loads(PRODUCT_FAMILIES_PATH.read_text(encoding="utf-8")) == config


def test_shipped_config_places_every_connector_key_exactly_once():
    keys = _connector_keys()
    assert len(keys) == 18
    config = load_product_families()
    mapped = [r for entry in config["families"].values() for r in entry.get("routes", ())]
    placed = mapped + list(config["unassigned_routes"])
    assert sorted(placed) == sorted(keys)


def test_shipped_route_mapping_is_pinned():
    config = load_product_families()
    routes = {name: entry.get("routes") for name, entry in config["families"].items()}
    assert routes == {
        "news": ["rss", "gdelt"],
        "search": ["bigquery_trends", "semrush", "google_trends_rss"],
        "social_conversation": ["ensemble", "reddit", "socialcrawl", "bluesky", "pulsar"],
        "comments": None,
        "music_sounds": ["apple_music", "spotify", "audiomack"],
        "apps": ["app_charts"],
        "creators": None,
        "events": None,
        "phrases": None,
        "unseeded_discovery": None,
    }
    assert set(config["unassigned_routes"]) == {
        "youtube",
        "youtube_scrape",
        "wikipedia",
        "cloudflare_radar",
    }


def test_shipped_unseeded_discovery_is_not_in_the_collection_window():
    entry = load_product_families()["families"]["unseeded_discovery"]
    assert entry["measured_by"] == "not_in_collection_window"
    assert entry["reason"]


def test_the_matrix_reads_the_window_readback_exactly_as_the_window_module_produces_it():
    from src.analysis.open_intelligence.coverage_window import (
        COLLECTION_WINDOW_COLUMNS,
        window_readback,
    )

    from tests.unit.test_coverage_window import row as window_row

    assert set(READBACK_ROW_COLUMNS) == set(COLLECTION_WINDOW_COLUMNS)
    produced = window_readback(
        [window_row()],
        window=(datetime(2026, 9, 24, tzinfo=UTC), datetime(2026, 9, 25, tzinfo=UTC)),
        market_strata=[window_row()["market"]],
        expected_sources=[(window_row()["market"], window_row()["route"])],
        known_quiet=[],
        query_limit=100,
        job_id=None,
        output_sha256=None,
    )
    result = evaluate_coverage_matrix(
        produced, load_product_families(), window_end=datetime(2026, 9, 25, tzinfo=UTC)
    )
    assert result["family_live_acceptance"] == "open"


# Review fixes: shared row validation, hidden zeros, unmeasured leakage.


def all_covered(extra_rows=(), unassigned=None, reassigned=1):
    measured_only = {
        name: entry for name, entry in FAMILIES_UNDER_TEST.items() if "routes" in entry
    }
    families = {
        name: FAMILIES_UNDER_TEST[name] if name in measured_only else {"routes": [name]}
        for name in FAMILIES
    }
    rows = [
        {**r, "reassigned_market_rows": reassigned}
        for market in ("ng", "ke")
        for r in healthy_rows(market)
        + [row(name, market) for name in FAMILIES if name not in measured_only]
    ]
    rows += list(extra_rows)
    config = {"families": families, "unassigned_routes": unassigned or {}}
    return readback(rows, strata=("ng", "ke")), config


def test_readback_rows_pass_the_window_row_validator():
    rb = readback(with_row("app_charts", physical_rows=0, control_state="emitting"))
    with pytest.raises(ValueError, match=r"^readback_rows_invalid:"):
        validate_readback(rb)
    inconsistent = readback(
        with_row(
            "app_charts",
            physical_rows=0,
            inferred_identity_observations=0,
            qualified_observations=0,
            classified_observations=0,
            unique_qualified_observations=0,
            rows_with_geo_method=0,
            control_state="known_quiet",
            control_reason="declared_quiet",
            recorded_failures=3,
        )
    )
    with pytest.raises(ValueError, match=r"^readback_rows_invalid:control_state_inconsistent$"):
        validate_readback(inconsistent)


def test_a_readback_where_a_zero_hides_something_keeps_acceptance_open():
    hidden = row(
        "wikipedia",
        physical_rows=0,
        inferred_identity_observations=0,
        qualified_observations=0,
        classified_observations=0,
        unique_qualified_observations=0,
        rows_with_geo_method=0,
        newest_published_at=None,
        newest_collected_at=None,
        control_state="unexplained_zero",
        control_reason="zero_without_decision_record",
    )
    rb, config = all_covered([hidden], {"wikipedia": "own channel family"})
    assert rb["zero_hides_nothing"] is False
    result = evaluate_coverage_matrix(
        rb, config, window_end=WINDOW_END, leakage_limit=0.1, rows_bytes=raw_rows(rb)
    )
    assert all(
        value["state"] == "covered" for value in result["markets"]["ng"]["families"].values()
    )
    assert result["family_live_acceptance"] == "open"
    assert result["acceptance_open_reasons"] == ["zero_hides_something"]


def test_a_readback_claiming_nothing_hidden_over_a_hidden_zero_is_still_open():
    hidden = row(
        "wikipedia",
        physical_rows=0,
        inferred_identity_observations=0,
        qualified_observations=0,
        classified_observations=0,
        unique_qualified_observations=0,
        rows_with_geo_method=0,
        newest_published_at=None,
        newest_collected_at=None,
        control_state="unexplained_zero",
        control_reason="zero_without_decision_record",
    )
    rb, config = all_covered([hidden], {"wikipedia": "own channel family"})
    rb["zero_hides_nothing"] = True
    result = evaluate_coverage_matrix(
        rb, config, window_end=WINDOW_END, leakage_limit=0.1, rows_bytes=raw_rows(rb)
    )
    assert "zero_hides_something" in result["acceptance_open_reasons"]
    rb["zero_hides_nothing"] = "yes"
    with pytest.raises(ValueError, match=r"^readback_zero_hides_nothing_invalid$"):
        validate_readback(rb)
    rb.pop("zero_hides_nothing")
    with pytest.raises(ValueError, match=r"^readback_field_missing:zero_hides_nothing$"):
        validate_readback(rb)


def test_leakage_the_writer_cannot_show_is_unmeasured_and_keeps_acceptance_open():
    rb, config = all_covered(reassigned=0)
    result = evaluate_coverage_matrix(rb, config, window_end=WINDOW_END, rows_bytes=raw_rows(rb))
    apps = cell(result, "apps")
    assert apps["foreign_market_leakage"] is None
    assert apps["foreign_market_leakage_reason"] == "market_never_reassigned_by_writer"
    assert apps["state"] == "covered"
    assert result["family_live_acceptance"] == "open"
    assert result["acceptance_open_reasons"] == ["foreign_market_leakage_unmeasured"]
    moved = [*with_row("app_charts", reassigned_market_rows=5), row("app_charts", "ke")]
    measured = cell(evaluate(readback(moved, strata=("ng", "ke"))), "apps")
    assert measured["foreign_market_leakage"] == 0.25
    assert measured["foreign_market_leakage_reason"] is None


def test_market_level_pipeline_route_is_neither_unmapped_nor_a_family_route():
    failure = row(
        "pipeline",
        expected=False,
        physical_rows=0,
        inferred_identity_observations=0,
        qualified_observations=0,
        classified_observations=0,
        unique_qualified_observations=0,
        rows_with_geo_method=0,
        newest_published_at=None,
        newest_collected_at=None,
        recorded_failures=1,
        control_state="failed",
        control_reason="recorded_failure",
    )
    result = evaluate(readback([*healthy_rows(), failure]))
    market = result["markets"]["ng"]
    assert market["unmapped_routes"] == []
    assert market["pipeline_failures"] == 1
    config = copy.deepcopy(CONFIG_UNDER_TEST)
    config["families"]["apps"] = {"routes": ["pipeline"]}
    with pytest.raises(ValueError, match=r"^route_reserved:pipeline$"):
        validate_product_families(config)


# Second review: per route reader checks, declared routes, bounded leakage.


def test_a_route_reader_mismatch_unmeasures_only_its_own_family():
    rows = with_row(
        "app_charts",
        physical_rows=0,
        inferred_identity_observations=0,
        qualified_observations=0,
        classified_observations=0,
        unique_qualified_observations=0,
        rows_with_geo_method=0,
        newest_published_at=None,
        newest_collected_at=None,
        pipeline_route_rows=12,
        control_state="reader_mismatch",
        control_reason="pipeline_route_rows_not_readable",
    )
    result = evaluate(readback(rows))
    assert cell(result, "apps")["state"] == "unmeasured"
    assert cell(result, "apps")["reason"] == "route_not_readable"
    assert cell(result, "news")["state"] != "unmeasured"
    assert "zero_hides_something" in result["acceptance_open_reasons"]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda rb: rb.pop("expected_sources"), "readback_field_missing:expected_sources"),
        (lambda rb: rb.pop("known_quiet"), "readback_field_missing:known_quiet"),
        (
            lambda rb: rb["expected_sources"].pop(),
            "readback_declared_routes_invalid:expected_routes_mismatch",
        ),
        (
            lambda rb: rb["expected_sources"].append(["ng", "wikipedia"]),
            "readback_declared_routes_invalid:expected_routes_mismatch",
        ),
        (
            lambda rb: rb["known_quiet"].append(["ng", "zz"]),
            "readback_declared_routes_invalid:known_quiet_not_expected",
        ),
        (
            lambda rb: rb.update(expected_sources="ng:rss"),
            "readback_declared_routes_invalid:expected_sources_invalid",
        ),
    ],
)
def test_declared_routes_are_cross_checked_against_the_rows(mutate, code):
    rb = readback()
    mutate(rb)
    with pytest.raises(ValueError, match=f"^{re.escape(code)}$"):
        validate_readback(rb)


def test_a_known_quiet_row_not_declared_quiet_is_refused():
    rows = with_row(
        "app_charts",
        physical_rows=0,
        inferred_identity_observations=0,
        qualified_observations=0,
        classified_observations=0,
        unique_qualified_observations=0,
        rows_with_geo_method=0,
        newest_published_at=None,
        newest_collected_at=None,
        control_state="known_quiet",
        control_reason="declared_quiet",
    )
    rb = readback(rows)
    rb["known_quiet"] = []
    with pytest.raises(
        ValueError, match=r"^readback_declared_routes_invalid:known_quiet_mismatch$"
    ):
        validate_readback(rb)


def test_leakage_stays_within_zero_and_one():
    ng = with_row("app_charts", reassigned_market_rows=25)
    ke = [
        row(
            "app_charts",
            "ke",
            physical_rows=30,
            inferred_identity_observations=30,
            rows_with_geo_method=30,
        )
    ]
    result = evaluate(readback(ng + ke, strata=("ng", "ke")))
    apps = cell(result, "apps")
    assert apps["foreign_market_leakage"] is None
    assert apps["foreign_market_leakage_reason"] == "reassigned_rows_exceed_market_rows"
    assert apps["state"] == "leaking"
    for market in result["markets"].values():
        for value in market["families"].values():
            leakage = value["foreign_market_leakage"]
            assert leakage is None or 0 <= leakage <= 1


# A native read is proved by the raw rows file it saved, not by a job id and a
# digest shaped string.


def test_native_readback_with_an_arbitrary_digest_is_refused():
    rb = readback()
    content = raw_rows(rb)
    rb["source"] = {"kind": "native_bigquery", "job_id": "any-job", "output_sha256": "b" * 64}
    with pytest.raises(ValueError, match=r"^readback_native_digest_mismatch$"):
        validate_readback(rb, rows_bytes=content)
    with pytest.raises(ValueError, match=r"^readback_native_digest_mismatch$"):
        evaluate(rb, rows_bytes=content)


def test_native_readback_without_its_saved_rows_is_refused():
    rb = readback()
    with pytest.raises(ValueError, match=r"^readback_native_rows_missing$"):
        validate_readback(rb)
    with pytest.raises(ValueError, match=r"^readback_native_rows_missing$"):
        evaluate_coverage_matrix(rb, CONFIG_UNDER_TEST, window_end=WINDOW_END)


def test_saved_rows_that_differ_from_the_readback_rows_are_refused():
    rb = readback()
    other = copy.deepcopy(rb)
    other["rows"] = other["rows"][:-1]
    content = raw_rows(other)
    rb["source"]["output_sha256"] = other["source"]["output_sha256"]
    with pytest.raises(ValueError, match=r"^readback_native_rows_mismatch$"):
        validate_readback(rb, rows_bytes=content)


def test_saved_rows_that_are_not_window_rows_are_refused():
    rb = readback()
    for content in (b"not json", b"{}", b'[{"route": "rss"}]'):
        rb["source"]["output_sha256"] = hashlib.sha256(content).hexdigest()
        with pytest.raises(ValueError, match=r"^readback_native_rows_mismatch$"):
            validate_readback(rb, rows_bytes=content)


def test_a_native_read_as_the_reader_saves_it_is_accepted():
    from scripts.staging.read_coverage_window import _encode
    from src.analysis.open_intelligence.coverage_window import window_readback

    from tests.unit.test_coverage_window import row as window_row

    rows = [window_row()]
    content = _encode(rows)
    produced = window_readback(
        rows,
        window=(datetime(2026, 9, 24, tzinfo=UTC), datetime(2026, 9, 25, tzinfo=UTC)),
        market_strata=[window_row()["market"]],
        expected_sources=[(window_row()["market"], window_row()["route"])],
        known_quiet=[],
        query_limit=100,
        job_id="bq-job-1",
        output_sha256=hashlib.sha256(content).hexdigest(),
    )
    saved = json.loads(_encode(produced))
    assert validate_readback(saved, rows_bytes=content)["kind"] == "native_bigquery"
    result = evaluate_coverage_matrix(
        saved,
        load_product_families(),
        window_end=datetime(2026, 9, 25, tzinfo=UTC),
        rows_bytes=content,
    )
    assert result["source"]["job_id"] == "bq-job-1"
    tampered = content.replace(b'"physical_rows": ', b'"physical_rows": 1', 1)
    with pytest.raises(ValueError, match=r"^readback_native_digest_mismatch$"):
        validate_readback(saved, rows_bytes=tampered)
