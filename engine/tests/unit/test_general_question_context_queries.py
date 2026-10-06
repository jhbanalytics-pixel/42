import copy
import importlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, date, datetime

import pytest
from src.analysis.open_intelligence.general_question_plan import validate_question_plan
from src.analysis.open_intelligence.general_question_policy import build_intake_context
from src.analysis.open_intelligence.general_question_request import normalize_question_request
from src.analysis.open_intelligence.production_snapshot_tables import LANES


def subject():
    return importlib.import_module(
        "src.analysis.open_intelligence.general_question_context_queries"
    )


def inputs(terms=None):
    request = normalize_question_request(
        {"message": "What explains unfamiliar night commute rituals?"},
        scope={
            "client_scope_id": "synthetic_scope",
            "market_scope": ["ng", "za"],
            "brand_config_id": None,
            "audience_lens_ids": [],
            "theme_id": None,
        },
        request_id="00000000-0000-4000-8000-000000000021",
        admitted_at=datetime(2026, 9, 9, 12, tzinfo=UTC),
        policy_digest="a" * 64,
    )
    intake = build_intake_context(request, selected_market="za")
    plan = validate_question_plan(
        {
            "status": "ready",
            "intent": "discovery",
            "markets": ["za"],
            "window": {"start": "2026-09-01", "end": "2026-09-08", "closed": True},
            "decision": None,
            "requirements": [
                {
                    "requirement_id": "context",
                    "question": "What is observed?",
                    "kind": "content",
                    "mandatory": True,
                    "search_terms": ["night commute"] if terms is None else terms,
                }
            ],
            "clarification": None,
            "limitations": [],
        },
        request=request,
        intake=intake,
    )
    source = {
        "profile_id": "protected_context_20260907_v1",
        "source_relation_set_id": "trends_v2_dev_table_snapshot_v1",
        "operation": "source_snapshot_capture",
        "cutoff_date": "2026-09-07",
        "source_as_of": "2026-09-08T00:00:00+00:00",
        "captured_at": "2026-09-08T00:05:00+00:00",
        "client_scope_id": "synthetic_scope",
        "market_scope": ["ng", "za"],
        "snapshot_digest": "b" * 64,
        "snapshot_tables": [
            {
                "lane": lane,
                "snapshot_table": "ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20260907_"
                + lane,
                "snapshot_time": "2026-09-08T00:00:00+00:00",
                "schema_digest": "c" * 64,
                "row_count": 10,
                "physical_result_digest": "d" * 64,
                "adapted_result_digest": "e" * 64,
            }
            for lane in LANES
        ],
    }
    return request, plan, intake, source


def metadata(source, count, full, *, evidence=False):
    row = {
        "lane": "__metadata__",
        "market": None,
        "payload": json.dumps(
            {
                "candidate_count": count,
                "full_matching_count": full,
                "overflow": full > count,
                "source_snapshot_digest": source["snapshot_digest"],
                "profile_id": source["profile_id"],
            }
        ),
    }
    return (
        {**row, "id": None, "match_count": None}
        if evidence
        else {**row, "candidate_key": None, "sample_row_ids": []}
    )


def candidate_rows(source):
    payload = {
        "market": "za",
        "term": "night commute",
        "sample_row_ids": ["one", "two"],
        "term_type": "keyword",
        "platform": "reddit",
    }
    return [
        metadata(source, 1, 1),
        {
            "lane": "seed_graph",
            "market": "za",
            "candidate_key": "key",
            "sample_row_ids": ["one", "two"],
            "payload": json.dumps(payload),
        },
    ]


def test_terms_bind_different_selection_with_fixed_sql_and_no_default_brand():
    first = subject().build_context_candidate_query(*inputs(), candidate_limit=10)
    second = subject().build_context_candidate_query(
        *inputs(["repair culture"]), candidate_limit=10
    )
    assert first.sql == second.sql
    assert first.parameters_digest != second.parameters_digest
    assert first.template_id == "protected_context_candidates_v1"
    assert first.transport_row_limit == 11
    assert "@search_terms" in first.sql
    assert "night commute" not in first.sql
    assert "r16" not in first.sql
    assert "brand_config_id" not in first.sql


def test_whole_capture_authority_keeps_query_and_decoding_limited_to_question_market():
    request, plan, intake, source = inputs()
    source["market_scope"] = ["ke", "ng", "za"]
    query = subject().build_context_candidate_query(
        request, plan, intake, source, candidate_limit=10
    )
    assert next(
        parameter.values for parameter in query.parameters if parameter.name == "markets"
    ) == ["za"]
    rows = candidate_rows(source)
    assert subject().decode_context_rows(rows, query=query)["candidate_count"] == 1
    rows[1]["market"] = "ng"
    payload = json.loads(rows[1]["payload"])
    payload["market"] = "ng"
    rows[1]["payload"] = json.dumps(payload)
    with pytest.raises(ValueError, match="context_query_invalid"):
        subject().decode_context_rows(rows, query=query)


def test_exact_keys_and_enriched_presence_control_raw_fallback():
    request, plan, intake, source = inputs()
    candidates = candidate_rows(source)
    enriched = build_evidence(
        request,
        plan,
        intake,
        source,
        candidate_rows=candidates,
        lane="enriched_content",
        candidate_limit=10,
    )
    assert enriched.expected_sample_keys == (("za", "one"), ("za", "two"))
    seen = [
        metadata(source, 1, 1, evidence=True),
        {
            "lane": "enriched_content",
            "market": "za",
            "id": "one",
            "payload": json.dumps({"id": "one", "market": "za"}),
            "match_count": 2,
        },
    ]
    raw = build_evidence(
        request,
        plan,
        intake,
        source,
        candidate_rows=candidates,
        enriched_rows=seen,
        lane="raw_content",
        candidate_limit=10,
    )
    assert raw.expected_sample_keys == (("za", "two"),)
    assert raw.template_id == "protected_context_raw_v1"
    assert "COUNT(*) OVER (PARTITION BY evidence.market, evidence.id)" in enriched.sql
    assert "CAST(NULL AS STRING) AS `native_id`" in enriched.sql


@pytest.mark.parametrize("mutation", ["client", "table", "future", "market", "profile"])
def test_wrong_source_scope_and_native_table_refuse(mutation):
    request, plan, intake, source = inputs()
    if mutation == "client":
        source["client_scope_id"] = "foreign"
    if mutation == "table":
        source["snapshot_tables"][0]["snapshot_table"] = "arbitrary.table"
    if mutation == "future":
        source["captured_at"] = "2026-09-10T00:00:00+00:00"
    if mutation == "market":
        source["market_scope"] = ["ng"]
    if mutation == "profile":
        source["profile_id"] = "unapproved"
    with pytest.raises(ValueError):
        subject().build_context_candidate_query(request, plan, intake, source, candidate_limit=10)


