"""Collection window rows: query text, row validation, control states and the read only runner."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from src.analysis.open_intelligence.coverage_window import (
    COLLECTION_WINDOW_ASSERTS,
    COLLECTION_WINDOW_COLUMNS,
    COLLECTION_WINDOW_PARAMETERS,
    COLLECTION_WINDOW_QUERY_PATH,
    CONTROL_STATES,
    PIPELINE_ROUTE,
    UNRESOLVED_ROUTE,
    control_state_for,
    validate_window_rows,
    window_readback,
)

START = datetime(2026, 9, 20, tzinfo=UTC)
END = datetime(2026, 9, 21, tzinfo=UTC)
WINDOW = (START, END)
STRATA = ("za", "ng", "ke")
DIGEST = "b" * 64
COUNT_COLUMNS = (
    "physical_rows",
    "distinct_native_observations",
    "inferred_identity_observations",
    "rows_without_identity",
    "qualified_observations",
    "classified_observations",
    "unique_qualified_observations",
    "reassigned_market_rows",
    "rows_with_geo_method",
    "rows_without_published_at",
    "recorded_failures",
    "pipeline_reported_rows",
)


def row(**overrides):
    fields = {
        "market": "za",
        "route": "rss",
        "expected": True,
        "physical_rows": 5,
        "distinct_native_observations": 2,
        "inferred_identity_observations": 2,
        "rows_without_identity": 1,
        "qualified_observations": 3,
        "classified_observations": 2,
        "unique_qualified_observations": 1,
        "reassigned_market_rows": 0,
        "rows_with_geo_method": 5,
        "newest_published_at": datetime(2026, 9, 20, 12, tzinfo=UTC),
        "newest_collected_at": datetime(2026, 9, 20, 13, tzinfo=UTC),
        "rows_without_published_at": 0,
        "recorded_failures": 0,
        "pipeline_reported_rows": 5,
        "pipeline_route_rows": None,
        "control_state": "emitting",
        "control_reason": None,
        "unresolved_source_values": [],
    }
    fields.update(overrides)
    return fields


def zero_row(**overrides):
    fields = {
        "route": "youtube",
        "physical_rows": 0,
        "distinct_native_observations": 0,
        "inferred_identity_observations": 0,
        "rows_without_identity": 0,
        "qualified_observations": 0,
        "classified_observations": 0,
        "unique_qualified_observations": 0,
        "rows_with_geo_method": 0,
        "newest_published_at": None,
        "newest_collected_at": None,
        "control_state": "unexplained_zero",
        "control_reason": "zero_without_decision_record",
    }
    fields.update(overrides)
    return row(**fields)


def unresolved_row(**overrides):
    fields = {
        "route": UNRESOLVED_ROUTE,
        "expected": False,
        "control_state": "unresolved_route",
        "control_reason": "route_not_resolved",
        "unresolved_source_values": ["Mystery Feed"],
    }
    fields.update(overrides)
    return row(**fields)


def declared(rows, quiet=None):
    """The expected and known quiet routes a set of rows was read under."""
    expected = [[r["market"], r["route"]] for r in rows if r["expected"]]
    if quiet is None:
        quiet = [[r["market"], r["route"]] for r in rows if r["control_state"] == "known_quiet"]
    return {"expected_sources": expected, "known_quiet": quiet}


def pipeline_row(**overrides):
    fields = {
        "route": PIPELINE_ROUTE,
        "expected": False,
        "recorded_failures": 2,
        "control_state": "failed",
        "control_reason": "recorded_failure",
    }
    fields.update(overrides)
    return zero_row(**fields)


# Query text, reviewed without running


def _query_text() -> str:
    return Path(COLLECTION_WINDOW_QUERY_PATH).read_text(encoding="utf-8")


def _final_select_columns(text: str) -> list[str]:
    final = text[text.rindex("\nSELECT\n") + len("\nSELECT\n") :]
    select_list = final[: final.index("\nFROM ")]
    items = [item.strip() for item in select_list.split(",\n") if item.strip()]
    return [re.findall(r"(\w+)\s*$", item)[0] for item in items]


def test_query_file_sits_beside_the_existing_window_totals():
    path = Path(COLLECTION_WINDOW_QUERY_PATH)
    assert path.name == "open_intelligence_coverage_collection_window_v1.sql"
    assert path.parent.name == "bigquery_queries"
    assert (path.parent / "open_intelligence_coverage_window_totals_v1.sql").is_file()


def test_query_text_declares_its_asserts_parameters_and_limit():
    text = _query_text()
    declared_asserts = re.findall(r"ASSERT\s.+?\sAS\s+'([a-z_]+)'", text, re.S)
    assert declared_asserts == list(COLLECTION_WINDOW_ASSERTS)
    assert len(set(declared_asserts)) == len(declared_asserts)
    assert {
        "window_is_ordered",
        "market_strata_are_declared",
        "query_limit_is_positive",
        "route_maps_are_unambiguous",
        "declared_routes_are_named",
        "known_quiet_routes_are_expected",
        "every_market_is_a_declared_stratum",
        "output_fits_the_query_limit",
    } <= set(declared_asserts)
    assert set(re.findall(r"@([a-z_]+)", text)) == set(COLLECTION_WINDOW_PARAMETERS)
    assert set(COLLECTION_WINDOW_PARAMETERS) == {
        "window_start",
        "window_end",
        "market_strata",
        "source_routes",
        "platform_routes",
        "expected_sources",
        "known_quiet",
        "query_limit",
    }
    assert text.rstrip().endswith("ORDER BY market, route;")
    for parameter in (
        "market_strata",
        "source_routes",
        "platform_routes",
        "expected_sources",
        "known_quiet",
    ):
        assert f"UNNEST(@{parameter})" in text


def test_query_reads_the_three_staging_tables_fully_qualified_and_writes_nothing():
    text = _query_text()
    for table in ("raw_content", "enriched_content", "pipeline_runs"):
        assert f"`ogilvy-trends-v2.trends_v2_staging.{table}`" in text
    code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    for keyword in ("INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP", "TRUNCATE"):
        assert re.search(rf"\b{keyword}\b", code) is None, keyword


def test_query_counts_distinct_identities_over_the_window_and_never_sums_them():
    text = _query_text()
    assert "COUNT(DISTINCT" in text
    assert "SUM(DISTINCT" not in text.upper()
    summed = re.findall(r"SUM\(\s*([a-z_.]+)", text, re.I)
    assert summed, "the market totals are sums of row counts"
    for column in summed:
        name = column.split(".")[-1]
        assert name in {"physical_rows", "total_rows", "route_rows"}, name
    assert "DATE(" not in text.upper(), "no per day grouping of distinct counts"


def test_query_names_every_output_column_in_contract_order():
    assert _final_select_columns(_query_text()) == list(COLLECTION_WINDOW_COLUMNS)
    assert COLLECTION_WINDOW_COLUMNS == (
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


def test_query_orders_control_states_as_the_contract_does():
    text = _query_text()
    positions = [
        text.index(f"THEN '{state}'")
        for state in ("reader_mismatch", "unresolved_route", "emitting", "failed", "known_quiet")
    ]
    assert positions == sorted(positions)
    assert "ELSE 'unexplained_zero'" in text
    for reason in (
        "recorded_failure",
        "declared_quiet",
        "zero_without_decision_record",
        "pipeline_rows_not_readable",
        "route_not_resolved",
    ):
        assert f"'{reason}'" in text
    assert "STARTS_WITH(" in text
    assert "CONCAT(routes.route, ':')" in text


def test_query_resolves_routes_by_source_then_platform_and_never_drops_a_row():
    text = _query_text()
    source_first = text.index("UNNEST(@source_routes)")
    assert source_first < text.index("UNNEST(@platform_routes)")
    assert f"'{UNRESOLVED_ROUTE}'" in text
    code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    assert "route IS NOT NULL" not in code
    assert "route != 'unresolved'" not in code


def test_query_asserts_every_stratum_is_expected_and_every_pipeline_market_is_a_stratum():
    assert "every_stratum_has_an_expected_route" in COLLECTION_WINDOW_ASSERTS
    assert "every_pipeline_market_is_a_declared_stratum" in COLLECTION_WINDOW_ASSERTS
    text = _query_text()
    pipeline_assert = text[: text.index("AS 'every_pipeline_market_is_a_declared_stratum'")]
    pipeline_assert = pipeline_assert[pipeline_assert.rindex("ASSERT") :]
    assert "`ogilvy-trends-v2.trends_v2_staging.pipeline_runs`" in pipeline_assert
    assert "total_rows" in pipeline_assert


def test_query_gives_failing_routes_and_pipeline_errors_a_row():
    text = _query_text()
    routes = text[text.index("\nroutes AS (") : text.index("\nfailures AS (")]
    assert "FROM error_routes" in routes
    errors = text[text.index("\nerror_routes AS (") : text.index("\nroutes AS (")]
    assert "REGEXP_EXTRACT(run_error" in errors
    assert f"'{PIPELINE_ROUTE}'" in errors
    assert "IN ('', 'unresolved', 'pipeline')" in text


def test_query_keeps_null_source_values_in_the_unresolved_list():
    text = _query_text()
    assert "ARRAY_AGG(DISTINCT IFNULL(source, '')" in text
    assert "ARRAY_AGG(DISTINCT source" not in text


def test_query_ranks_rows_and_returns_one_more_than_the_limit_without_limit_arithmetic():
    text = _query_text()
    code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    assert "ROW_NUMBER() OVER (ORDER BY market, route) AS row_rank" in code
    assert "WHERE row_rank <= @query_limit + 1" in code
    assert re.search(r"\bLIMIT\b", code) is None
    assert re.search(r"\bQUALIFY\b", code) is None
    final = code[code.rindex("\nSELECT\n") :]
    assert "FROM ranked" in final
    assert final.rstrip().endswith("ORDER BY market, route;")


CONNECTOR_ROW_COLUMNS = {
    "rss": "rss_rows",
    "youtube": "youtube_rows",
    "gdelt": "gdelt_rows",
    "ensemble": "ensemble_rows",
    "bigquery_trends": "bigquery_trends_rows",
    "reddit": "reddit_rows",
    "apple_music": "apple_music_rows",
    "wikipedia": "wikipedia_rows",
    "bluesky": "bluesky_rows",
    "google_trends_rss": "google_trends_rss_rows",
    "app_charts": "app_charts_rows",
    "audiomack": "audiomack_rows",
    "cloudflare_radar": "cloudflare_radar_rows",
    "youtube_scrape": "youtube_scrape_rows",
    "socialcrawl": "socialcrawl_rows",
}


def test_query_unpivots_each_connector_row_column_explicitly_and_no_sub_feed():
    text = _query_text()
    unpivot = text[text.index("\nroute_pipeline_rows AS (") :]
    unpivot = unpivot[: unpivot.index("\n),") + 3]
    mapped = dict(re.findall(r"STRUCT\('([a-z_]+)' AS route, ([a-z_]+) AS route_rows\)", unpivot))
    assert mapped == CONNECTOR_ROW_COLUMNS
    schema = (
        Path(COLLECTION_WINDOW_QUERY_PATH).parents[1] / "bigquery_schemas" / "pipeline_runs.sql"
    ).read_text(encoding="utf-8")
    for column in mapped.values():
        assert re.search(rf"^\s*{column} INT64", schema, re.M), column
    for sub_feed in ("top_terms_rows", "youtube_playlist_items_rows", "mention_reach_rows"):
        assert sub_feed not in text
    assert "SUM(route_rows)" in unpivot
    assert "'pipeline_route_rows_not_readable'" in text
    route_rule = text.index("WHEN pipeline_route_rows > 0 AND physical_rows = 0")
    assert route_rule < text.index("THEN 'emitting'")
    assert route_rule < text.index("THEN 'failed'")
    assert route_rule < text.index("THEN 'known_quiet'")


def test_query_requires_declared_routes_to_be_connector_keys():
    assert "declared_routes_are_connector_keys" in COLLECTION_WINDOW_ASSERTS
    text = _query_text()
    check = text[: text.index("AS 'declared_routes_are_connector_keys'")]
    check = check[check.rindex("ASSERT") :]
    assert "REGEXP_CONTAINS(route, r'^[a-z][a-z0-9_]*$')" in check
    assert "UNNEST(@expected_sources)" in check
    assert "UNNEST(@known_quiet)" in check


def test_query_keeps_inferred_identities_apart_from_native_ones():
    text = _query_text()
    assert "CONCAT(platform, ':', native_id)" in text
    assert "'native:'" in text
    assert "'url:'" in text


# Control state rules


def test_control_states_are_the_six_named():
    assert CONTROL_STATES == (
        "reader_mismatch",
        "unresolved_route",
        "emitting",
        "failed",
        "known_quiet",
        "unexplained_zero",
    )


def _state(**overrides):
    fields = {
        "physical_rows": 0,
        "recorded_failures": 0,
        "declared_quiet": False,
        "unresolved": False,
        "market_physical_rows": 4,
        "market_pipeline_rows": 4,
    }
    fields.update(overrides)
    return control_state_for(**fields)


def test_a_failed_source_with_zero_rows_is_failed_not_quiet():
    assert _state(recorded_failures=1, declared_quiet=True) == ("failed", "recorded_failure")


def test_a_zero_with_no_record_is_unexplained():
    assert _state() == ("unexplained_zero", "zero_without_decision_record")


def test_a_declared_quiet_zero_is_known_quiet():
    assert _state(declared_quiet=True) == ("known_quiet", "declared_quiet")


def test_pipeline_rows_with_no_readable_raw_rows_is_reader_mismatch():
    for failures, quiet in ((0, False), (2, False), (0, True)):
        state = _state(
            recorded_failures=failures,
            declared_quiet=quiet,
            market_physical_rows=0,
            market_pipeline_rows=12,
        )
        assert state == ("reader_mismatch", "pipeline_rows_not_readable")


def test_rows_present_are_emitting_even_with_a_recorded_failure():
    assert _state(physical_rows=3, recorded_failures=1) == ("emitting", None)


def test_rows_with_no_resolvable_route_are_unresolved_not_emitting():
    assert _state(physical_rows=3, unresolved=True) == ("unresolved_route", "route_not_resolved")


def test_pipeline_route_rows_without_raw_rows_are_a_route_reader_mismatch():
    mismatch = ("reader_mismatch", "pipeline_route_rows_not_readable")
    assert _state(declared_quiet=True, pipeline_route_rows=3) == mismatch
    assert _state(recorded_failures=2, pipeline_route_rows=3) == mismatch
    assert _state(pipeline_route_rows=3) == mismatch
    assert _state(physical_rows=3, pipeline_route_rows=3) == ("emitting", None)
    assert _state(declared_quiet=True, pipeline_route_rows=0) == ("known_quiet", "declared_quiet")
    assert _state(declared_quiet=True, pipeline_route_rows=None) == (
        "known_quiet",
        "declared_quiet",
    )
    assert _state(pipeline_route_rows=3, market_physical_rows=0, market_pipeline_rows=3) == (
        "reader_mismatch",
        "pipeline_rows_not_readable",
    )


def test_a_known_quiet_route_with_pipeline_route_rows_and_no_raw_rows_is_reader_mismatch():
    quiet = zero_row(
        control_state="known_quiet", control_reason="declared_quiet", pipeline_route_rows=4
    )
    with pytest.raises(ValueError, match="control_state_inconsistent"):
        validate_window_rows([row(), quiet])
    mismatch = zero_row(
        control_state="reader_mismatch",
        control_reason="pipeline_route_rows_not_readable",
        pipeline_route_rows=4,
    )
    assert validate_window_rows([row(), mismatch])[1]["control_reason"] == (
        "pipeline_route_rows_not_readable"
    )
    wrong_reason = {**mismatch, "control_reason": "pipeline_rows_not_readable"}
    with pytest.raises(ValueError, match="control_state_inconsistent"):
        validate_window_rows([row(), wrong_reason])


@pytest.mark.parametrize("value", [-1, True, 1.5, "4"])
def test_malformed_pipeline_route_rows_are_refused(value):
    with pytest.raises(ValueError, match="window_count_invalid:pipeline_route_rows"):
        validate_window_rows([row(pipeline_route_rows=value)])


def test_reserved_routes_carry_no_pipeline_route_rows():
    with pytest.raises(ValueError, match="pipeline_route_invalid"):
        validate_window_rows([row(), pipeline_row(pipeline_route_rows=2)])
    with pytest.raises(ValueError, match="unresolved_route_invalid"):
        validate_window_rows([unresolved_row(pipeline_route_rows=2)])


def test_reassigned_rows_are_bounded_by_the_route_rows_other_markets_collected():
    with pytest.raises(ValueError, match="identity_arithmetic_invalid:reassigned_market_rows"):
        validate_window_rows([row(reassigned_market_rows=1)])
    other = row(market="ng", reassigned_market_rows=0)
    assert (
        validate_window_rows([row(reassigned_market_rows=5), other])[0]["reassigned_market_rows"]
        == 5
    )
    with pytest.raises(ValueError, match="identity_arithmetic_invalid:reassigned_market_rows"):
        validate_window_rows([row(reassigned_market_rows=6), other])


# Validator refusals


def test_valid_rows_round_trip_with_timestamps_as_text():
    rows = validate_window_rows([row(), zero_row(), unresolved_row()])
    assert len(rows) == 3
    assert rows[0]["newest_published_at"] == "2026-09-20T12:00:00+00:00"
    assert rows[1]["newest_published_at"] is None
    assert rows[2]["unresolved_source_values"] == ["Mystery Feed"]
    assert tuple(rows[0]) == COLLECTION_WINDOW_COLUMNS
    json.dumps(rows)


def test_missing_column_is_refused():
    broken = row()
    del broken["rows_with_geo_method"]
    with pytest.raises(ValueError, match="window_row_columns_invalid"):
        validate_window_rows([broken])


def test_extra_column_is_refused():
    with pytest.raises(ValueError, match="window_row_columns_invalid"):
        validate_window_rows([row(topic_group_count=4)])


def test_the_old_source_column_is_refused():
    legacy = row()
    legacy["source"] = legacy.pop("route")
    with pytest.raises(ValueError, match="window_row_columns_invalid"):
        validate_window_rows([legacy])


@pytest.mark.parametrize("column", COUNT_COLUMNS)
def test_negative_count_is_refused(column):
    with pytest.raises(ValueError, match=f"window_count_invalid:{column}"):
        validate_window_rows([row(**{column: -1})])


@pytest.mark.parametrize("value", [True, 1.5, "3", None])
def test_non_integer_count_is_refused(value):
    with pytest.raises(ValueError, match="window_count_invalid:physical_rows"):
        validate_window_rows([row(physical_rows=value)])


def test_classified_above_qualified_is_refused():
    with pytest.raises(ValueError, match="identity_arithmetic_invalid:classified_observations"):
        validate_window_rows([row(qualified_observations=2, classified_observations=3)])


def test_unique_qualified_above_qualified_is_refused():
    with pytest.raises(
        ValueError, match="identity_arithmetic_invalid:unique_qualified_observations"
    ):
        validate_window_rows([row(qualified_observations=2, unique_qualified_observations=3)])


def test_more_identities_than_rows_is_refused():
    with pytest.raises(ValueError, match="identity_arithmetic_invalid:physical_rows"):
        validate_window_rows(
            [
                row(
                    physical_rows=4,
                    distinct_native_observations=2,
                    inferred_identity_observations=2,
                    rows_without_identity=1,
                    rows_with_geo_method=4,
                )
            ]
        )


def test_row_subsets_above_physical_rows_are_refused():
    with pytest.raises(ValueError, match="identity_arithmetic_invalid:rows_with_geo_method"):
        validate_window_rows([row(rows_with_geo_method=6)])
    with pytest.raises(ValueError, match="identity_arithmetic_invalid:rows_without_published_at"):
        validate_window_rows([row(rows_without_published_at=6)])


def test_unknown_control_state_is_refused():
    with pytest.raises(ValueError, match="control_state_invalid"):
        validate_window_rows([row(control_state="healthy")])


def test_emitting_claimed_with_zero_rows_is_refused():
    with pytest.raises(ValueError, match="control_state_inconsistent"):
        validate_window_rows([row(), zero_row(control_state="emitting", control_reason=None)])


def test_failed_zero_claimed_as_quiet_is_refused():
    failed = zero_row(
        recorded_failures=1, control_state="failed", control_reason="recorded_failure"
    )
    assert validate_window_rows([row(), failed])[1]["control_state"] == "failed"
    quiet = zero_row(
        recorded_failures=1, control_state="known_quiet", control_reason="declared_quiet"
    )
    with pytest.raises(ValueError, match="control_state_inconsistent"):
        validate_window_rows([row(), quiet])


def test_unrecorded_zero_claimed_as_failed_is_refused():
    with pytest.raises(ValueError, match="control_state_inconsistent"):
        validate_window_rows(
            [row(), zero_row(control_state="failed", control_reason="recorded_failure")]
        )


def test_known_quiet_route_not_expected_is_refused():
    quiet = zero_row(control_state="known_quiet", control_reason="declared_quiet")
    assert validate_window_rows([row(), quiet])[1]["control_state"] == "known_quiet"
    with pytest.raises(ValueError, match="known_quiet_not_expected"):
        validate_window_rows([row(), {**quiet, "expected": False}])


def test_pipeline_rows_without_raw_rows_must_be_reader_mismatch():
    blind = {"pipeline_reported_rows": 9}
    mismatch = zero_row(
        control_state="reader_mismatch", control_reason="pipeline_rows_not_readable", **blind
    )
    other = zero_row(
        route="rss",
        control_state="reader_mismatch",
        control_reason="pipeline_rows_not_readable",
        **blind,
    )
    assert {r["control_state"] for r in validate_window_rows([mismatch, other])} == {
        "reader_mismatch"
    }
    with pytest.raises(ValueError, match="control_state_inconsistent"):
        validate_window_rows([zero_row(**blind), other])


def test_reader_mismatch_claimed_where_the_market_has_rows_is_refused():
    with pytest.raises(ValueError, match="control_state_inconsistent"):
        validate_window_rows(
            [
                row(),
                zero_row(
                    control_state="reader_mismatch", control_reason="pipeline_rows_not_readable"
                ),
            ]
        )


def test_unresolved_rows_claimed_as_emitting_are_refused():
    with pytest.raises(ValueError, match="control_state_inconsistent"):
        validate_window_rows([unresolved_row(control_state="emitting", control_reason=None)])


def test_a_named_route_claimed_as_unresolved_is_refused():
    with pytest.raises(ValueError, match="control_state_inconsistent"):
        validate_window_rows(
            [
                row(
                    control_state="unresolved_route",
                    control_reason="route_not_resolved",
                    unresolved_source_values=["Mystery Feed"],
                )
            ]
        )


def test_unresolved_route_must_name_the_source_values_it_holds():
    with pytest.raises(ValueError, match="unresolved_source_values_invalid"):
        validate_window_rows([unresolved_row(unresolved_source_values=[])])
    with pytest.raises(ValueError, match="unresolved_source_values_invalid"):
        validate_window_rows([unresolved_row(unresolved_source_values=["a", "a"])])
    with pytest.raises(ValueError, match="unresolved_source_values_invalid"):
        validate_window_rows([unresolved_row(unresolved_source_values="Mystery Feed")])
    with pytest.raises(ValueError, match="unresolved_source_values_invalid"):
        validate_window_rows([row(unresolved_source_values=["Mystery Feed"])])


def test_unresolved_route_is_never_expected_and_never_empty():
    with pytest.raises(ValueError, match="unresolved_route_invalid"):
        validate_window_rows([unresolved_row(expected=True)])
    with pytest.raises(ValueError, match="unresolved_route_invalid"):
        validate_window_rows(
            [
                row(),
                zero_row(
                    route=UNRESOLVED_ROUTE,
                    expected=False,
                    control_state="unresolved_route",
                    control_reason="route_not_resolved",
                    unresolved_source_values=["Mystery Feed"],
                ),
            ]
        )


def test_pipeline_row_carries_market_level_failures():
    rows = validate_window_rows([row(), pipeline_row()])
    assert rows[1]["route"] == PIPELINE_ROUTE
    assert rows[1]["control_state"] == "failed"
    blind = {"pipeline_reported_rows": 9}
    mismatch = pipeline_row(
        control_state="reader_mismatch", control_reason="pipeline_rows_not_readable", **blind
    )
    other = zero_row(
        control_state="reader_mismatch", control_reason="pipeline_rows_not_readable", **blind
    )
    assert len(validate_window_rows([other, mismatch])) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected": True},
        {
            "recorded_failures": 0,
            "control_state": "known_quiet",
            "control_reason": "declared_quiet",
        },
        {"recorded_failures": 0},
        {"physical_rows": 1, "rows_without_identity": 1},
        {"qualified_observations": 1},
    ],
)
def test_pipeline_row_is_refused_unless_it_is_a_market_level_failure(overrides):
    with pytest.raises(ValueError, match=r"pipeline_route_invalid|control_state_inconsistent"):
        validate_window_rows([row(), pipeline_row(**overrides)])


def test_pipeline_rows_must_agree_within_a_market():
    with pytest.raises(ValueError, match="pipeline_reported_rows_inconsistent"):
        validate_window_rows([row(), zero_row(pipeline_reported_rows=7)])


def test_control_reason_must_match_the_state():
    with pytest.raises(ValueError, match="control_reason_invalid"):
        validate_window_rows([row(control_reason="recorded_failure")])
    with pytest.raises(ValueError, match="control_reason_invalid"):
        validate_window_rows([row(), zero_row(control_reason="declared_quiet")])


def test_duplicate_route_is_refused():
    with pytest.raises(ValueError, match="window_row_duplicate"):
        validate_window_rows([row(), row()])


def test_naive_or_malformed_timestamps_are_refused():
    with pytest.raises(ValueError, match="window_timestamp_invalid:newest_published_at"):
        validate_window_rows([row(newest_published_at=datetime(2026, 9, 20, 12))])
    with pytest.raises(ValueError, match="window_timestamp_invalid:newest_collected_at"):
        validate_window_rows([row(newest_collected_at="yesterday")])
    accepted = validate_window_rows([row(newest_collected_at="2026-09-20T13:00:00+00:00")])
    assert accepted[0]["newest_collected_at"] == "2026-09-20T13:00:00+00:00"


def test_blank_market_or_route_and_non_boolean_expected_are_refused():
    with pytest.raises(ValueError, match="window_field_invalid:market"):
        validate_window_rows([row(market="")])
    with pytest.raises(ValueError, match="window_field_invalid:route"):
        validate_window_rows([row(route=None)])
    with pytest.raises(ValueError, match="window_field_invalid:expected"):
        validate_window_rows([row(expected=1)])


# The retained window record


def test_fixture_readback_says_fixture_and_counts_states_per_market():
    rows = [
        row(),
        zero_row(recorded_failures=1, control_state="failed", control_reason="recorded_failure"),
        zero_row(route="apple_music", control_state="known_quiet", control_reason="declared_quiet"),
    ]
    readback = window_readback(
        rows,
        window=WINDOW,
        market_strata=STRATA,
        **declared(rows),
        query_limit=50,
        job_id=None,
        output_sha256=None,
    )
    assert readback["source"] == {"kind": "fixture"}
    assert readback["window"] == {
        "start": "2026-09-20T00:00:00+00:00",
        "end": "2026-09-21T00:00:00+00:00",
    }
    assert readback["market_strata"] == ["za", "ng", "ke"]
    assert readback["query_limit"] == 50
    assert readback["route_maps"] is None
    assert len(readback["rows"]) == 3
    assert readback["controls"]["za"] == {
        "reader_mismatch": 0,
        "unresolved_route": 0,
        "emitting": 1,
        "failed": 1,
        "known_quiet": 1,
        "unexplained_zero": 0,
    }
    assert readback["controls"]["ng"] == dict.fromkeys(CONTROL_STATES, 0)
    assert readback["zero_hides_nothing"] is True
    json.dumps(readback)


def test_native_readback_carries_the_job_digest_and_route_maps():
    maps = {
        "source_routes": [["Google Trends", "bigquery_trends"]],
        "platform_routes": [["web", "rss"]],
    }
    readback = window_readback(
        [row()],
        window=WINDOW,
        market_strata=STRATA,
        **declared([row()]),
        query_limit=10,
        job_id="job_abc",
        output_sha256=DIGEST,
        route_maps=maps,
    )
    assert readback["source"] == {
        "kind": "native_bigquery",
        "job_id": "job_abc",
        "output_sha256": DIGEST,
    }
    assert readback["route_maps"] == maps


def test_unexplained_zero_means_zero_hides_something():
    readback = window_readback(
        [row(), zero_row()],
        **declared([row(), zero_row()]),
        window=WINDOW,
        market_strata=STRATA,
        query_limit=10,
        job_id=None,
        output_sha256=None,
    )
    assert readback["zero_hides_nothing"] is False


def test_reader_mismatch_means_zero_hides_something():
    mismatch = zero_row(
        market="ng",
        pipeline_reported_rows=9,
        control_state="reader_mismatch",
        control_reason="pipeline_rows_not_readable",
    )
    readback = window_readback(
        [row(), mismatch],
        **declared([row(), mismatch]),
        window=WINDOW,
        market_strata=STRATA,
        query_limit=10,
        job_id=None,
        output_sha256=None,
    )
    assert readback["controls"]["ng"]["reader_mismatch"] == 1
    assert readback["zero_hides_nothing"] is False


def test_unresolved_rows_mean_zero_hides_something():
    quiet = zero_row(control_state="known_quiet", control_reason="declared_quiet")
    readback = window_readback(
        [row(), quiet, unresolved_row()],
        **declared([row(), quiet]),
        window=WINDOW,
        market_strata=STRATA,
        query_limit=10,
        job_id=None,
        output_sha256=None,
    )
    assert readback["controls"]["za"]["unresolved_route"] == 1
    assert readback["zero_hides_nothing"] is False


def test_readback_refuses_rows_outside_the_strata_or_above_the_limit():
    with pytest.raises(ValueError, match="window_market_not_a_stratum"):
        window_readback(
            [row(market="gh")],
            **declared([row(market="gh")]),
            window=WINDOW,
            market_strata=STRATA,
            query_limit=10,
            job_id=None,
            output_sha256=None,
        )
    with pytest.raises(ValueError, match="window_rows_exceed_limit"):
        window_readback(
            [row(), zero_row()],
            **declared([row(), zero_row()]),
            window=WINDOW,
            market_strata=STRATA,
            query_limit=1,
            job_id=None,
            output_sha256=None,
        )


def test_a_declared_quiet_route_reading_as_unexplained_is_refused():
    with pytest.raises(ValueError, match="known_quiet_mismatch"):
        window_readback(
            [row(), zero_row()],
            window=WINDOW,
            market_strata=STRATA,
            expected_sources=[("za", "rss"), ("za", "youtube")],
            known_quiet=[("za", "youtube")],
            query_limit=10,
            job_id=None,
            output_sha256=None,
        )


@pytest.mark.parametrize(
    ("expected", "quiet", "code"),
    [
        ([("za", "RSS"), ("za", "rss"), ("za", "youtube")], [], "expected_sources_invalid"),
        ([("za", "rss"), ("za", "youtube")], [("za", "you tube")], "known_quiet_invalid"),
    ],
)
def test_readback_refuses_declared_routes_that_are_not_connector_keys(expected, quiet, code):
    rows = [row(), zero_row(control_state="known_quiet", control_reason="declared_quiet")]
    with pytest.raises(ValueError, match=code):
        window_readback(
            rows,
            window=WINDOW,
            market_strata=STRATA,
            expected_sources=expected,
            known_quiet=quiet,
            query_limit=10,
            job_id=None,
            output_sha256=None,
        )


def test_readback_retains_the_declared_expected_and_known_quiet_routes():
    rows = [row(), zero_row(control_state="known_quiet", control_reason="declared_quiet")]
    readback = window_readback(
        rows,
        window=WINDOW,
        market_strata=STRATA,
        expected_sources=[("za", "rss"), ("za", "youtube")],
        known_quiet=[("za", "youtube")],
        query_limit=10,
        job_id=None,
        output_sha256=None,
    )
    assert readback["expected_sources"] == [["za", "rss"], ["za", "youtube"]]
    assert readback["known_quiet"] == [["za", "youtube"]]
    json.dumps(readback)


@pytest.mark.parametrize(
    ("expected", "quiet", "code"),
    [
        ([("za", "rss")], [], "expected_routes_mismatch"),
        ([("za", "rss"), ("za", "youtube"), ("ng", "rss")], [], "expected_routes_mismatch"),
        ([("za", "rss"), ("za", "youtube")], [], "known_quiet_mismatch"),
        ([("za", "rss"), ("za", "youtube")], [("ng", "rss")], "known_quiet_not_expected"),
        ([("za", "rss"), ("za", "youtube"), ("za", "rss")], [], "expected_sources_invalid"),
        ([("gh", "rss"), ("za", "rss"), ("za", "youtube")], [], "expected_sources_invalid"),
        ([("za", "rss"), ("za", "youtube"), ("za", "pipeline")], [], "expected_sources_invalid"),
        ("za:rss", [], "expected_sources_invalid"),
    ],
)
def test_readback_refuses_declarations_the_rows_do_not_bear_out(expected, quiet, code):
    rows = [row(), zero_row(control_state="known_quiet", control_reason="declared_quiet")]
    with pytest.raises(ValueError, match=code):
        window_readback(
            rows,
            window=WINDOW,
            market_strata=STRATA,
            expected_sources=expected,
            known_quiet=quiet,
            query_limit=10,
            job_id=None,
            output_sha256=None,
        )


@pytest.mark.parametrize(
    ("window", "strata", "limit", "job_id", "digest", "code"),
    [
        ((END, START), STRATA, 10, None, None, "window_invalid"),
        ((START.replace(tzinfo=None), END), STRATA, 10, None, None, "window_invalid"),
        (WINDOW, (), 10, None, None, "market_strata_invalid"),
        (WINDOW, ("za", "za"), 10, None, None, "market_strata_invalid"),
        (WINDOW, STRATA, 0, None, None, "query_limit_invalid"),
        (WINDOW, STRATA, True, None, None, "query_limit_invalid"),
        (WINDOW, STRATA, 10, "job_abc", None, "output_sha256_invalid"),
        (WINDOW, STRATA, 10, "job_abc", "XYZ", "output_sha256_invalid"),
        (WINDOW, STRATA, 10, "", DIGEST, "job_id_invalid"),
        (WINDOW, STRATA, 10, None, DIGEST, "fixture_carries_no_digest"),
    ],
)
def test_readback_refuses_malformed_bindings(window, strata, limit, job_id, digest, code):
    with pytest.raises(ValueError, match=code):
        window_readback(
            [row()],
            **declared([row()]),
            window=window,
            market_strata=strata,
            query_limit=limit,
            job_id=job_id,
            output_sha256=digest,
        )


# Read only runner


def _runner():
    return importlib.import_module("scripts.staging.read_coverage_window")


class _Job:
    def __init__(self, rows, job_id="job_window_1"):
        self._rows = rows
        self.job_id = job_id
        self.total_bytes_processed = 1234

    def result(self):
        return iter(self._rows)


class _Client:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return _Job(self.rows)

    def __getattr__(self, name):
        raise AssertionError(f"the runner touched client.{name}")


NOW = datetime(2026, 9, 25, 8, 30, 5, tzinfo=UTC)
ARGV = [
    "--maximum-bytes-billed",
    "5000000000",
    "--window-start",
    "2026-09-20T00:00:00+00:00",
    "--window-end",
    "2026-09-21T00:00:00+00:00",
    "--market",
    "za",
    "--market",
    "ng",
    "--market",
    "ke",
    "--source-route",
    "Google Trends:bigquery_trends",
    "--source-route",
    "youtube_scrape:youtube_scrape",
    "--platform-route",
    "web:rss",
    "--platform-route",
    "youtube:youtube",
    "--expected",
    "za:rss",
    "--expected",
    "za:youtube",
    "--expected",
    "ng:gdelt",
    "--expected",
    "ke:gdelt",
    "--known-quiet",
    "za:youtube",
    "--query-limit",
    "40",
]


def _sent_parameters(config) -> dict:
    # The payload the job sends; the library cannot rebuild an empty struct
    # array from it, so the typed parameters are read as sent.
    return {p["name"]: p for p in config.to_api_repr()["query"]["queryParameters"]}


def _struct_fields(parameter) -> list[tuple[str, str]]:
    array_type = parameter["parameterType"]["arrayType"]
    assert array_type["type"] == "STRUCT"
    return [(field["name"], field["type"]["type"]) for field in array_type["structTypes"]]


def test_runner_builds_typed_parameters():
    from google.cloud import bigquery

    runner = _runner()
    config = runner.build_job_config(
        bigquery,
        window_start=START,
        window_end=END,
        market_strata=("za", "ng"),
        source_routes=(("Google Trends", "bigquery_trends"),),
        platform_routes=(("web", "rss"),),
        expected_sources=(("za", "rss"), ("ng", "rss")),
        known_quiet=(),
        query_limit=40,
        maximum_bytes_billed=5_000_000_000,
    )
    parameters = _sent_parameters(config)
    assert set(parameters) == {
        "window_start",
        "window_end",
        "market_strata",
        "source_routes",
        "platform_routes",
        "expected_sources",
        "known_quiet",
        "query_limit",
    }
    assert parameters["window_start"]["parameterType"] == {"type": "TIMESTAMP"}
    assert parameters["window_end"]["parameterType"] == {"type": "TIMESTAMP"}
    assert parameters["query_limit"]["parameterType"] == {"type": "INT64"}
    assert parameters["query_limit"]["parameterValue"] == {"value": "40"}
    assert parameters["market_strata"]["parameterType"] == {
        "type": "ARRAY",
        "arrayType": {"type": "STRING"},
    }
    assert _struct_fields(parameters["source_routes"]) == [
        ("source", "STRING"),
        ("route", "STRING"),
    ]
    assert _struct_fields(parameters["platform_routes"]) == [
        ("platform", "STRING"),
        ("route", "STRING"),
    ]
    for name in ("expected_sources", "known_quiet"):
        assert _struct_fields(parameters[name]) == [("market", "STRING"), ("route", "STRING")]
    assert parameters["source_routes"]["parameterValue"]["arrayValues"] == [
        {
            "structValues": {
                "source": {"value": "Google Trends"},
                "route": {"value": "bigquery_trends"},
            }
        }
    ]
    assert parameters["expected_sources"]["parameterValue"]["arrayValues"] == [
        {"structValues": {"market": {"value": "za"}, "route": {"value": "rss"}}},
        {"structValues": {"market": {"value": "ng"}, "route": {"value": "rss"}}},
    ]
    assert parameters["known_quiet"]["parameterValue"]["arrayValues"] == []
    assert config.dry_run is not True
    assert config.destination is None
    assert config.maximum_bytes_billed == 5_000_000_000


def test_runner_dry_run_sends_the_query_file_and_writes_nothing(tmp_path, capsys):
    runner = _runner()
    client = _Client([])
    code = runner.main([*ARGV, "--output-dir", str(tmp_path), "--dry-run"], client=client, now=NOW)
    assert code == 0
    assert list(tmp_path.iterdir()) == []
    [(sql, config)] = client.calls
    assert sql == Path(COLLECTION_WINDOW_QUERY_PATH).read_text(encoding="utf-8")
    assert config.dry_run is True
    assert config.use_query_cache is False
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_runner_saves_rows_and_readback_with_a_utc_stamp(tmp_path):
    runner = _runner()
    rows = [
        row(),
        zero_row(control_state="known_quiet", control_reason="declared_quiet"),
        row(market="ng", route="gdelt"),
        row(market="ke", route="gdelt"),
    ]
    client = _Client(rows)
    code = runner.main([*ARGV, "--output-dir", str(tmp_path)], client=client, now=NOW)
    assert code == 0
    rows_path = tmp_path / "coverage_window_rows_20260925T083005Z.json"
    readback_path = tmp_path / "coverage_window_readback_20260925T083005Z.json"
    assert sorted(tmp_path.iterdir()) == sorted([rows_path, readback_path])
    readback = json.loads(readback_path.read_text(encoding="utf-8"))
    assert readback["source"] == {
        "kind": "native_bigquery",
        "job_id": "job_window_1",
        "output_sha256": hashlib.sha256(rows_path.read_bytes()).hexdigest(),
    }
    assert readback["market_strata"] == ["za", "ng", "ke"]
    assert readback["query_limit"] == 40
    assert readback["route_maps"] == {
        "source_routes": [
            ["Google Trends", "bigquery_trends"],
            ["youtube_scrape", "youtube_scrape"],
        ],
        "platform_routes": [["web", "rss"], ["youtube", "youtube"]],
    }
    assert readback["zero_hides_nothing"] is True
    assert readback["expected_sources"] == [
        ["za", "rss"],
        ["za", "youtube"],
        ["ng", "gdelt"],
        ["ke", "gdelt"],
    ]
    assert readback["known_quiet"] == [["za", "youtube"]]
    saved = json.loads(rows_path.read_text(encoding="utf-8"))
    assert saved[0]["newest_published_at"] == "2026-09-20T12:00:00+00:00"
    [(_, config)] = client.calls
    assert config.dry_run is not True
    assert config.maximum_bytes_billed == 5_000_000_000


def test_runner_refuses_to_overwrite_and_leaves_existing_output(tmp_path):
    runner = _runner()
    existing = tmp_path / "coverage_window_readback_20260925T083005Z.json"
    existing.write_text("kept", encoding="utf-8")
    client = _Client(RUNNER_ROWS)
    code = runner.main([*ARGV, "--output-dir", str(tmp_path)], client=client, now=NOW)
    assert code != 0
    assert existing.read_text(encoding="utf-8") == "kept"
    assert list(tmp_path.iterdir()) == [existing]


RUNNER_ROWS = [
    row(),
    zero_row(control_state="known_quiet", control_reason="declared_quiet"),
    row(market="ng", route="gdelt"),
    row(market="ke", route="gdelt"),
]


REFUSED_ROWS_PATH = "coverage_window_rows_20260925T083005Z.json"


def test_runner_refuses_more_rows_than_the_limit_and_keeps_only_the_raw_rows(tmp_path):
    runner = _runner()
    argv = [*ARGV[: ARGV.index("--query-limit")], "--query-limit", "3"]
    client = _Client(RUNNER_ROWS)
    code = runner.main([*argv, "--output-dir", str(tmp_path)], client=client, now=NOW)
    assert code != 0
    assert list(tmp_path.iterdir()) == [tmp_path / REFUSED_ROWS_PATH]
    saved = json.loads((tmp_path / REFUSED_ROWS_PATH).read_text(encoding="utf-8"))
    assert len(saved) == len(RUNNER_ROWS)


def test_save_leaves_no_file_alone_when_a_later_open_fails(tmp_path):
    runner = _runner()
    first = tmp_path / "rows.json"
    second = tmp_path / "missing" / "readback.json"
    with pytest.raises(OSError):
        runner._save((first, second), (b"rows", b"readback"))
    assert list(tmp_path.iterdir()) == []


def test_save_never_deletes_and_leaves_an_existing_file_untouched(tmp_path, monkeypatch):
    runner = _runner()
    first = tmp_path / "rows.json"
    second = tmp_path / "readback.json"
    second.write_bytes(b"earlier")

    def refuse_unlink(self, *args, **kwargs):
        raise AssertionError("output_directory_is_append_only")

    monkeypatch.setattr(Path, "unlink", refuse_unlink)
    with pytest.raises(FileExistsError):
        runner._save((first, second), (b"rows", b"readback"))
    assert not first.exists()
    assert second.read_bytes() == b"earlier"


def test_runner_refuses_a_stratum_with_no_expected_route(tmp_path):
    runner = _runner()
    client = _Client(RUNNER_ROWS)
    argv = [*ARGV, "--market", "gh", "--output-dir", str(tmp_path)]
    with pytest.raises(SystemExit) as refusal:
        runner.main(argv, client=client, now=NOW)
    assert refusal.value.code != 0
    assert client.calls == []


def test_runner_keeps_the_raw_rows_of_a_refused_read_and_no_readback(tmp_path, capsys):
    runner = _runner()
    client = _Client([row(control_state="healthy")])
    code = runner.main([*ARGV, "--output-dir", str(tmp_path)], client=client, now=NOW)
    assert code != 0
    assert list(tmp_path.iterdir()) == [tmp_path / REFUSED_ROWS_PATH]
    saved = json.loads((tmp_path / REFUSED_ROWS_PATH).read_text(encoding="utf-8"))
    assert saved[0]["control_state"] == "healthy"
    assert "control_state_invalid" in capsys.readouterr().err


def test_a_refused_read_never_replaces_an_earlier_raw_rows_file(tmp_path):
    runner = _runner()
    earlier = tmp_path / REFUSED_ROWS_PATH
    earlier.write_bytes(b"earlier")
    client = _Client([row(control_state="healthy")])
    code = runner.main([*ARGV, "--output-dir", str(tmp_path)], client=client, now=NOW)
    assert code != 0
    assert earlier.read_bytes() == b"earlier"
    assert list(tmp_path.iterdir()) == [earlier]


@pytest.mark.parametrize(
    "bad",
    [
        ["--expected", "za"],
        ["--expected", "za:"],
        ["--expected", "za:unresolved"],
        ["--expected", "za:pipeline"],
        ["--expected", "za:RSS"],
        ["--known-quiet", "za:You_tube"],
        ["--source-route", "Mystery:pipeline"],
        ["--known-quiet", "ng:rss"],
        ["--source-route", "Google Trends:rss"],
        ["--source-route", "Mystery:unresolved"],
        ["--platform-route", "web:youtube"],
        ["--platform-route", ":rss"],
        ["--window-end", "2026-09-19T00:00:00+00:00"],
        ["--window-end", "2026-09-21T00:00:00"],
        ["--query-limit", "0"],
    ],
)
def test_runner_refuses_malformed_arguments_before_querying(tmp_path, bad):
    runner = _runner()
    client = _Client([])
    argv = [*ARGV, *bad, "--output-dir", str(tmp_path)]
    with pytest.raises(SystemExit) as refusal:
        runner.main(argv, client=client, now=NOW)
    assert refusal.value.code != 0
    assert client.calls == []


def test_runner_refuses_a_query_text_that_writes():
    runner = _runner()
    with pytest.raises(ValueError, match="query_text_writes"):
        runner.read_only_query_text("INSERT INTO t SELECT 1")
    assert runner.read_only_query_text(Path(COLLECTION_WINDOW_QUERY_PATH).read_text("utf-8"))


ROUTE_ROW_COLUMNS = (
    "rss_rows",
    "youtube_rows",
    "gdelt_rows",
    "ensemble_rows",
    "bigquery_trends_rows",
    "reddit_rows",
    "apple_music_rows",
    "wikipedia_rows",
    "bluesky_rows",
    "google_trends_rss_rows",
    "app_charts_rows",
    "audiomack_rows",
    "cloudflare_radar_rows",
    "youtube_scrape_rows",
    "socialcrawl_rows",
)


def test_a_route_the_pipeline_reports_rows_for_always_gets_a_row_and_counts_toward_the_limit():
    # A connector that reports rows but is neither expected, observed nor failing
    # must still get a row, or its broken reader would never read as a mismatch.
    text = _query_text()
    routes_block = text.split("\nroutes AS (", 1)[1].split("\n),", 1)[0]
    assert "FROM route_pipeline_rows" in routes_block
    assert "pipeline_route_rows > 0" in routes_block
    limit_assert = text.split("AS 'output_fits_the_query_limit'", 1)[0].rsplit("ASSERT", 1)[1]
    for column in ROUTE_ROW_COLUMNS:
        assert column in limit_assert, column


def _without_cap(argv):
    out = list(argv)
    at = out.index("--maximum-bytes-billed")
    del out[at : at + 2]
    return out


def test_runner_refuses_a_real_read_without_a_byte_cap(tmp_path):
    runner = _runner()
    client = _Client([row()])
    with pytest.raises(SystemExit) as refusal:
        runner.main([*_without_cap(ARGV), "--output-dir", str(tmp_path)], client=client, now=NOW)
    assert refusal.value.code != 0
    assert client.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("cap", ["0", "-1", "1e9", "many"])
def test_runner_refuses_a_byte_cap_that_is_not_a_positive_integer(tmp_path, cap):
    runner = _runner()
    client = _Client([row()])
    argv = [*_without_cap(ARGV), "--maximum-bytes-billed", cap, "--output-dir", str(tmp_path)]
    with pytest.raises(SystemExit):
        runner.main(argv, client=client, now=NOW)
    assert client.calls == []


def test_a_dry_run_needs_no_byte_cap(tmp_path):
    runner = _runner()
    client = _Client([])
    argv = [*_without_cap(ARGV), "--output-dir", str(tmp_path), "--dry-run"]
    assert runner.main(argv, client=client, now=NOW) == 0
    [(_, config)] = client.calls
    assert config.dry_run is True


def test_a_real_job_config_cannot_be_built_without_a_byte_cap():
    from google.cloud import bigquery

    with pytest.raises(ValueError, match=r"^maximum_bytes_billed_required$"):
        _runner().build_job_config(
            bigquery,
            window_start=START,
            window_end=END,
            market_strata=("za",),
            source_routes=(),
            platform_routes=(),
            expected_sources=(("za", "rss"),),
            known_quiet=(),
            query_limit=40,
        )
