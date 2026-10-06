import copy
import hashlib
import importlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import date, datetime, timedelta

import pytest
from src.analysis.open_intelligence.general_question_request import normalize_question_request

from tests.unit import test_intelligence_brain_live_reader as fixture


def module():
    return importlib.import_module("src.analysis.open_intelligence.general_question_source_query")


def request():
    return normalize_question_request(
        {"message": "What is changing in shared repair services?"},
        scope={
            "client_scope_id": "live_scope",
            "market_scope": ["za"],
            "brand_config_id": None,
            "audience_lens_ids": [],
            "theme_id": None,
        },
        request_id="c8b64b92-1b83-4212-9d65-2b7611432a3a",
        admitted_at=fixture.COMPLETED_AT + timedelta(days=1),
        policy_digest="a" * 64,
    )


def selected():
    return [{"run_id": fixture.RUN_ID, "signal_id": fixture.SIGNAL_ID, "market": "za"}]


def plan_window():
    evidence = fixture._receipt_and_rows()[1]["signal_evidence_v2"][0]
    return {
        "start": evidence["published_at"].date().isoformat(),
        "end": evidence["published_at"].date().isoformat(),
        "closed": True,
    }


def prepared():
    return module().build_selected_source_query(
        request(),
        selected(),
        plan_window=plan_window(),
        candidate_limit=10,
        evidence_limit=5,
    )


def json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(type(value))


def row(kind, value, *, run_id=None, signal_id=None, market=None):
    payload = json.dumps(value, default=json_value, sort_keys=False, separators=(",", ":"))
    return {
        "kind": kind,
        "run_id": run_id,
        "signal_id": signal_id,
        "market": market,
        "payload_json": payload,
        "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
    }


def rows(relations=None):
    _receipt, baseline = fixture._receipt_and_rows()
    relations = baseline if relations is None else relations
    receipt_row = relations["open_intelligence_run_receipts_v1"][0]
    metadata = {
        "receipt_count": len(relations["open_intelligence_run_receipts_v1"]),
        "receipt_json": json.dumps(receipt_row, default=json_value),
        "row_set_digest": receipt_row["row_set_digest"],
        "family_counts": {
            kind: sum(
                kind != "outcomes" or item["evaluated_at"] <= fixture.COMPLETED_AT
                for item in relations[relation]
            )
            for kind, relation in module().FAMILY_RELATIONS.items()
        },
        "manifest_count": 1,
        "copy_receipt_count": 1,
        "manifest": relations["open_intelligence_source_copy_manifest_v1"],
        "copy_receipts": relations["open_intelligence_source_copy_receipts_v1"],
    }
    output = [row("run_metadata", metadata, run_id=fixture.RUN_ID)]
    for kind, relation in module().FAMILY_RELATIONS.items():
        for value in relations[relation]:
            if kind == "outcomes" and value["evaluated_at"] > fixture.COMPLETED_AT:
                continue
            output.append(
                row(
                    kind,
                    value,
                    run_id=value["run_id"],
                    signal_id=value.get("signal_id"),
                    market=value["market"],
                )
            )
    total = len(output) - 1
    evidence_count = sum(value["kind"] == "evidence" for value in output)
    output.append(
        row(
            "request_metadata",
            {
                "request_digest": request()["request_digest"],
                "candidate_count": total,
                "available_content_rows": total,
                "overflow_count": 0,
                "evidence_count": evidence_count,
                "available_evidence_rows": evidence_count,
                "schema_rows": [],
                "schema_row_count": 0,
            },
        )
    )
    return output


def test_complete_seven_family_material_calls_real_validator(monkeypatch):
    from src.analysis.open_intelligence import brain_live_reader

    calls = []
    original = brain_live_reader._validate_complete_run_rows

    def validate(receipt, run_rows):
        calls.append(set(run_rows))
        return original(receipt, run_rows)

    monkeypatch.setattr(module(), "_validate_complete_run_rows", validate)
    data = module().decode_selected_source_rows(rows(), query=prepared())
    assert calls == [set(brain_live_reader.RECEIPT_RELATIONS)]
    assert data["candidate_count"] == 7
    assert "full_run_row_and_reference_validation" not in data["missing_checks"]


