"""Offline recursive physical metadata comparisons using SDK table resources."""

import copy
import importlib
from pathlib import Path

import pytest
from google.cloud import bigquery
from src.contracts.bigquery_ddl import parse_table_ddl

TABLES = (
    "signal_candidates_v2",
    "signal_evidence_v2",
    "signal_membership_v2",
    "signal_lineage_v2",
    "signal_predictions_v2",
    "signal_outcomes_v2",
    "signal_analysis_v2",
    "open_intelligence_run_receipts_v1",
)
ROOT = Path(__file__).resolve().parents[2]


def resources():
    def field(value):
        name, kind, mode, description, children = value
        return bigquery.SchemaField(
            name,
            kind,
            mode=mode,
            description=description,
            fields=tuple(field(child) for child in children),
        )

    output = []
    for name in TABLES:
        ddl = (ROOT / "infra" / "bigquery_schemas" / (name + ".sql")).read_text()
        parsed = parse_table_ddl(
            ddl.format(project="ogilvy-trends-v2", dataset="trends_v2_staging")
        )
        table = bigquery.Table(
            "ogilvy-trends-v2.trends_v2_staging." + name,
            schema=[field(item) for item in parsed["fields"]],
        )
        output.append(table.to_api_repr())
    return output


def validate(values, version="hybrid_graph_v3"):
    module = importlib.import_module("src.analysis.open_intelligence.general_question_schema")
    return module.validate_source_schemas(values, cluster_build_version=version)


@pytest.mark.parametrize("version", ["hybrid_graph_v1", "hybrid_graph_v2", "hybrid_graph_v3"])
def test_declared_sdk_resources_and_reordered_fields_are_data_only(version):
    values = resources()
    for table in values:
        table["schema"]["fields"].reverse()
        for field in table["schema"]["fields"]:
            if "fields" in field:
                field["fields"].reverse()
    result = validate(values[::-1], version)
    assert result["source_authority"] is False
    assert result["table_resources"] == values[::-1]
    assert len(result["schema_digests"]) == 8
    assert result["table_resources"] is not values


@pytest.mark.parametrize("version", ["hybrid_graph_v1", "hybrid_graph_v2"])
def test_legacy_provenance_is_optional_but_nullable_when_present(version):
    values = resources()
    fields = values[2]["schema"]["fields"]
    fields[:] = [field for field in fields if field["name"] != "source_provenance_json"]
    assert validate(values, version)["source_authority"] is False


def test_safe_nullable_addition_is_compatible_without_physical_order_requirement():
    values = resources()
    values[0]["schema"]["fields"].insert(
        0, bigquery.SchemaField("extra_note", "STRING", mode="NULLABLE").to_api_repr()
    )
    assert validate(values)["source_authority"] is False


def test_native_omitted_nullable_mode_preserves_schema_and_raw_metadata():
    values = resources()
    expected = validate(values)
    field = next(
        item for item in values[2]["schema"]["fields"] if item["name"] == "source_provenance_json"
    )
    assert field.pop("mode") == "NULLABLE"
    actual = validate(values)
    assert actual["schema_digests"] == expected["schema_digests"]
    assert actual["table_resources"] == values


@pytest.mark.parametrize("mode", [None, True, "", {}])
def test_explicit_malformed_mode_is_not_a_nullable_default(mode):
    values = resources()
    field = next(
        item for item in values[2]["schema"]["fields"] if item["name"] == "source_provenance_json"
    )
    field["mode"] = mode
    with pytest.raises(ValueError, match="physical_schema_invalid"):
        validate(values)


@pytest.mark.parametrize(
    "case",
    [
        "child_mode",
        "missing_mode",
        "duplicate_field",
        "missing_field",
        "wrong_identity",
        "duplicate_table",
        "missing_table",
        "v3_missing_provenance",
        "required_provenance",
        "wrong_array",
        "required_addition",
        "unknown_version",
    ],
)
def test_invalid_native_metadata_refuses(case):
    values = resources()
    fields = values[4]["schema"]["fields"]
    baseline = next(item for item in fields if item["name"] == "baseline")
    if case == "child_mode":
        baseline["fields"][0]["mode"] = "NULLABLE"
    elif case == "missing_mode":
        del baseline["fields"][0]["mode"]
    elif case == "duplicate_field":
        fields.append(copy.deepcopy(fields[0]))
    elif case == "missing_field":
        fields.pop(0)
    elif case == "wrong_identity":
        values[0]["tableReference"]["datasetId"] = "trends_v2"
    elif case == "duplicate_table":
        values[-1] = copy.deepcopy(values[0])
    elif case == "missing_table":
        values.pop()
    elif case in ("v3_missing_provenance", "required_provenance"):
        membership = values[2]["schema"]["fields"]
        provenance = next(item for item in membership if item["name"] == "source_provenance_json")
        if case == "v3_missing_provenance":
            membership.remove(provenance)
        else:
            provenance["mode"] = "REQUIRED"
    elif case == "wrong_array":
        next(item for item in fields if item["name"] == "market_scope")["mode"] = "NULLABLE"
    elif case == "required_addition":
        fields.append(bigquery.SchemaField("extra", "STRING", mode="REQUIRED").to_api_repr())
    with pytest.raises(ValueError):
        validate(values, "unknown" if case == "unknown_version" else "hybrid_graph_v3")


@pytest.mark.parametrize(
    "case", ["bytes", "depth", "field_count", "missing_schema", "nested_duplicate"]
)
def test_metadata_bounds_and_nested_completeness(case):
    values = resources()
    fields = values[4]["schema"]["fields"]
    if case == "bytes":
        values[0]["description"] = "x" * 1_000_001
    elif case == "depth":
        child = {"name": "leaf", "type": "STRING", "mode": "NULLABLE"}
        for _ in range(10):
            child = {"name": "nested", "type": "RECORD", "mode": "NULLABLE", "fields": [child]}
        fields.append(child)
    elif case == "field_count":
        fields.extend(
            {"name": f"extra_{i}", "type": "STRING", "mode": "NULLABLE"} for i in range(1001)
        )
    elif case == "missing_schema":
        del values[0]["schema"]
    else:
        children = next(item for item in fields if item["name"] == "baseline")["fields"]
        children.append(copy.deepcopy(children[0]))
    with pytest.raises(ValueError):
        validate(values)


def test_semantic_digest_ignores_order_and_preserves_raw_metadata():
    original = resources()
    reordered = copy.deepcopy(original)
    for table in reordered:
        table["schema"]["fields"].reverse()
    left, right = validate(original), validate(reordered)
    assert left["schema_digests"] == right["schema_digests"]
    assert left["metadata_digest"] != right["metadata_digest"]
    right["table_resources"][0]["schema"]["fields"].clear()
    assert reordered[0]["schema"]["fields"]


@pytest.mark.parametrize("version", ["hybrid_graph_v1", "hybrid_graph_v2", "hybrid_graph_v3"])
def test_provenance_wrong_type_or_mode_cannot_hide_as_an_addition(version):
    values = resources()
    provenance = next(
        field
        for field in values[2]["schema"]["fields"]
        if field["name"] == "source_provenance_json"
    )
    provenance["mode"] = "REQUIRED"
    with pytest.raises(ValueError):
        validate(values, version)
    provenance["mode"] = "NULLABLE"
    provenance["type"] = "JSON"
    with pytest.raises(ValueError):
        validate(values, version)
