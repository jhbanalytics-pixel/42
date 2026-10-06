import copy
import importlib
import re
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime

import pytest
from google.cloud import bigquery
from src.analysis.open_intelligence.general_question_plan import validate_question_plan
from src.analysis.open_intelligence.general_question_policy import build_intake_context
from src.analysis.open_intelligence.general_question_request import normalize_question_request

NOW = datetime(2026, 9, 6, 20, 30, tzinfo=UTC)


def module():
    return importlib.import_module(
        "src.analysis.open_intelligence.general_question_retrieval_queries"
    )


def inputs(*, requirements=None, markets=None, scope=None):
    scope = scope or {
        "client_scope_id": "synthetic_scope",
        "market_scope": ["ke", "ng", "za"],
        "brand_config_id": None,
        "audience_lens_ids": [],
        "theme_id": None,
    }
    request = normalize_question_request(
        {
            "message": "How are unfamiliar mobility rituals changing?",
            "history": [],
        },
        scope=scope,
        request_id="00000000-0000-4000-8000-000000000021",
        admitted_at=NOW,
        policy_digest="a" * 64,
        requested_window={"start": "2026-08-20", "end": "2026-09-05"},
    )
    intake = build_intake_context(request, selected_market="ng")
    plan = validate_question_plan(
        {
            "status": "ready",
            "intent": "discovery",
            "markets": markets or ["ng"],
            "window": {"start": "2026-08-20", "end": "2026-09-05", "closed": True},
            "decision": None,
            "requirements": requirements
            or [
                {
                    "requirement_id": "mobility",
                    "question": "Which observed mobility rituals matter?",
                    "kind": "content",
                    "mandatory": True,
                    "search_terms": ["night commute"],
                }
            ],
            "clarification": None,
            "limitations": [],
        },
        request=request,
        intake=intake,
    )
    return request, plan, intake


def build(request, plan, intake, candidate_limit=100):
    return module().build_released_candidate_query(
        request, plan, intake, candidate_limit=candidate_limit
    )


def parameters_by_name(query):
    return {parameter.name: parameter for parameter in query.parameters}


def test_unseen_terms_change_bound_relevance_without_changing_server_sql():
    request, first_plan, intake = inputs()
    second_plan = copy.deepcopy(first_plan)
    second_plan["requirements"][0]["search_terms"] = ["repair culture"]
    second_plan = validate_question_plan(
        {
            key: second_plan[key]
            for key in (
                "status",
                "intent",
                "markets",
                "window",
                "decision",
                "requirements",
                "clarification",
                "limitations",
            )
        },
        request=request,
        intake=intake,
    )

    first = build(request, first_plan, intake)
    second = build(request, second_plan, intake)

    assert first.sql == second.sql
    assert first.sql_digest == second.sql_digest
    assert first.parameters_digest != second.parameters_digest
    assert parameters_by_name(first)["search_terms"].values == ("night commute",)
    assert parameters_by_name(second)["search_terms"].values == ("repair culture",)
    assert "night commute" not in first.sql
    assert "repair culture" not in second.sql
    assert "matched_term_count DESC" in first.sql


def test_null_scope_market_subset_window_and_cutoffs_are_typed_parameters():
    request, plan, intake = inputs(markets=["ng", "za"])
    query = build(request, plan, intake, candidate_limit=73)
    parameters = parameters_by_name(query)

    assert query.template_id == "released_candidates_v1"
    assert query.candidate_limit == 73
    assert query.transport_row_limit == 73
    assert isinstance(query.parameters, tuple)
    assert all(
        isinstance(item, (bigquery.ScalarQueryParameter, bigquery.ArrayQueryParameter))
        for item in query.parameters
    )
    assert parameters["client_scope_id"].value == "synthetic_scope"
    assert parameters["markets"].values == ("ng", "za")
    assert "brand_config_id" not in parameters
    assert "audience_lens_ids" not in parameters
    assert "theme_id" not in parameters
    assert parameters["window_start"].value == date(2026, 8, 20)
    assert parameters["window_end"].value == date(2026, 9, 5)
    assert parameters["as_of"].value == NOW
    assert parameters["release_day_sast"].value == date(2026, 9, 6)
    assert parameters["candidate_limit"].value == 73
    assert parameters["window_start"].type_ == "DATE"
    assert parameters["as_of"].type_ == "TIMESTAMP"
    assert parameters["candidate_limit"].type_ == "INT64"
    assert parameters["markets"].array_type == "STRING"