@pytest.mark.parametrize("evidence_total", [200, 201])
def test_inspected_evidence_selection_counts_are_not_integrity_gaps(evidence_total):
    relations = fixture._receipt_and_rows()[1]
    original = relations["signal_evidence_v2"][0]
    relations["signal_evidence_v2"] = [
        {**original, "evidence_id": f"ev_{i:03d}", "row_id": f"row_{i:03d}"}
        for i in range(evidence_total)
    ]
    relations["open_intelligence_run_receipts_v1"][0]["evidence_count"] = evidence_total
    query = module().build_selected_source_query(
        request(), selected(), plan_window=plan_window(), candidate_limit=300
    )
    data = module().decode_selected_source_rows(rows(relations), query=query)
    assert data["candidate_count"] == evidence_total + 6
    assert data["evidence_count"] == evidence_total
    assert len(data["selected_facts"]) == 200
    assert len(data["records"]["evidence"]) == evidence_total
    assert data["selection_counts"] == {
        "available_fact_count": evidence_total,
        "selected_fact_count": 200,
        "omitted_fact_count": evidence_total - 200,
        "limit": 200,
    }
    assert data["selection_truncation_reason"] == (
        "selection_limit" if evidence_total > 200 else None
    )
    assert "selected_evidence_limit" not in data["missing_checks"]
    assert "physical_schema_validation" in data["missing_checks"]


def test_partial_family_retains_validation_gap_and_no_facts(monkeypatch):
    values = [wire for wire in rows() if wire["kind"] != "analysis"]
    index = next(i for i, wire in enumerate(values) if wire["kind"] == "request_metadata")
    summary = json.loads(values[index]["payload_json"])
    summary.update(candidate_count=6, overflow_count=1)
    values[index] = row("request_metadata", summary)
    monkeypatch.setattr(
        module(), "_validate_complete_run_rows", lambda *args: pytest.fail("partial validated")
    )
    data = module().decode_selected_source_rows(values, query=prepared())
    assert data["selected_facts"] == []
    assert "full_run_row_and_reference_validation" in data["missing_checks"]
    assert "unselected_future_and_version_validation" in data["missing_checks"]
    assert data["selection_counts"] == {
        "available_fact_count": None,
        "selected_fact_count": 0,
        "omitted_fact_count": None,
        "limit": 5,
    }
    assert data["selection_truncation_reason"] == "inspection_truncated"
    assert "selected_content_overflow" in data["missing_checks"]


def test_later_outcome_excluded_without_changing_full_digest_expression():
    relations = fixture._receipt_and_rows()[1]
    later = {
        **relations["signal_outcomes_v2"][0],
        "outcome_id": "later",
        "evaluated_at": fixture.COMPLETED_AT + timedelta(days=1),
    }
    relations["signal_outcomes_v2"].append(later)
    data = module().decode_selected_source_rows(rows(relations), query=prepared())
    assert len(data["records"]["outcomes"]) == 1
    assert "t.evaluated_at <= rr.completed_at" in prepared().sql
    assert "f.evaluated_at <= rr.completed_at" in prepared().sql


def test_explicit_window_filters_facts_but_not_validation_rows():
    normalized = normalize_question_request(
        {"message": "What changed?"},
        scope={key: request()[key] for key in module()._SCOPE_FIELDS},
        request_id=request()["request_id"],
        admitted_at=fixture.COMPLETED_AT + timedelta(days=1),
        policy_digest="a" * 64,
        requested_window={"start": "2026-08-01", "end": "2026-08-02"},
    )
    query = module().build_selected_source_query(
        normalized,
        selected(),
        plan_window={"start": "2026-08-01", "end": "2026-08-02", "closed": True},
        candidate_limit=10,
        evidence_limit=5,
    )
    values = rows()
    index = next(i for i, wire in enumerate(values) if wire["kind"] == "request_metadata")
    summary = json.loads(values[index]["payload_json"])
    summary["request_digest"] = normalized["request_digest"]
    values[index] = row("request_metadata", summary)
    data = module().decode_selected_source_rows(values, query=query)
    assert len(data["records"]["evidence"]) == 1
    assert data["selected_facts"] == []
    assert "resolved_plan_window_and_scope_admission" in data["missing_checks"]