def test_metadata_counts_are_separate_and_truncated_enriched_uses_native_antijoin():
    args = inputs()
    source = args[3]
    query = subject().build_context_candidate_query(*args, candidate_limit=10)
    material = subject().decode_context_rows(candidate_rows(source), query=query)
    assert material["candidate_count"] == 1
    assert len(material["records"]) == 1
    truncated = [metadata(source, 0, 10, evidence=True)]
    raw = subject().build_context_index_query(
        *args,
        candidate_rows=candidate_rows(source),
        enriched_rows=truncated,
        lane="raw_content",
        candidate_limit=10,
    )
    assert "NOT EXISTS" in raw.sql
    assert "prior.market=evidence.market AND prior.id=evidence.id" in raw.sql


def test_decoder_rejects_count_or_pin_tampering():
    args = inputs()
    query = subject().build_context_candidate_query(*args, candidate_limit=10)
    rows = candidate_rows(args[3])
    rows[0] = metadata(args[3], 2, 2)
    with pytest.raises(ValueError):
        subject().decode_context_rows(rows, query=query)


def authority_rows(status="succeeded"):
    """Retained v1 capture rows read under historical_read: successor 1 excludes protected
    capture from fresh execution authority and preserves every historical approval,
    consumption and result byte, so the rows are built from the retained records."""
    from datetime import timedelta

    from src.analysis.open_intelligence import execution_approval as e
    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    from tests.unit.test_open_intelligence_execution_approval_runtime import (
        retained_source_snapshot_records,
    )

    # Successor 1: protected capture is excluded from fresh execution authority, so the
    # rows come from the retained v1 records the readers consume under historical_read.
    approval, consumption = retained_source_snapshot_records()
    completed = consumption.consumed_at + timedelta(seconds=1)
    reference = consumption.execution_name + "#source-snapshot"
    payload = canonical_bytes(
        {
            "contract_version": "open_intelligence_protected_source_snapshot_v1",
            "cutoff_date": "2026-09-07",
        }
    ).decode()
    import hashlib

    digest = hashlib.sha256(payload.encode()).hexdigest()
    result = e.ExecutionResult(
        result_contract_version="open_intelligence_execution_result_v1",
        result_id=e.result_id(consumption.consumption_id, reference, digest, status, completed),
        consumption_id=consumption.consumption_id,
        approval_id=consumption.approval_id,
        manifest_sha256=consumption.manifest_sha256,
        operation="source_snapshot_capture",
        execution_name=consumption.execution_name,
        result_reference=reference,
        canonical_result_json=payload,
        result_digest=digest,
        status=status,
        completed_at=completed,
    )
    return [
        {
            "approval_count": 1,
            "consumption_count": 1,
            "result_count": 1,
            "approval_json": canonical_bytes(asdict(approval)).decode(),
            "consumption_json": canonical_bytes(asdict(consumption)).decode(),
            "result_json": canonical_bytes(asdict(result)).decode(),
        }
    ], consumption.consumption_id


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_source_authority_decoder_preserves_native_json_and_historical_times(status):
    rows, consumption_id = authority_rows(status)
    decoded = subject().decode_context_result_rows(rows, consumption_id=consumption_id)
    assert decoded["result"]["status"] == status
    assert isinstance(decoded["result"]["completed_at"], datetime)
    assert (
        decoded["result"]["canonical_result_json"]
        == json.loads(rows[0]["result_json"])["canonical_result_json"]
    )
    assert decoded["consumption"].consumption_id == consumption_id


@pytest.mark.parametrize(
    "mutation",
    ["approval_count", "consumption_count", "result_count", "foreign_id", "source_sha", "digest"],
)
def test_source_authority_decoder_refuses_collision_and_native_binding_mutations(mutation):
    rows, consumption_id = authority_rows()
    if mutation.endswith("count"):
        rows[0][mutation] = 2
    elif mutation == "foreign_id":
        consumption_id = "exc_" + "0" * 64
    elif mutation == "source_sha":
        value = json.loads(rows[0]["consumption_json"])
        value["source_sha"] = "f" * 40
        rows[0]["consumption_json"] = json.dumps(value)
    else:
        value = json.loads(rows[0]["result_json"])
        value["canonical_result_json"] = '{"changed":true}'
        rows[0]["result_json"] = json.dumps(value)
    with pytest.raises(ValueError):
        subject().decode_context_result_rows(rows, consumption_id=consumption_id)


def test_empty_key_queries_are_native_zero_metadata_without_source_scan():
    args = inputs()
    source = args[3]
    candidates = [metadata(source, 0, 0)]
    enriched = build_evidence(
        *args, candidate_rows=candidates, lane="enriched_content", candidate_limit=10
    )
    raw = build_evidence(
        *args,
        candidate_rows=candidates,
        lane="raw_content",
        candidate_limit=10,
        enriched_rows=[metadata(source, 0, 0, evidence=True)],
    )
    for query in (enriched, raw):
        assert query.expected_sample_keys == ()
        assert "FROM `ogilvy-trends-v2" not in query.sql
        assert (
            subject().decode_context_rows([metadata(source, 0, 0, evidence=True)], query=query)[
                "candidate_count"
            ]
            == 0
        )


def test_source_binding_self_digest_has_one_verified_domain():
    from src.analysis.open_intelligence.brain_contract import canonical_digest

    args = inputs()
    source = args[3]
    expected = canonical_digest(source)
    source["source_binding_digest"] = expected
    query = subject().build_context_candidate_query(*args, candidate_limit=10)
    assert query.source_binding_digest == expected
    assert next(p.value for p in query.parameters if p.name == "source_binding_digest") == expected
    source["source_binding_digest"] = "0" * 64
    with pytest.raises(ValueError):
        subject().build_context_candidate_query(*args, candidate_limit=10)
    rows = candidate_rows(args[3])
    value = json.loads(rows[0]["payload"])
    value["source_snapshot_digest"] = "f" * 64
    rows[0]["payload"] = json.dumps(value)
    with pytest.raises(ValueError):
        subject().decode_context_rows(rows, query=query)