def test_request_presentation_context_does_not_change_source_eligibility_binding():
    baseline_request, baseline_plan, baseline_intake = inputs()
    configured_scope = {
        "client_scope_id": "synthetic_scope",
        "market_scope": ["ke", "ng", "za"],
        "brand_config_id": "configured_brand",
        "audience_lens_ids": [],
        "theme_id": "presentation_theme",
    }
    themed_request, themed_plan, themed_intake = inputs(scope=configured_scope)

    baseline = build(baseline_request, baseline_plan, baseline_intake)
    themed = build(themed_request, themed_plan, themed_intake)

    assert baseline.sql == themed.sql
    assert baseline.parameters_digest == themed.parameters_digest
    assert parameters_by_name(baseline) == parameters_by_name(themed)


def test_requested_lens_does_not_become_measured_source_data():
    scope = {
        "client_scope_id": "synthetic_scope",
        "market_scope": ["ke", "ng", "za"],
        "brand_config_id": None,
        "audience_lens_ids": ["authorized_lens"],
        "theme_id": None,
    }
    request, plan, intake = inputs(scope=scope)
    query = build(request, plan, intake)

    assert "audience_lens_ids" not in parameters_by_name(query)
    assert "ARRAY_LENGTH(IFNULL(c.audience_lens_ids, ARRAY<STRING>[])) = 0" in query.sql


def test_no_term_discovery_and_nonreleased_requirement_kinds_do_not_fake_matches():
    requirements = [
        {
            "requirement_id": "history",
            "question": "What earlier context is admitted?",
            "kind": "history",
            "mandatory": True,
            "search_terms": ["must not become a released match"],
        },
        {
            "requirement_id": "external",
            "question": "Which external fact is admitted?",
            "kind": "external_fact",
            "mandatory": False,
            "search_terms": ["also excluded"],
        },
    ]
    request, plan, intake = inputs(requirements=requirements)
    with pytest.raises(ValueError, match="candidate_query_invalid"):
        build(request, plan, intake)


def test_content_requirement_with_no_terms_keeps_bounded_broad_discovery():
    requirements = [
        {
            "requirement_id": "content",
            "question": "Which eligible released records matter?",
            "kind": "content",
            "mandatory": True,
            "search_terms": [],
        },
        {
            "requirement_id": "external",
            "question": "Which external fact is admitted?",
            "kind": "external_fact",
            "mandatory": False,
            "search_terms": ["must stay excluded"],
        },
    ]
    request, plan, intake = inputs(requirements=requirements)
    query = build(request, plan, intake)

    assert parameters_by_name(query)["search_terms"].values == ()
    assert "ARRAY_LENGTH(@search_terms) = 0" in query.sql
    assert "must stay excluded" not in query.sql


def test_run_window_overlap_and_explicit_utc_publication_bounds_are_separate():
    request, plan, intake = inputs()
    query = build(request, plan, intake)
    parameters = parameters_by_name(query)

    assert parameters["publication_start"].value == datetime(2026, 8, 20, tzinfo=UTC)
    assert parameters["publication_end_exclusive"].value == datetime(2026, 9, 6, tzinfo=UTC)
    assert "observation_start <= @window_end" in query.sql
    assert "observation_end >= @window_start" in query.sql
    assert "c.signal_date BETWEEN @window_start AND @window_end" not in query.sql
    assert "published_at >= @publication_start" in query.sql
    assert "published_at < @publication_end_exclusive" in query.sql


def test_evidence_ranking_uses_exact_scope_contract_and_creation_cutoff():
    request, plan, intake = inputs()
    query = build(request, plan, intake)
    evidence = query.sql.split("evidence_rollup AS (", 1)[1].split("),\neligible AS", 1)[0]

    for fragment in (
        "contract_version = @contract_version",
        "created_at <= @as_of",
    ):
        assert fragment in evidence
    assert "brand_config_id IS NOT DISTINCT FROM @brand_config_id" not in evidence
    assert "TO_JSON_STRING(audience_lens_ids) = TO_JSON_STRING(@audience_lens_ids)" not in evidence
    assert "theme_id IS NOT DISTINCT FROM @theme_id" not in evidence
    assert "LEFT JOIN evidence_rollup" not in query.sql
    assert "qualified_evidence_count > 0" in query.sql