def test_stored_plan_window_filters_facts_when_request_has_no_explicit_window():
    query = module().build_selected_source_query(
        request(),
        selected(),
        plan_window={"start": "2026-08-01", "end": "2026-08-02", "closed": True},
        candidate_limit=10,
        evidence_limit=5,
    )
    data = module().decode_selected_source_rows(rows(), query=query)
    assert request()["requested_window"] is None
    assert query.plan_window == {"start": "2026-08-01", "end": "2026-08-02", "closed": True}
    assert data["selected_facts"] == []
    assert len(data["records"]["evidence"]) == 1


@pytest.mark.parametrize(
    "family,field",
    [
        ("signal_predictions_v2", "rule_version"),
        ("signal_candidates_v2", "cluster_build_version"),
    ],
)
def test_complete_family_version_mutation_refuses(family, field):
    relations = fixture._receipt_and_rows()[1]
    relations[family][0][field] = "wrong"
    with pytest.raises(ValueError):
        module().decode_selected_source_rows(rows(relations), query=prepared())


def test_single_select_has_typed_parameters_and_explicit_conservative_bounds():
    query = prepared()
    assert query.template_id == "released_evidence_v1"
    assert query.sql.lstrip().startswith("WITH ")
    assert ";" not in query.sql
    assert query.candidate_limit == 10
    assert query.transport_row_limit == 12
    assert isinstance(query.parameters, tuple)
    assert query.sql_digest == hashlib.sha256(query.sql.encode()).hexdigest()
    assert "source_provenance_json" in query.sql
    assert "INFORMATION_SCHEMA.COLUMNS" in query.sql
    assert "row_set_membership" in query.sql
    assert "@client_scope_id" in query.sql
    assert fixture.RUN_ID not in query.sql
    assert "candidate_count" in query.sql
    assert "overflow_count" in query.sql


@pytest.mark.parametrize("version", ["hybrid_graph_v1", "hybrid_graph_v2", "hybrid_graph_v3"])
def test_digest_dispatch_pins_each_receipt_version(version):
    from src.analysis.open_intelligence.persistence import row_set_digest_sql

    expression = row_set_digest_sql("r.run_id", sql_expression=True, cluster_build_version=version)
    sql = prepared().sql
    assert f"WHEN '{version}' THEN {expression}" in sql
    assert "CASE rr.cluster_build_version" in sql
    assert "ELSE ERROR('source_material_version_invalid') END" in sql


def test_valid_v3_material_retains_provenance():
    relations = fixture._v3_rows()
    data = module().decode_selected_source_rows(rows(relations), query=prepared())
    assert (
        data["records"]["membership"][0]["source_provenance_json"]
        == (relations["signal_membership_v2"][0]["source_provenance_json"])
    )
    assert data["records"]["candidates"][0]["cluster_build_version"] == "hybrid_graph_v3"
    assert "unselected_future_and_version_validation" not in data["missing_checks"]


@pytest.mark.parametrize("count, returned", [(1, 0), (1001, 1001), (1500, 1001)])
def test_incomplete_or_over_cap_schema_material_refuses(count, returned):
    values = rows()
    index = next(i for i, value in enumerate(values) if value["kind"] == "request_metadata")
    metadata = json.loads(values[index]["payload_json"])
    metadata.update(schema_row_count=count, schema_rows=[{} for _ in range(returned)])
    values[index] = row("request_metadata", metadata)
    with pytest.raises(ValueError):
        module().decode_selected_source_rows(values, query=prepared())


def test_complete_schema_metadata_at_cap_is_retained_as_unvalidated_data():
    values = rows()
    index = next(i for i, value in enumerate(values) if value["kind"] == "request_metadata")
    metadata = json.loads(values[index]["payload_json"])
    metadata.update(schema_row_count=1000, schema_rows=[{} for _ in range(1000)])
    values[index] = row("request_metadata", metadata)
    data = module().decode_selected_source_rows(values, query=prepared())
    assert len(data["schema_rows"]) == 1000
    assert "physical_schema_validation" in data["missing_checks"]