@pytest.mark.parametrize("lane", ["enriched_content", "raw_content"])
def test_evidence_decoder_rejects_other_evidence_lane(lane):
    args = inputs()
    source = args[3]
    query = build_evidence(
        *args,
        candidate_rows=candidate_rows(source),
        enriched_rows=[metadata(source, 0, 0, evidence=True)],
        lane=lane,
        candidate_limit=10,
    )
    rows = [
        metadata(source, 1, 1, evidence=True),
        {
            "lane": "raw_content" if lane == "enriched_content" else "enriched_content",
            "market": "za",
            "id": "one",
            "payload": json.dumps(
                {"market": "za", "id": "one", "collected_at": "2026-09-01T01:00:00+00:00"}
            ),
            "match_count": 1,
        },
    ]
    with pytest.raises(ValueError, match="context_query_invalid"):
        subject().decode_context_rows(rows, query=query)
    rows[1]["lane"] = lane
    assert subject().decode_context_rows(rows, query=query)["candidate_count"] == 1


def test_evidence_preserves_full_plan_publication_window_and_old_collection():
    args = inputs()
    query = build_evidence(
        *args,
        candidate_rows=candidate_rows(args[3]),
        lane="enriched_content",
        candidate_limit=10,
    )
    params = {parameter.name: getattr(parameter, "value", None) for parameter in query.parameters}
    assert params["publication_start"] == datetime(2026, 9, 1, tzinfo=UTC)
    assert params["publication_end"] == datetime(2026, 9, 9, tzinfo=UTC)
    assert "evidence.published_at>=@publication_start" in query.sql
    assert "evidence.published_at<@publication_end" in query.sql
    assert "evidence.collected_at>=" not in query.sql


def test_partition_index_and_payload_bind_old_collection_dates():
    args = inputs()
    candidates = candidate_rows(args[3])
    index = subject().build_context_index_query(
        *args, candidate_rows=candidates, lane="enriched_content", candidate_limit=10
    )
    rows = [
        metadata(args[3], 1, 1, evidence=True),
        {
            "lane": "enriched_content",
            "market": "za",
            "id": "one",
            "payload": json.dumps({"market": "za", "id": "one", "collected_date": "2026-08-01"}),
            "match_count": 1,
        },
    ]
    assert subject().decode_context_rows(rows, query=index)["candidate_count"] == 1
    payload = build_evidence(
        *args,
        candidate_rows=candidates,
        lane="enriched_content",
        candidate_limit=10,
        index_rows=rows,
    )
    assert "DATE(evidence.collected_at) IN UNNEST(@selected_dates)" in payload.sql
    assert next(p.values for p in payload.parameters if p.name == "selected_dates") == [
        date(2026, 8, 1)
    ]
    assert "content_text" not in index.sql


def index_rows(source, lane="enriched_content"):
    return [metadata(source, 2, 2, evidence=True)] + [
        {
            "lane": lane,
            "market": "za",
            "id": key,
            "payload": json.dumps({"market": "za", "id": key, "collected_date": "2026-09-01"}),
            "match_count": 1,
        }
        for key in ("one", "two")
    ]


def test_payload_exact_index_tuples_exclude_unindexed_same_date_and_crossed_dates():
    args = inputs()
    rows = index_rows(args[3])
    rows[2]["payload"] = rows[2]["payload"].replace("2026-09-01", "2026-08-01")
    query = build_evidence(
        *args,
        candidate_rows=candidate_rows(args[3]),
        lane="enriched_content",
        candidate_limit=10,
        index_rows=rows,
    )
    params = {p.name: getattr(p, "values", None) for p in query.parameters}
    assert params["sample_dates"] == [date(2026, 9, 1), date(2026, 8, 1)]
    assert "DATE(evidence.collected_at)=requested.collected_date" in query.sql
    wire = [
        metadata(args[3], 1, 1, evidence=True),
        {
            "lane": "enriched_content",
            "market": "za",
            "id": "one",
            "match_count": 1,
            "payload": json.dumps(
                {"market": "za", "id": "one", "collected_at": "2026-08-01T01:00:00+00:00"}
            ),
        },
    ]
    with pytest.raises(ValueError, match="context_query_invalid"):
        subject().decode_context_rows(wire, query=query)
    single = [metadata(args[3], 1, 1, evidence=True), rows[1]]
    restricted = build_evidence(
        *args,
        candidate_rows=candidate_rows(args[3]),
        lane="enriched_content",
        candidate_limit=10,
        index_rows=single,
    )
    assert restricted.expected_sample_keys == (("za", "one"),)
    assert next(p.values for p in restricted.parameters if p.name == "sample_ids") == ["one"]
    wire[1]["id"] = "two"
    wire[1]["payload"] = json.dumps(
        {"market": "za", "id": "two", "collected_at": "2026-09-01T01:00:00+00:00"}
    )
    with pytest.raises(ValueError, match="context_query_invalid"):
        subject().decode_context_rows(wire, query=restricted)
    wire[1]["id"] = "one"
    wire[1]["payload"] = json.dumps(
        {"market": "za", "id": "one", "collected_at": "2026-09-01T01:00:00+00:00"}
    )
    assert subject().decode_context_rows(wire, query=restricted)["candidate_count"] == 1


def test_raw_payload_reuses_owned_index_antijoin():
    args = inputs()
    kwargs = {
        "candidate_rows": candidate_rows(args[3]),
        "lane": "raw_content",
        "candidate_limit": 10,
        "enriched_rows": [metadata(args[3], 0, 0, evidence=True)],
    }
    index = subject().build_context_index_query(*args, **kwargs)
    query = build_evidence(*args, **kwargs)
    assert "NOT EXISTS" in index.sql
    assert "NOT EXISTS" not in query.sql
    prior_date_guard = (
        "DATE(prior.collected_at) BETWEEN DATE_SUB(@capture_cutoff, INTERVAL 6 DAY) "
        "AND @capture_cutoff"
    )
    assert prior_date_guard in index.sql
    legacy_index = subject()._build_context_evidence_query(
        *args, _index=True, _validation_legacy=True, **kwargs
    )
    assert prior_date_guard not in legacy_index.sql


def build_evidence(*args, **kwargs):
    if "index_rows" not in kwargs:
        source = args[3]
        keys = {
            (row["market"], key)
            for row in kwargs["candidate_rows"][1:]
            for key in row["sample_row_ids"]
        }
        keys -= {(row["market"], row["id"]) for row in (kwargs.get("enriched_rows") or [])[1:]}
        rows = [
            row
            for row in index_rows(source, kwargs["lane"])[1:]
            if (row["market"], row["id"]) in keys
        ]
        kwargs["index_rows"] = [metadata(source, len(rows), len(rows), evidence=True), *rows]
    return subject().build_context_evidence_query(*args, **kwargs)