def test_evidence_must_match_candidate_producer_context_and_client():
    request, plan, intake = inputs()
    sql = build(request, plan, intake).sql
    join = sql.split("JOIN evidence_rollup AS e", 1)[1].split("WHERE c.contract_version", 1)[0]

    for fragment in (
        "e.client_scope_id = c.client_scope_id",
        "e.brand_config_id IS NOT DISTINCT FROM c.brand_config_id",
        "TO_JSON_STRING(e.audience_lens_ids) = TO_JSON_STRING(c.audience_lens_ids)",
        "e.theme_id IS NOT DISTINCT FROM c.theme_id",
    ):
        assert fragment in join
    assert "c.client_scope_id = @client_scope_id" in sql


def test_duplicate_cardinality_is_measured_before_eligibility_filters():
    request, plan, intake = inputs()
    sql = build(request, plan, intake).sql

    receipt_rows = sql.split("receipt_rows AS (", 1)[1].split("),\nreceipts AS", 1)[0]
    release_rows = sql.split("release_rows AS (", 1)[1].split("),\nreleases AS", 1)[0]
    assert "COUNT(*) OVER (PARTITION BY run_id) AS run_identity_count" in receipt_rows
    assert "status = 'completed'" not in receipt_rows
    assert "COUNT(*) OVER (PARTITION BY run_id) AS release_identity_count" in release_rows
    assert "release_contract_version = @release_contract_version" not in release_rows
    assert "run_identity_count = 1" in sql
    assert "release_identity_count = 1" in sql


def test_terms_are_lowercased_deduplicated_and_detached_from_inputs():
    requirements = [
        {
            "requirement_id": "content",
            "question": "Which records matter?",
            "kind": "content",
            "mandatory": True,
            "search_terms": ["  Night Commute  ", "night commute", "CAFÉ"],
        }
    ]
    request, plan, intake = inputs(requirements=requirements)
    query = build(request, plan, intake)
    plan["requirements"][0]["search_terms"][0] = "mutated"

    assert parameters_by_name(query)["search_terms"].values == ("night commute", "café")
    with pytest.raises(FrozenInstanceError):
        query.candidate_limit = 1


def test_untrusted_scope_and_term_text_never_enters_server_owned_sql():
    scope = {
        "client_scope_id": "scope` JOIN foreign_table",
        "market_scope": ["ng"],
        "brand_config_id": "brand' OR TRUE",
        "audience_lens_ids": ["lens); DELETE"],
        "theme_id": "theme/* injected */",
    }
    requirements = [
        {
            "requirement_id": "content",
            "question": "Which records matter?",
            "kind": "content",
            "mandatory": True,
            "search_terms": ["x') DROP TABLE synthetic;"],
        }
    ]
    request, plan, intake = inputs(requirements=requirements, scope=scope)
    query = build(request, plan, intake)
    parameters = parameters_by_name(query)

    for value in (
        scope["client_scope_id"],
        scope["brand_config_id"],
        scope["audience_lens_ids"][0],
        scope["theme_id"],
        "x') drop table synthetic;",
    ):
        assert value not in query.sql
    assert parameters["client_scope_id"].value == scope["client_scope_id"]
    assert parameters["search_terms"].values == ("x') drop table synthetic;",)


@pytest.mark.parametrize("candidate_limit", [True, 0, -1, 1001, 1.5, "10", None])
def test_candidate_limit_is_a_positive_integer_within_the_request_ceiling(candidate_limit):
    request, plan, intake = inputs()
    with pytest.raises(ValueError, match="candidate_query_invalid"):
        build(request, plan, intake, candidate_limit=candidate_limit)


@pytest.mark.parametrize("target", ["request", "plan", "intake"])
def test_changed_stored_bindings_refuse_with_a_controlled_error(target):
    request, plan, intake = inputs()
    value = {"request": request, "plan": plan, "intake": intake}[target]
    value["request_digest" if target != "intake" else "intake_digest"] = "b" * 64

    with pytest.raises(ValueError, match="candidate_query_invalid"):
        build(request, plan, intake)