def test_selected_material_retains_original_citations_without_issuing_authority():
    data = module().decode_selected_source_rows(rows(), query=prepared())
    assert data["candidate_count"] == 7
    evidence = data["records"]["evidence"][0]
    original = fixture._receipt_and_rows()[1]["signal_evidence_v2"][0]
    for key in (
        "row_id",
        "author_label",
        "url",
        "platform",
        "published_at",
        "run_id",
        "signal_id",
        "client_scope_id",
    ):
        assert evidence[key] == original[key]
    assert data["request_scope"]["brand_config_id"] is None
    assert evidence["brand_config_id"] == original["brand_config_id"]
    assert data["collection_time_available"] is False
    assert data["selected_facts"][0]["collected_at"] is None
    assert data["selected_facts"][0]["author"] == original["author_label"]
    assert data["missing_checks"]
    assert "authority" not in data
    assert "full_run_row_and_reference_validation" not in data["missing_checks"]


@pytest.mark.parametrize("nullable", [False, True])
def test_fact_preserves_actual_market_excerpt_and_source_label(nullable):
    relations = fixture._receipt_and_rows()[1]
    evidence = relations["signal_evidence_v2"][0]
    evidence["excerpt"] = None if nullable else "Synthetic attributed observation."
    evidence["source_label"] = None if nullable else "Synthetic source label"
    data = module().decode_selected_source_rows(rows(relations), query=prepared())
    fact = data["selected_facts"][0]
    assert fact["market"] == evidence["market"]
    assert fact["excerpt"] == evidence["excerpt"]
    assert fact["source_label"] == evidence["source_label"]


def test_cross_market_reused_evidence_ids_keep_attribution_and_sorting():
    relations = fixture._receipt_and_rows()[1]
    for relation in module().FAMILY_RELATIONS.values():
        for record in relations[relation]:
            record["market_scope"] = ["ng", "za"]
    receipt = relations["open_intelligence_run_receipts_v1"][0]
    receipt.update(market_scope=["za", "ng"], candidate_count=2, evidence_count=2)
    ng_signal = "sig_" + "f" * 64
    relations["signal_candidates_v2"].append(
        {
            **relations["signal_candidates_v2"][0],
            "market": "ng",
            "signal_id": ng_signal,
        }
    )
    relations["signal_evidence_v2"][0].update(excerpt="Synthetic ZA fact", source_label="ZA source")
    relations["signal_evidence_v2"].append(
        {
            **relations["signal_evidence_v2"][0],
            "market": "ng",
            "signal_id": ng_signal,
            "excerpt": "Synthetic NG fact",
            "source_label": "NG source",
        }
    )
    normalized = normalize_question_request(
        {"message": "Compare the two markets."},
        scope={
            **{key: request()[key] for key in module()._SCOPE_FIELDS},
            "market_scope": ["ng", "za"],
        },
        request_id=request()["request_id"],
        admitted_at=fixture.COMPLETED_AT + timedelta(days=1),
        policy_digest="a" * 64,
    )
    query = module().build_selected_source_query(
        normalized,
        [*selected(), {"run_id": fixture.RUN_ID, "signal_id": ng_signal, "market": "ng"}],
        plan_window=plan_window(),
        candidate_limit=20,
        evidence_limit=5,
    )
    values = rows(relations)
    index = next(i for i, wire in enumerate(values) if wire["kind"] == "request_metadata")
    summary = json.loads(values[index]["payload_json"])
    summary["request_digest"] = normalized["request_digest"]
    values[index] = row("request_metadata", summary)
    facts = module().decode_selected_source_rows(values, query=query)["selected_facts"]
    assert [fact["market"] for fact in facts] == ["ng", "za"]
    assert facts[0]["evidence_id"] == facts[1]["evidence_id"]
    assert facts[0]["row_id"] == facts[1]["row_id"]
    assert [(fact["excerpt"], fact["source_label"]) for fact in facts] == [
        ("Synthetic NG fact", "NG source"),
        ("Synthetic ZA fact", "ZA source"),
    ]
    assert (
        module().decode_selected_source_rows(list(reversed(values)), query=query)["selected_facts"]
        == facts
    )