def test_authority_batch_is_bounded_and_binds_each_id():
    request, plan, intake, _ = inputs()
    identifiers = tuple("exc_" + digit * 64 for digit in ("1", "2", "3"))
    query = subject().build_context_result_batch_query(
        request, plan, intake, consumption_ids=identifiers
    )
    assert query.transport_row_limit == 3
    assert query.candidate_limit == 0
    assert tuple(parameter.value for parameter in query.parameters) == identifiers
    assert query.template_id == "protected_context_results_v1"
    assert query.sql.count("AS requested_consumption_id") == 3


@pytest.mark.parametrize(
    "identifiers",
    [
        (),
        ("exc_" + "1" * 64,) * 2,
        tuple("exc_" + value * 64 for value in ("1", "2", "3", "4")),
        ("invalid",),
    ],
)
def test_authority_batch_rejects_unbounded_or_duplicate_ids(identifiers):
    request, plan, intake, _ = inputs()
    with pytest.raises(ValueError):
        subject().build_context_result_batch_query(
            request, plan, intake, consumption_ids=identifiers
        )


@pytest.mark.parametrize("mutation", [None, "missing", "duplicate", "unrelated", "collision"])
def test_authority_batch_decodes_exact_requested_set(mutation):
    rows, identifier = authority_rows()
    batch = [{"requested_consumption_id": identifier, **rows[0]}]
    if mutation == "missing":
        batch = []
    elif mutation == "duplicate":
        batch *= 2
    elif mutation == "unrelated":
        batch[0]["requested_consumption_id"] = "exc_" + "0" * 64
    elif mutation == "collision":
        batch[0]["consumption_count"] = 2
    if mutation:
        with pytest.raises(ValueError):
            subject().decode_context_result_batch_rows(batch, consumption_ids=(identifier,))
    else:
        decoded = subject().decode_context_result_batch_rows(batch, consumption_ids=(identifier,))
        assert decoded[identifier]["consumption"].consumption_id == identifier