def test_sql_is_fixed_select_only_and_exposes_named_candidate_count_semantics():
    request, plan, intake = inputs()
    query = build(request, plan, intake)
    normalized = " ".join(query.sql.split())

    assert normalized.startswith("WITH ")
    assert re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|CALL)\b", normalized) is None
    assert set(re.findall(r"`([^`]+)`", query.sql)) == {
        "ogilvy-trends-v2.trends_v2_staging.open_intelligence_run_receipts_v1",
        "ogilvy-trends-v2.trends_v2_staging.open_intelligence_quality_release_records_v2",
        "ogilvy-trends-v2.trends_v2_staging.signal_candidates_v2",
        "ogilvy-trends-v2.trends_v2_staging.signal_evidence_v2",
        "ogilvy-trends-v2.trends_v2_staging.signal_outcomes_v2",
    }
    assert "full_matched_candidate_count" in query.sql
    assert "candidate_rows_truncated" in query.sql
    assert "latest_evidence_published_at" in query.sql
    assert "release_day_sast" in query.sql
    assert "released_at" in query.sql
    assert "receipt_cluster_build_version" in query.sql
    assert "SELECT\n  *,\n  full_matched_candidate_count" not in query.sql


def test_complete_run_selection_hints_keep_declared_and_observed_counts_distinct():
    request, plan, intake = inputs()
    module_value = module()
    query = build(request, plan, intake)

    assert module_value._RESULT_COLUMNS[30:37] == (
        "candidate_count",
        "evidence_count",
        "membership_count",
        "lineage_count",
        "analysis_count",
        "prediction_count",
        "outcome_count_at_completion",
    )
    for field in module_value._RESULT_COLUMNS[30:36]:
        assert f"r.{field}" in query.sql
    assert "o.outcome_count_at_completion" in query.sql
    assert "COALESCE(o.outcome_count_at_completion, 0)" in query.sql


def test_outcome_hint_counts_exact_run_identity_at_receipt_completion():
    request, plan, intake = inputs()
    query = build(request, plan, intake)
    outcome_cte = query.sql.split("outcome_counts AS (", 1)[1].split("),\nevidence_rollup", 1)[0]

    for fragment in (
        "o.client_scope_id = r.client_scope_id",
        "o.run_id = r.run_id",
        "o.signal_date = r.signal_date",
        "o.contract_version = @contract_version",
        "o.evaluated_at <= r.completed_at",
        "COUNT(*) AS outcome_count_at_completion",
    ):
        assert fragment in outcome_cte


def test_count_hints_do_not_change_candidate_eligibility_order_or_limit():
    request, plan, intake = inputs()
    sql = build(request, plan, intake, candidate_limit=17).sql

    assert "AND c.client_scope_id = @client_scope_id" in sql
    assert "AND c.market IN UNNEST(@markets)" in sql
    assert "AND e.qualified_evidence_count > 0" in sql
    assert sql.endswith(
        "ORDER BY\n"
        "  matched_term_count DESC,\n"
        "  qualified_source_family_count DESC,\n"
        "  breadth_score DESC,\n"
        "  latest_evidence_published_at DESC,\n"
        "  signal_date DESC,\n"
        "  released_at DESC,\n"
        "  run_id,\n"
        "  signal_id\n"
        "LIMIT @candidate_limit"
    )
    assert parameters_by_name(build(request, plan, intake, 17))["candidate_limit"].value == 17


COUNT_HINTS = (
    "candidate_count",
    "evidence_count",
    "membership_count",
    "lineage_count",
    "analysis_count",
    "prediction_count",
    "outcome_count_at_completion",
)


def selection_row(run_id, signal_id, *, client="scope", market="ng", counts=None):
    values = dict.fromkeys(COUNT_HINTS, 0)
    values.update(
        counts
        or {
            "candidate_count": 1,
            "evidence_count": 2,
            "membership_count": 1,
            "lineage_count": 0,
            "analysis_count": 1,
            "prediction_count": 1,
            "outcome_count_at_completion": 0,
        }
    )
    return {
        "client_scope_id": client,
        "market_scope": [market],
        "run_id": run_id,
        "signal_id": signal_id,
        "market": market,
        "full_matched_candidate_count": 1,
        "candidate_rows_truncated": False,
        **values,
    }