@pytest.mark.parametrize("family", ["evidence", "membership"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("brand_config_id", "other_producer_brand"),
        ("theme_id", "other_producer_theme"),
        ("audience_lens_ids", ["other_producer_lens"]),
    ],
)
def test_selected_producer_scope_must_match_candidate(family, field, value):
    values = rows()
    index = next(i for i, wire in enumerate(values) if wire["kind"] == family)
    wire = values[index]
    record = json.loads(wire["payload_json"])
    record[field] = value
    values[index] = row(
        family, record, run_id=wire["run_id"], signal_id=wire["signal_id"], market=wire["market"]
    )
    with pytest.raises(ValueError, match="source_material_invalid"):
        module().decode_selected_source_rows(values, query=prepared())


def test_missing_candidate_under_cap_withholds_selected_facts():
    values = [wire for wire in rows() if wire["kind"] != "candidates"]
    index = next(i for i, wire in enumerate(values) if wire["kind"] == "request_metadata")
    metadata = json.loads(values[index]["payload_json"])
    metadata.update(candidate_count=6, overflow_count=1)
    values[index] = row("request_metadata", metadata)
    data = module().decode_selected_source_rows(values, query=prepared())
    assert data["records"]["evidence"]
    assert data["selected_facts"] == []
    assert "selected_producer_scope_unverifiable" in data["missing_checks"]


def test_candidate_after_evidence_still_checks_producer_scope():
    values = rows()
    values.sort(key=lambda wire: wire["kind"] == "candidates")
    data = module().decode_selected_source_rows(values, query=prepared())
    assert len(data["selected_facts"]) == 1
    assert data["request_scope"]["brand_config_id"] is None
    assert data["selected_facts"][0]["source_scope"]["brand_config_id"] == "live_brand"


@pytest.mark.parametrize(
    "mutation",
    [
        "hash",
        "foreign",
        "duplicate",
        "future",
        "null_source_brand",
        "count",
        "overflow",
        "missing_metadata",
    ],
)
def test_invalid_returned_material_refuses(mutation):
    values = rows()
    index = next(i for i, value in enumerate(values) if value["kind"] == "evidence")
    wire = values[index]
    if mutation == "hash":
        wire["payload_sha256"] = "a" * 64
    elif mutation == "duplicate":
        values.append(copy.deepcopy(wire))
    elif mutation == "missing_metadata":
        values = [value for value in values if value["kind"] != "run_metadata"]
    elif mutation in ("count", "overflow"):
        index = next(i for i, value in enumerate(values) if value["kind"] == "request_metadata")
        value = json.loads(values[index]["payload_json"])
        value["candidate_count" if mutation == "count" else "overflow_count"] = (
            True if mutation == "count" else -1
        )
        values[index] = row("request_metadata", value)
    else:
        value = json.loads(wire["payload_json"])
        if mutation == "foreign":
            value["client_scope_id"] = "foreign"
        elif mutation == "future":
            value["published_at"] = (fixture.COMPLETED_AT + timedelta(days=1)).isoformat()
        else:
            value["brand_config_id"] = None
        values[index] = row(
            "evidence",
            value,
            run_id=wire["run_id"],
            signal_id=wire["signal_id"],
            market=wire["market"],
        )
    with pytest.raises(ValueError):
        module().decode_selected_source_rows(values, query=prepared())


@pytest.mark.parametrize(
    "mutation", ["partial", "receipt_duplicate", "sha", "digest", "copy_count"]
)
def test_existing_receipt_source_validators_are_retained(mutation):
    values = rows()
    metadata = json.loads(values[0]["payload_json"])
    receipt = json.loads(metadata["receipt_json"])
    if mutation == "partial":
        receipt["complete_partitions"] = False
    elif mutation == "receipt_duplicate":
        metadata["receipt_count"] = 2
    elif mutation == "sha":
        receipt["source_sha"] = "invalid"
    elif mutation == "digest":
        metadata["row_set_digest"] = "a" * 64
    else:
        metadata["copy_receipts"][0]["source_rows"] = 2
    metadata["receipt_json"] = json.dumps(receipt)
    values[0] = row("run_metadata", metadata, run_id=fixture.RUN_ID)
    with pytest.raises(ValueError):
        module().decode_selected_source_rows(values, query=prepared())