_RECONCILED_FAILURE_WIRE_JSON = (
    '{"approval_count":1,"approval_json":"{\\"approval_contract_version\\":\\"open_intelligence_'
    'execution_approval_v1\\",\\"approval_id\\":\\"exa_9797927ec6012c50bbb811f49c8b92541e3e82f16a'
    '9842c4c4f73121c9c91924\\",\\"manifest_version\\":\\"open_intelligence_execution_manifest_v1\\'
    '",\\"operation\\":\\"source_snapshot_capture\\",\\"contract_sha256\\":\\"5dcd9346af27fd7dac97ef'
    'dd138fce19b56813e0d1eb6629aebcfa9b0233595f\\",\\"manifest_sha256\\":\\"109ea182ba6f1b7042c76'
    'ecbea7d62cab753b3867cca57c909b422878b0a5433\\",\\"canonical_manifest_json\\":\\"{\\\\\\"argumen'
    'ts\\\\\\":[\\\\\\"scripts/staging/capture_protected_production_snapshot.py\\\\\\",\\\\\\"--cutoff-da'
    'te\\\\\\",\\\\\\"2026-09-07\\\\\\",\\\\\\"--mode\\\\\\",\\\\\\"recover\\\\\\"],\\\\\\"build_resource\\\\\\":\\\\\\"pro'
    "jects/ogilvy-trends-v2/locations/us-central1/builds/0aa8138f-19a1-4084-96db-34acb49a8f3c"
    '\\\\\\",\\\\\\"command\\\\\\":[\\\\\\"python\\\\\\"],\\\\\\"contract_sha256\\\\\\":\\\\\\"5dcd9346af27fd7dac97ef'
    'dd138fce19b56813e0d1eb6629aebcfa9b0233595f\\\\\\",\\\\\\"datasets\\\\\\":[\\\\\\"trends_v2_dev\\\\\\",\\'
    '\\\\"trends_v2_staging\\\\\\"],\\\\\\"environment\\\\\\":[{\\\\\\"name\\\\\\":\\\\\\"BIGQUERY_DATASET\\\\\\",\\\\'
    '\\"value\\\\\\":\\\\\\"trends_v2_staging\\\\\\"},{\\\\\\"name\\\\\\":\\\\\\"GCP_PROJECT\\\\\\",\\\\\\"value\\\\\\":\\'
    '\\\\"ogilvy-trends-v2\\\\\\"},{\\\\\\"name\\\\\\":\\\\\\"TRENDS_ENV\\\\\\",\\\\\\"value\\\\\\":\\\\\\"staging\\\\\\"}'
    '],\\\\\\"expires_at\\\\\\":\\\\\\"2026-09-08T15:23:05.000000Z\\\\\\",\\\\\\"image_uri\\\\\\":\\\\\\"us-centra'
    "l1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:944c8957edeb10220e4dc1d"
    '568396939020ac94aebc074dffdfd8e77cf7a7443\\\\\\",\\\\\\"input_artifacts\\\\\\":[{\\\\\\"name\\\\\\":\\\\\\'
    '"build_provenance\\\\\\",\\\\\\"sha256\\\\\\":\\\\\\"fc3cf8b5b2483065235b30f75d8eb0a968baee3828c063c'
    '96d6915266dbef42b\\\\\\"},{\\\\\\"name\\\\\\":\\\\\\"capture_contract\\\\\\",\\\\\\"sha256\\\\\\":\\\\\\"5dcd934'
    '6af27fd7dac97efdd138fce19b56813e0d1eb6629aebcfa9b0233595f\\\\\\"},{\\\\\\"name\\\\\\":\\\\\\"capture'
    '_plan\\\\\\",\\\\\\"sha256\\\\\\":\\\\\\"797783ca30e81333f012f9bc0f976c339e779975ef13ff93b0e0f170f54'
    '9a868\\\\\\"},{\\\\\\"name\\\\\\":\\\\\\"recovery_context\\\\\\",\\\\\\"sha256\\\\\\":\\\\\\"ade29835ba6ca2192ad'
    '2ae7ae3d87d97567ce7ea4796390ce7f5c934fd1a1d95\\\\\\"},{\\\\\\"name\\\\\\":\\\\\\"source_metadata\\\\\\"'
    ',\\\\\\"sha256\\\\\\":\\\\\\"3ebfae005828ada1646ca082f18de9a4a8a17400a125a8f517c9334d00916258\\\\\\"'
    '},{\\\\\\"name\\\\\\":\\\\\\"storage_policy\\\\\\",\\\\\\"sha256\\\\\\":\\\\\\"87e65ce8b153cc607a8b71de8131a8'
    '038d05389863b6f8e9953f896e1c1e0b6f\\\\\\"}],\\\\\\"job_resource\\\\\\":\\\\\\"projects/ogilvy-trends'
    '-v2/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging\\\\\\",\\\\\\"limits\\\\'
    '\\":{\\\\\\"max_bytes_billed\\\\\\":1000000000,\\\\\\"max_credits\\\\\\":0,\\\\\\"max_model_calls\\\\\\":0,'
    '\\\\\\"max_rows_written\\\\\\":0},\\\\\\"location\\\\\\":\\\\\\"US\\\\\\",\\\\\\"manifest_version\\\\\\":\\\\\\"ope'
    'n_intelligence_execution_manifest_v1\\\\\\",\\\\\\"max_retries\\\\\\":0,\\\\\\"operation\\\\\\":\\\\\\"sou'
    'rce_snapshot_capture\\\\\\",\\\\\\"project\\\\\\":\\\\\\"ogilvy-trends-v2\\\\\\",\\\\\\"secrets\\\\\\":[],\\\\\\'
    '"service_identity\\\\\\":\\\\\\"trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com'
    '\\\\\\",\\\\\\"source_sha\\\\\\":\\\\\\"5bbe23042b89f5312d46f1cb0a2f6eccf3a73281\\\\\\",\\\\\\"timeout_sec'
    'onds\\\\\\":600}\\",\\"approved_by\\":\\"usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b'
    '6617fe13f7d2ab\\",\\"approved_at\\":\\"2026-09-08T13:25:54.931Z\\",\\"expires_at\\":\\"2026-09-0'
    '8T15:23:05Z\\",\\"approval_phrase_sha256\\":\\"121b65ddf4f1759a01728802e7a264715ea417b256fba'
    '9aed574e57a9a2537ee\\"}","consumption_count":1,"consumption_json":"{\\"consumption_contrac'
    't_version\\":\\"open_intelligence_execution_consumption_v1\\",\\"consumption_id\\":\\"exc_3178'
    'cc3f6011e245b21ade8f8677593f8cd23afedd7f23218ce13c209a2cbb47\\",\\"approval_id\\":\\"exa_979'
    '7927ec6012c50bbb811f49c8b92541e3e82f16a9842c4c4f73121c9c91924\\",\\"manifest_sha256\\":\\"10'
    '9ea182ba6f1b7042c76ecbea7d62cab753b3867cca57c909b422878b0a5433\\",\\"operation\\":\\"source_'
    'snapshot_capture\\",\\"execution_name\\":\\"projects/ogilvy-trends-v2/locations/us-central1/'
    "jobs/trends-engine-oi-source-snapshot-staging/executions/trends-engine-oi-source-snapsho"
    't-staging-v7k8f\\",\\"job_resource\\":\\"projects/ogilvy-trends-v2/locations/us-central1/job'
    's/trends-engine-oi-source-snapshot-staging\\",\\"source_sha\\":\\"5bbe23042b89f5312d46f1cb0a'
    '2f6eccf3a73281\\",\\"image_uri\\":\\"us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/tr'
    'ends-engine@sha256:944c8957edeb10220e4dc1d568396939020ac94aebc074dffdfd8e77cf7a7443\\",\\"'
    'consumed_at\\":\\"2026-09-08T13:30:02.162Z\\"}","result_count":1,"result_json":"{\\"result_c'
    'ontract_version\\":\\"open_intelligence_execution_result_v1\\",\\"result_id\\":\\"exr_407b5ccb'
    'c5e2216f8e0582ea478af1cf9e726424c0aca5bb3ef0403ebc9f29bd\\",\\"consumption_id\\":\\"exc_3178'
    'cc3f6011e245b21ade8f8677593f8cd23afedd7f23218ce13c209a2cbb47\\",\\"approval_id\\":\\"exa_979'
    '7927ec6012c50bbb811f49c8b92541e3e82f16a9842c4c4f73121c9c91924\\",\\"manifest_sha256\\":\\"10'
    '9ea182ba6f1b7042c76ecbea7d62cab753b3867cca57c909b422878b0a5433\\",\\"operation\\":\\"source_'
    'snapshot_capture\\",\\"execution_name\\":\\"projects/ogilvy-trends-v2/locations/us-central1/'
    "jobs/trends-engine-oi-source-snapshot-staging/executions/trends-engine-oi-source-snapsho"
    't-staging-v7k8f\\",\\"result_reference\\":\\"projects/ogilvy-trends-v2/locations/us-central1'
    "/jobs/trends-engine-oi-source-snapshot-staging/executions/trends-engine-oi-source-snapsh"
    'ot-staging-v7k8f#source-snapshot\\",\\"canonical_result_json\\":\\"{\\\\\\"artifact_attempt\\\\\\"'
    ':null,\\\\\\"capture_receipt_digest\\\\\\":null,\\\\\\"captured_at\\\\\\":null,\\\\\\"client_scope_id\\\\'
    '\\":\\\\\\"ogilvy_default\\\\\\",\\\\\\"contract_version\\\\\\":\\\\\\"open_intelligence_protected_sourc'
    'e_snapshot_v1\\\\\\",\\\\\\"creation_records\\\\\\":[{\\\\\\"destination\\\\\\":\\\\\\"ogilvy-trends-v2.tr'
    'ends_v2_staging.open_intelligence_v3_source_20260907_event_ledger\\\\\\",\\\\\\"job_id\\\\\\":\\\\\\'
    '"oi_v3_snapshot_109ea182ba6f1b7042c76ecbea7d62cab753b3867cca57c909b422878b0a5433_event_l'
    'edger\\\\\\",\\\\\\"lane\\\\\\":\\\\\\"event_ledger\\\\\\",\\\\\\"native_job_digest\\\\\\":null,\\\\\\"state\\\\\\"'
    ':\\\\\\"unresolved\\\\\\"}],\\\\\\"cutoff_date\\\\\\":\\\\\\"2026-09-07\\\\\\",\\\\\\"limitations\\\\\\":[\\\\\\"up'
    'stream_collection_completeness_unproven\\\\\\"],\\\\\\"market_scope\\\\\\":[\\\\\\"ke\\\\\\",\\\\\\"ng\\\\\\"'
    ',\\\\\\"za\\\\\\"],\\\\\\"missing_checks\\\\\\":[\\\\\\"snapshot_creation_incomplete\\\\\\"],\\\\\\"query_cou'
    'nt\\\\\\":0,\\\\\\"snapshot_digest\\\\\\":null,\\\\\\"snapshot_plan_digest\\\\\\":\\\\\\"866bb6439e41eb21b'
    'aef9d87e50f7168e236613208c428cead643836e3844e29\\\\\\",\\\\\\"source_as_of\\\\\\":\\\\\\"2026-09-08T'
    '00:00:00+00:00\\\\\\",\\\\\\"stored_artifact\\\\\\":null,\\\\\\"total_bytes_billed\\\\\\":null}\\",\\"res'
    'ult_digest\\":\\"466d2bafc12afd90fcc863ef300bb3f3b8875957a2e6d5d29c47ad3a8def2f23\\",\\"stat'
    'us\\":\\"failed\\",\\"completed_at\\":\\"2026-09-08T14:04:47.839Z\\"}"}'
)