def select(rows, slots):
    return module().select_released_candidates_for_inspection(rows, remaining_candidate_slots=slots)


def test_multi_signal_run_is_selected_whole_and_counted_once_in_discovery_order():
    counts = dict.fromkeys(COUNT_HINTS, 1)
    counts["candidate_count"] = 2
    rows = [
        selection_row("run_a", "signal_2", counts=counts),
        selection_row("run_a", "signal_1", counts=counts),
    ]
    for row in rows:
        row["full_matched_candidate_count"] = 2

    result = select(rows, 8)

    assert result["selected_ids"] == (
        {"run_id": "run_a", "signal_id": "signal_2", "market": "ng"},
        {"run_id": "run_a", "signal_id": "signal_1", "market": "ng"},
    )
    assert result["selected_run_ids"] == ("run_a",)
    assert result["reserved_inspection_slots"] == 8
    assert result["omitted_runs"] == ()
    assert result["source_authority"] is False


def test_too_large_first_run_is_skipped_and_later_fitting_run_is_selected():
    large = dict.fromkeys(COUNT_HINTS, 2)
    small = dict.fromkeys(COUNT_HINTS, 0)
    small.update(candidate_count=1, evidence_count=1)
    rows = [
        selection_row("run_large", "signal_large", counts=large),
        selection_row("run_small", "signal_small", counts=small),
    ]
    for row in rows:
        row["full_matched_candidate_count"] = 2

    result = select(rows, 2)

    assert result["selected_run_ids"] == ("run_small",)
    assert result["reserved_inspection_slots"] == 2
    assert result["omitted_runs"] == (
        {
            "run_id": "run_large",
            "reason": "insufficient_inspection_slots",
            "required_inspection_slots": 14,
        },
    )


@pytest.mark.parametrize("value", [None, True, -1, "1"])
def test_unknown_or_invalid_count_hint_skips_the_whole_run(value):
    row = selection_row("run_bad", "signal_bad")
    row["membership_count"] = value
    result = select([row], 100)

    assert result["selected_ids"] == ()
    assert result["reserved_inspection_slots"] == 0
    assert result["omitted_runs"][0]["reason"] == (
        "count_hints_unknown" if value is None else "count_hints_invalid"
    )
    assert result["omitted_runs"][0]["required_inspection_slots"] is None


def test_inconsistent_run_hints_and_declared_candidate_underflow_are_skipped():
    first = selection_row("run_inconsistent", "signal_1")
    second = selection_row("run_inconsistent", "signal_2")
    first["full_matched_candidate_count"] = second["full_matched_candidate_count"] = 2
    second["evidence_count"] += 1
    inconsistent = select([first, second], 100)
    assert inconsistent["omitted_runs"][0]["reason"] == "count_hints_inconsistent"

    first["evidence_count"] = second["evidence_count"]
    first["candidate_count"] = second["candidate_count"] = 1
    underflow = select([first, second], 100)
    assert underflow["omitted_runs"][0]["reason"] == "candidate_count_exceeded"


def test_zero_slots_and_discovery_truncation_are_preserved_without_splitting_run():
    row = selection_row("run_a", "signal_a")
    row["full_matched_candidate_count"] = 3
    row["candidate_rows_truncated"] = True
    result = select([row], 0)

    assert result["selected_ids"] == ()
    assert result["reserved_inspection_slots"] == 0
    assert result["discovery_truncated"] is True
    assert result["omitted_runs"][0]["reason"] == "insufficient_inspection_slots"


@pytest.mark.parametrize("mutation", ["duplicate", "foreign_client", "foreign_market"])
def test_duplicate_or_foreign_discovery_identity_refuses(mutation):
    first = selection_row("run_a", "signal_a")
    second = selection_row("run_b", "signal_b")
    first["full_matched_candidate_count"] = second["full_matched_candidate_count"] = 2
    if mutation == "duplicate":
        second = copy.deepcopy(first)
    elif mutation == "foreign_client":
        second["client_scope_id"] = "other_scope"
    else:
        second["market_scope"] = ["za"]

    with pytest.raises(ValueError, match="inspection_selection_invalid"):
        select([first, second], 100)