def test_positive_overflow_is_returned_as_explicit_data_not_completeness():
    values = rows()
    for index, value in enumerate(values):
        if value["kind"] == "request_metadata":
            metadata = json.loads(value["payload_json"])
            metadata.update(available_content_rows=10000, overflow_count=9993)
            values[index] = row("request_metadata", metadata)
    material = module().decode_selected_source_rows(values, query=prepared())
    assert material["overflow_count"] == 9993
    assert "selected_content_overflow" in material["missing_checks"]


@pytest.mark.parametrize("change", ["market", "duplicate", "limit", "evidence_limit"])
def test_selection_and_limits_are_admitted_before_sql(change):
    identities = selected()
    limits = {"candidate_limit": 10, "evidence_limit": 5}
    if change == "market":
        identities[0]["market"] = "ke"
    elif change == "duplicate":
        identities *= 2
    elif change == "limit":
        limits["candidate_limit"] = True
    else:
        limits["evidence_limit"] = 201
    with pytest.raises(ValueError):
        module().build_selected_source_query(
            request(), identities, plan_window=plan_window(), **limits
        )


def test_evidence_ceiling_is_independent_from_inspection_reservation():
    query = module().build_selected_source_query(
        request(),
        selected(),
        plan_window=plan_window(),
        candidate_limit=4,
        evidence_limit=200,
    )
    assert query.candidate_limit == 4
    assert query.evidence_limit == 200


@pytest.mark.parametrize(
    "mutation", [None, "null", "foreign", "duplicate", "future", "partial", "copy_tamper"]
)
def test_selected_checks_match_existing_python_reader_decisions(mutation):
    from src.analysis.open_intelligence.brain_live_reader import read_live_brain_authority

    _, relations = fixture._receipt_and_rows()
    if mutation == "null":
        relations["signal_evidence_v2"][0]["brand_config_id"] = None
    elif mutation == "foreign":
        relations["signal_evidence_v2"][0]["client_scope_id"] = "foreign"
    elif mutation == "duplicate":
        relations["signal_candidates_v2"] *= 2
    elif mutation == "future":
        relations["signal_evidence_v2"][0]["published_at"] = fixture.COMPLETED_AT + timedelta(
            days=1
        )
    elif mutation == "partial":
        relations["open_intelligence_run_receipts_v1"][0]["complete_partitions"] = False
    elif mutation == "copy_tamper":
        relations["open_intelligence_source_copy_receipts_v1"][0]["source_rows"] = 2
    old_accepted = new_accepted = False
    try:
        read_live_brain_authority(
            client=fixture._Client(relations), run_id=fixture.RUN_ID, signal_id=fixture.SIGNAL_ID
        )
        old_accepted = True
    except Exception:
        old_accepted = False
    try:
        module().decode_selected_source_rows(rows(relations), query=prepared())
        new_accepted = True
    except ValueError:
        new_accepted = False
    assert old_accepted is (mutation is None)
    assert new_accepted == old_accepted


def test_unselected_future_row_remains_a_required_full_run_check():
    from src.analysis.open_intelligence.brain_live_reader import read_live_brain_authority

    _, relations = fixture._receipt_and_rows()
    unselected = copy.deepcopy(relations["signal_evidence_v2"][0])
    unselected.update(
        signal_id="sig_" + "f" * 64,
        evidence_id="ev_unselected",
        published_at=fixture.COMPLETED_AT + timedelta(days=1),
    )
    relations["signal_evidence_v2"].append(unselected)
    relations["open_intelligence_run_receipts_v1"][0]["evidence_count"] = 2
    with pytest.raises(ValueError):
        read_live_brain_authority(
            client=fixture._Client(relations), run_id=fixture.RUN_ID, signal_id=fixture.SIGNAL_ID
        )
    with pytest.raises(ValueError):
        module().decode_selected_source_rows(rows(relations), query=prepared())