def reconciled_failure_rows():
    return [
        json.loads(_RECONCILED_FAILURE_WIRE_JSON)
    ], "exc_3178cc3f6011e245b21ade8f8677593f8cd23afedd7f23218ce13c209a2cbb47"


def test_exact_late_recorded_failure_remains_valid_metadata():
    rows, identifier = reconciled_failure_rows()
    decoded = subject().decode_context_result_rows(rows, consumption_id=identifier)
    assert (
        decoded["result"]["result_id"]
        == "exr_407b5ccbc5e2216f8e0582ea478af1cf9e726424c0aca5bb3ef0403ebc9f29bd"
    )
    assert decoded["result"]["completed_at"] == datetime(2026, 9, 8, 14, 4, 47, 839000, tzinfo=UTC)


@pytest.mark.parametrize(
    "mutation",
    [
        "consumed_time",
        "recorded_time",
        "result_id",
        "succeeded",
        "query_count",
        "artifact",
        "manifest",
    ],
)
def test_late_failure_exception_rejects_any_changed_pin(mutation):
    rows, identifier = reconciled_failure_rows()
    consumption = json.loads(rows[0]["consumption_json"])
    result = json.loads(rows[0]["result_json"])
    if mutation == "consumed_time":
        consumption["consumed_at"] = "2026-09-08T13:30:03.162000Z"
    elif mutation == "recorded_time":
        result["completed_at"] = "2026-09-08T14:04:48.839000Z"
    elif mutation == "result_id":
        result["result_id"] = "exr_" + "0" * 64
    elif mutation == "succeeded":
        result["status"] = "succeeded"
    elif mutation == "manifest":
        result["manifest_sha256"] = "0" * 64
    else:
        payload = json.loads(result["canonical_result_json"])
        payload["query_count" if mutation == "query_count" else "artifact_attempt"] = 1
        result["canonical_result_json"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    rows[0]["consumption_json"] = json.dumps(consumption)
    rows[0]["result_json"] = json.dumps(result)
    with pytest.raises(ValueError):
        subject().decode_context_result_rows(rows, consumption_id=identifier)


@pytest.mark.parametrize("lane", ["enriched_content", "raw_content"])
@pytest.mark.parametrize("index", [True, False])
def test_empty_evidence_routes_have_valid_from_before_where(lane, index):
    args = inputs()
    kwargs = {
        "candidate_rows": [metadata(args[3], 0, 0)],
        "lane": lane,
        "candidate_limit": 10,
        "enriched_rows": [metadata(args[3], 0, 0, evidence=True)],
    }
    query = (
        subject().build_context_index_query(*args, **kwargs)
        if index
        else build_evidence(*args, **kwargs)
    )
    assert "FROM UNNEST(ARRAY<INT64>[]) AS empty_row WHERE FALSE" in query.sql
    assert "FROM `ogilvy-trends-v2" not in query.sql
    assert query.expected_sample_keys == ()


def test_direct_partition_fallback_is_bound_and_does_not_join_empty_ids():
    args = inputs()
    query = subject().build_context_partition_query(*args)
    rows = [
        {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
        {"lane": "enriched_content", "partition_date": "2026-09-07", "partition_count": 1},
    ]
    assert subject().decode_context_partition_rows(rows, query=query)["enriched_content"] == [
        "2026-09-07"
    ]
    index = subject().build_context_index_query(
        *args,
        candidate_rows=[metadata(args[3], 0, 0)],
        lane="enriched_content",
        candidate_limit=200,
        partition_rows=rows,
    )
    assert "DATE(evidence.collected_at) IN UNNEST(@eligible_partition_dates)" in index.sql
    assert "JOIN requested USING" not in index.sql
    assert "COALESCE(evidence.text" in index.sql
    raw_partitions = [
        {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
        {"lane": "raw_content", "partition_date": "2026-09-07", "partition_count": 1},
    ]
    raw = subject().build_context_index_query(
        *args,
        candidate_rows=[metadata(args[3], 0, 0)],
        enriched_rows=[metadata(args[3], 0, 0, evidence=True)],
        lane="raw_content",
        candidate_limit=200,
        partition_rows=raw_partitions,
    )
    assert "NOT EXISTS" in raw.sql
    assert "DATE(evidence.collected_at) BETWEEN DATE_SUB(@capture_cutoff" not in raw.sql
    assert "DATE(prior.collected_at) BETWEEN DATE_SUB(@capture_cutoff" not in raw.sql
    assert "direct_content_fallback" in {item.name for item in raw.parameters}


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "unknown", "null", "future", "count_bool", "overflow"]
)
def test_partition_decoder_refuses_incomplete_or_altered_rows(mutation):
    args = inputs()
    q = subject().build_context_partition_query(*args)
    rows = [
        {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
        {"lane": "enriched_content", "partition_date": "2026-09-07", "partition_count": 1},
    ]
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[1]))
    elif mutation == "unknown":
        rows[1]["lane"] = "foreign"
    elif mutation == "null":
        rows[1]["partition_date"] = None
    elif mutation == "future":
        rows[1]["partition_date"] = "2027-01-01"
    elif mutation == "count_bool":
        rows[0]["partition_count"] = True
    else:
        rows[0]["partition_count"] = 1001
    with pytest.raises(ValueError):
        subject().decode_context_partition_rows(rows, query=q)


@pytest.mark.parametrize(
    "mutation", [None, "irrelevant", "foreign_market", "outside_window", "outside_index"]
)
def test_direct_evidence_requires_full_text_match_and_owned_partition(mutation):
    args = inputs(terms=["needle"])
    source = args[3]
    partitions = [
        {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
        {"lane": "enriched_content", "partition_date": "2026-09-07", "partition_count": 1},
    ]
    indexes = [
        metadata(source, 1, 1, evidence=True),
        {
            "lane": "enriched_content",
            "market": "za",
            "id": "outside_graph",
            "match_count": 1,
            "payload": json.dumps(
                {"market": "za", "id": "outside_graph", "collected_date": "2026-09-07"}
            ),
        },
    ]
    q = subject().build_context_evidence_query(
        *args,
        candidate_rows=[metadata(source, 0, 0)],
        lane="enriched_content",
        candidate_limit=200,
        partition_rows=partitions,
        index_rows=indexes,
    )
    payload = {
        "market": "za",
        "id": "outside_graph",
        "collected_at": "2026-09-07T01:00:00+00:00",
        "published_at": None,
        "text": "x" * 4500 + " needle",
        "title": None,
    }
    rows = [
        metadata(source, 1, 1, evidence=True),
        {
            "lane": "enriched_content",
            "market": "za",
            "id": "outside_graph",
            "match_count": 1,
            "payload": json.dumps(payload),
        },
    ]
    if mutation == "irrelevant":
        payload["text"] = "unrelated"
    elif mutation == "foreign_market":
        payload["market"] = rows[1]["market"] = "ke"
    elif mutation == "outside_window":
        payload["published_at"] = "2026-08-01T01:00:00+00:00"
    elif mutation == "outside_index":
        payload["id"] = rows[1]["id"] = "other"
    rows[1]["payload"] = json.dumps(payload)
    if mutation:
        with pytest.raises(ValueError):
            subject().decode_context_rows(rows, query=q)
    else:
        assert subject().decode_context_rows(rows, query=q)["candidate_count"] == 1


def test_empty_terms_keep_zero_evidence_and_disallow_direct_search():
    args = inputs(terms=[])
    query = subject().build_context_index_query(
        *args,
        candidate_rows=[metadata(args[3], 0, 0)],
        lane="enriched_content",
        candidate_limit=200,
    )
    assert "FROM `ogilvy-trends-v2" not in query.sql
    with pytest.raises(ValueError):
        subject().build_context_index_query(
            *args,
            candidate_rows=[metadata(args[3], 0, 0)],
            lane="enriched_content",
            candidate_limit=200,
            partition_rows=[{"lane": "__metadata__", "partition_date": None, "partition_count": 0}],
        )


def test_seed_samples_do_not_exclude_direct_text_matches_from_same_index():
    args = inputs()
    query = subject().build_context_index_query(
        *args,
        candidate_rows=candidate_rows(args[3]),
        lane="enriched_content",
        candidate_limit=200,
        partition_rows=[
            {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
            {"lane": "enriched_content", "partition_date": "2026-09-07", "partition_count": 1},
        ],
    )
    assert query.template_id == "protected_context_enriched_index_v2"
    assert "JOIN requested USING(market,id)" in query.sql
    assert "UNION ALL" in query.sql
    assert "STRPOS(LOWER(CONCAT" in query.sql
    assert "SELECT DISTINCT *" in query.sql
    rows = [metadata(args[3], 2, 2, evidence=True)]
    for identifier in ("one", "outside_graph"):
        rows.append(
            {
                "lane": "enriched_content",
                "market": "za",
                "id": identifier,
                "match_count": 1,
                "payload": json.dumps(
                    {"market": "za", "id": identifier, "collected_date": "2026-09-07"}
                ),
            }
        )
    assert subject().decode_context_rows(rows, query=query)["candidate_count"] == 2


@pytest.mark.parametrize("selection_limit", [24, 200])
def test_supplement_cap_preserves_seed_rows_and_balances_556_record_pool(selection_limit):
    args = inputs()
    query = subject().build_context_index_query(
        *args,
        candidate_rows=candidate_rows(args[3]),
        lane="enriched_content",
        candidate_limit=selection_limit,
        partition_rows=[
            {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
            {"lane": "enriched_content", "partition_date": "2026-09-07", "partition_count": 1},
        ],
    )
    selection = query.sql.split("), balanced AS (", 1)[1].split("), output AS (", 1)[0]
    with closing(sqlite3.connect(":memory:")) as connection, connection:
        connection.execute(
            "CREATE TABLE eligible (market TEXT,id TEXT,payload TEXT,source_platform TEXT,term_relevance INTEGER,seed_priority INTEGER,selection_priority INTEGER,pair_rank INTEGER)"
        )
        seed_ids = {f"seed_{i}" for i in range(11)}
        rows = [("za", name, name, "reddit", 1, 0, 2, 1) for name in seed_ids]
        rows += [
            (
                "za",
                f"direct_{i:04}",
                str(i),
                ("reddit", "youtube", "tiktok", "news", "threads")[i % 5],
                1 + i % 3,
                1,
                2,
                1,
            )
            for i in range(545)
        ]
        connection.executemany("INSERT INTO eligible VALUES (?,?,?,?,?,?,?,?)", rows)
        selected = connection.execute(
            "WITH balanced AS ("
            + selection
            + ") SELECT id,source_platform,seed_priority,term_relevance FROM picked",
            {"candidate_limit": selection_limit},
        ).fetchall()
        assert len(selected) == selection_limit
        assert seed_ids <= {row[0] for row in selected}
        direct = [row for row in selected if row[2] == 1]
        assert {row[1] for row in direct} == {"reddit", "youtube", "tiktok", "news", "threads"}
        counts = [
            sum(row[1] == platform for row in direct) for platform in {row[1] for row in direct}
        ]
        assert max(counts) - min(counts) <= 1
        assert len({row[0] for row in selected}) == selection_limit


@pytest.mark.parametrize("enriched_count", [24, 200])
def test_exhausted_raw_tail_still_measures_matching_universe_with_zero_selection(enriched_count):
    args = inputs()
    identifiers = ["one", "two", *[f"enriched_{i}" for i in range(enriched_count - 2)]]
    enriched = [metadata(args[3], enriched_count, enriched_count, evidence=True)] + [
        {
            "lane": "enriched_content",
            "market": "za",
            "id": identifier,
            "match_count": 1,
            "payload": json.dumps({"market": "za", "id": identifier}),
        }
        for identifier in identifiers
    ]
    common = {
        "candidate_rows": candidate_rows(args[3]),
        "lane": "raw_content",
        "candidate_limit": 0,
        "enriched_rows": enriched,
        "partition_rows": [
            {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
            {"lane": "raw_content", "partition_date": "2026-09-07", "partition_count": 1},
        ],
    }
    query = subject().build_context_index_query(*args, **common)
    assert "FROM `ogilvy-trends-v2" in query.sql
    assert "(SELECT COUNT(*) FROM eligible) AS full_matching_count" in query.sql
    assert query.candidate_limit == 0
    empty_branch = query.sql.split("SELECT DISTINCT * FROM (", 1)[1].split(" UNION ALL ", 1)[0]
    empty_branch = empty_branch.replace("FROM UNNEST(ARRAY<INT64>[]) AS empty_row ", "")
    with closing(sqlite3.connect(":memory:")) as connection, connection:
        result = connection.execute(empty_branch)
        assert len(result.description) == 8
        assert result.fetchall() == []
    rows = [metadata(args[3], 0, 31, evidence=True)]
    assert subject().decode_context_rows(rows, query=query)["full_matching_count"] == 31
    projection = subject().build_context_evidence_query(*args, index_rows=rows, **common)
    assert "FROM `ogilvy-trends-v2" not in projection.sql
    assert (
        subject().decode_context_rows([metadata(args[3], 0, 0, evidence=True)], query=projection)[
            "candidate_count"
        ]
        == 0
    )
    with pytest.raises(ValueError):
        subject().build_context_index_query(
            *args, **{**common, "enriched_rows": [metadata(args[3], 0, 0, evidence=True)]}
        )


def test_mixed_requirement_terms_keep_historical_content_only_parameters():
    args = inputs(terms=["contentterm"])
    mixed = copy.deepcopy(args[1])
    aggregate = copy.deepcopy(mixed["requirements"][0])
    aggregate.update(
        kind="aggregate",
        requirement_id="aggregate_only_term",
        search_terms=["differentaggregateterm"],
    )
    mixed["requirements"].append(aggregate)
    before = subject()._parameters(args[0], args[1], args[3], date(2026, 9, 7), 100)
    after = subject()._parameters(args[0], mixed, args[3], date(2026, 9, 7), 100)
    assert [p.to_api_repr() for p in after] == [p.to_api_repr() for p in before]


def test_index_and_evidence_queries_require_usable_source_content():
    args = inputs()
    candidates = candidate_rows(args[3])
    index = subject().build_context_index_query(
        *args,
        candidate_rows=candidates,
        lane="enriched_content",
        candidate_limit=10,
    )
    evidence = build_evidence(
        *args,
        candidate_rows=candidates,
        lane="enriched_content",
        candidate_limit=10,
    )
    direct_index = subject().build_context_index_query(
        *args,
        candidate_rows=[metadata(args[3], 0, 0)],
        lane="enriched_content",
        candidate_limit=10,
        partition_rows=[
            {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
            {"lane": "enriched_content", "partition_date": "2026-09-07", "partition_count": 1},
        ],
    )
    predicate = subject()._EVIDENCE_CONTENT_ELIGIBILITY
    keyed_date_guard = (
        "DATE(evidence.collected_at) BETWEEN DATE_SUB(@capture_cutoff, INTERVAL 6 DAY) "
        "AND @capture_cutoff"
    )
    assert predicate in index.sql
    assert predicate in evidence.sql
    assert predicate in direct_index.sql
    assert keyed_date_guard in index.sql
    assert keyed_date_guard in evidence.sql
    assert keyed_date_guard not in direct_index.sql

    legacy_index = subject()._build_context_evidence_query(
        *args,
        candidate_rows=candidates,
        lane="enriched_content",
        candidate_limit=10,
        _index=True,
        _validation_legacy=True,
    )
    assert keyed_date_guard not in legacy_index.sql
    assert [item.to_api_repr() for item in index.parameters] == [
        item.to_api_repr() for item in legacy_index.parameters
    ]


def test_content_eligibility_keeps_mixed_valid_rows_and_drops_blank_rows():
    with closing(sqlite3.connect(":memory:")) as connection, connection:
        connection.execute("CREATE TABLE evidence (id TEXT, text TEXT, title TEXT)")
        connection.executemany(
            "INSERT INTO evidence VALUES (?, ?, ?)",
            [
                ("text", " useful evidence ", None),
                ("title", None, " useful title "),
                ("empty", "", None),
                ("whitespace", "  ", "   "),
                ("null", None, None),
            ],
        )
        rows = connection.execute(
            "SELECT id FROM evidence evidence WHERE "
            + subject()._EVIDENCE_CONTENT_ELIGIBILITY
            + " ORDER BY id"
        ).fetchall()
    assert rows == [("text",), ("title",)]


def test_validation_only_legacy_builder_reconstructs_exact_prepredicate_sql():
    args = inputs()
    candidates = candidate_rows(args[3])
    expected = {
        "enriched_index": "647601b3e078ed1aecbe25769008cdd4a134c0e78f81d5b31364b09c7684cb63",
        "enriched": "e452d28210bce8d168eddc48a3af689a12dc066115aef44433bb0fc46f39033c",
        "raw_index": "d960a049af00f836a55ac815226a704470f3cfd53ca58727bc8951f0e1d1bb85",
        "raw": "88845e2d7147714ca85cb51a469875f25c6aa72767ec0a4f6a870aaabe0b68c1",
    }
    actual = {}
    for key, lane in (("enriched", "enriched_content"), ("raw", "raw_content")):
        common = {
            "candidate_rows": candidates,
            "lane": lane,
            "candidate_limit": 10,
            **({"enriched_rows": [metadata(args[3], 0, 0, evidence=True)]} if key == "raw" else {}),
        }
        actual[key + "_index"] = (
            subject()
            ._build_context_evidence_query(*args, _index=True, _validation_legacy=True, **common)
            .sql_digest
        )
        actual[key] = (
            subject()
            ._build_context_evidence_query(
                *args,
                index_rows=index_rows(args[3], lane),
                _validation_legacy=True,
                **common,
            )
            .sql_digest
        )
    assert actual == expected
    with pytest.raises(TypeError):
        subject().build_context_index_query(
            *args,
            candidate_rows=candidates,
            lane="enriched_content",
            candidate_limit=10,
            _validation_legacy=True,
        )
