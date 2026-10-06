import copy
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from src.analysis.open_intelligence import production_snapshot_rows as subject

CUTOFF = date(2030, 1, 2)
SOURCE_AS_OF = datetime(2030, 1, 3, tzinfo=UTC)
CAPTURED_AT = datetime(2030, 1, 3, 0, 5, tzinfo=UTC)
MARKETS = ("ng", "za")


def metadata_bytes():
    return (
        Path(__file__).resolve().parents[1]
        / "fixtures/open_intelligence/v3_source_relation_metadata.json"
    ).read_bytes()


def field_schema(lane):
    resource = json.loads(metadata_bytes())["tables"][f"trends_v2_dev.{lane}"]
    return {field["name"]: field for field in resource["schema"]["fields"]}


def value(field, schema, *, market="za", row_id="row"):
    kind = schema.get("type")
    mode = schema.get("mode", "NULLABLE")
    if field == "market":
        return market
    if field in {"trend_date", "proposed_date"}:
        return CUTOFF
    if field == "event_date":
        return CUTOFF - timedelta(days=1)
    if field in {"collected_at", "published_at", "as_of"}:
        return SOURCE_AS_OF - timedelta(hours=1)
    if field == "id":
        return row_id
    if field == "ledger_id":
        return "ledger_" + row_id
    if field == "candidate_id":
        return "candidate_" + row_id
    if field == "sample_row_ids":
        return []
    if field == "source":
        return "reddit"
    if field == "platform":
        return "reddit"
    if field == "content_type":
        return "post"
    if mode == "REPEATED":
        return []
    if kind in {"INTEGER", "INT64"}:
        return 1
    if kind in {"FLOAT", "FLOAT64"}:
        return 1.0
    if kind in {"BOOLEAN", "BOOL"}:
        return False
    return field + "_value"


def row(lane, *, market="za", row_id="row", samples=()):
    schema = field_schema(lane)
    fields = subject.physical_fields(lane)
    output = {field: value(field, schema[field], market=market, row_id=row_id) for field in fields}
    if "sample_row_ids" in output:
        output["sample_row_ids"] = list(samples)
    return output


def rows():
    graph = row(
        "seed_graph",
        row_id="graph",
        samples=("raw", "enriched", "missing", "duplicate", "unsupported"),
    )
    return {
        "event_ledger": [row("event_ledger", row_id="event")],
        "seed_graph": [graph],
        "seed_candidates": [],
        "enriched_content": [
            row("enriched_content", row_id="enriched"),
            row("enriched_content", row_id="duplicate"),
            {**row("enriched_content", row_id="duplicate"), "text": "second"},
            {
                **row("enriched_content", row_id="unsupported"),
                "source": "unknown_source",
                "platform": "unknown_platform",
            },
        ],
        "raw_content": [row("raw_content", row_id="raw")],
    }


def normalize(values):
    return subject.normalize_snapshot_rows(
        CUTOFF,
        source_as_of=SOURCE_AS_OF,
        captured_at=CAPTURED_AT,
        market_scope=MARKETS,
        reviewed_metadata=metadata_bytes(),
        rows_by_table=values,
    )


def test_normalizes_fixed_rows_and_preserves_existing_fallback_and_refusals():
    result = normalize(rows())
    assert result["requested_sample_keys"] == [
        ["za", "duplicate"],
        ["za", "enriched"],
        ["za", "missing"],
        ["za", "raw"],
        ["za", "unsupported"],
    ]
    assert [(item["row_id"], item["source_table"]) for item in result["resolved_refs"]] == [
        ("enriched", "enriched_content"),
        ("raw", "raw_content"),
    ]
    assert result["ambiguous_refs"] == [["za", "duplicate"]]
    assert result["missing_refs"] == [["za", "missing"]]
    assert result["unsupported_refs"] == [["za", "unsupported"]]
    assert result["collection_complete"] is False
    assert result["source_as_of"].endswith("+00:00")
    assert result["physical_rows_by_table"]["raw_content"][0]["collected_at"].endswith("+00:00")
    assert result["physical_table_digests"]["raw_content"] == subject._digest(
        result["physical_rows_by_table"]["raw_content"]
    )
    assert (
        result["physical_table_digests"]["enriched_content"]
        != result["adapted_table_digests"]["enriched_content"]
    )
    for lane in ("enriched_content", "raw_content"):
        for item in result["adapted_rows_by_table"][lane]:
            assert all(item[field] is None for field in subject.ABSENT_NULLABLE_FIELDS)


def test_input_order_does_not_change_rows_or_digests():
    first = rows()
    second = {lane: list(reversed(values)) for lane, values in first.items()}
    assert normalize(first) == normalize(second)


def test_adapted_evidence_fields_follow_current_logical_schema_order():
    result = normalize(rows())
    for lane in ("enriched_content", "raw_content"):
        expected = tuple(subject.pipeline.EVIDENCE_COLUMNS_BY_TABLE[lane])
        assert result["adapted_rows_by_table"][lane]
        for item in result["adapted_rows_by_table"][lane]:
            assert tuple(item) == expected
        assert result["adapted_table_digests"][lane] == subject._digest(
            result["adapted_rows_by_table"][lane]
        )


def test_date_like_string_is_preserved_while_declared_timestamp_normalizes_to_utc():
    values = rows()
    values["raw_content"][0]["query_term"] = "2030-01-02T00:00:00Z"
    values["raw_content"][0]["collected_at"] = "2030-01-02T14:00:00+02:00"
    result = normalize(values)
    item = result["physical_rows_by_table"]["raw_content"][0]
    assert item["query_term"] == "2030-01-02T00:00:00Z"
    assert item["collected_at"] == "2030-01-02T12:00:00+00:00"


def test_existing_pipeline_mixed_null_rows_receive_deterministic_null_safe_order():
    null_row = row("event_ledger", row_id="null")
    null_row["entity_key"] = None
    newline_row = row("event_ledger", row_id="newline")
    newline_row["entity_key"] = "\nkey"
    text_row = row("event_ledger", row_id="text")
    text_row["entity_key"] = "Alpha"

    class Job:
        def result(self, *, max_results):
            assert max_results == subject.pipeline.SOURCE_CEILINGS["event_ledger"] + 1
            return [null_row, newline_row, text_row]

    class Client:
        def query(self, *_args, **_kwargs):
            return Job()

    loaded = subject.pipeline._load_source(
        Client(),
        table="event_ledger",
        columns=subject.pipeline.EVENT_COLUMNS,
        date_column="trend_date",
        trend_date=CUTOFF,
        markets=MARKETS,
    )
    values = rows()
    values["event_ledger"] = list(reversed(loaded))
    result = normalize(values)
    ordered = result["physical_rows_by_table"]["event_ledger"]
    assert [item["entity_key"] for item in ordered] == [None, "\nkey", "Alpha"]
    assert [item["entity_key"] for item in loaded] == [None, "\nkey", "Alpha"]
    assert normalize({**values, "event_ledger": list(loaded)}) == result


@pytest.mark.parametrize(
    "mutation",
    [
        "market",
        "candidate_date",
        "evidence_date",
        "future",
        "event_future",
        "graph_future",
        "candidate_status",
        "naive",
        "type",
        "extra",
        "repeated_null",
    ],
)
def test_wrong_scope_time_and_physical_shape_refuse(mutation):
    values = rows()
    if mutation == "market":
        values["seed_graph"][0]["market"] = "ke"
    elif mutation == "candidate_date":
        values["seed_graph"][0]["trend_date"] = CUTOFF - timedelta(days=1)
    elif mutation == "evidence_date":
        values["raw_content"][0]["collected_at"] = SOURCE_AS_OF - timedelta(days=8)
    elif mutation == "future":
        values["raw_content"][0]["published_at"] = SOURCE_AS_OF + timedelta(seconds=1)
    elif mutation == "event_future":
        values["event_ledger"][0]["as_of"] = SOURCE_AS_OF + timedelta(seconds=1)
    elif mutation == "graph_future":
        values["seed_graph"][0]["event_date"] = CUTOFF + timedelta(days=1)
    elif mutation == "candidate_status":
        values["seed_candidates"] = [row("seed_candidates", row_id="bad")]
        values["seed_candidates"][0]["status"] = "pending"
    elif mutation == "naive":
        values["raw_content"][0]["collected_at"] = datetime(2030, 1, 2, 12)
    elif mutation == "type":
        values["event_ledger"][0]["source_count"] = True
    elif mutation == "extra":
        values["event_ledger"][0]["unexpected"] = "x"
    else:
        values["event_ledger"][0]["entity_aliases"] = None
    with pytest.raises(ValueError, match="snapshot_rows_invalid"):
        normalize(values)


def test_no_truncation_extra_unrequested_or_too_many_sample_ids_refuse():
    values = rows()
    values["raw_content"].append(row("raw_content", row_id="not_requested"))
    with pytest.raises(ValueError, match="snapshot_rows_invalid"):
        normalize(values)
    values = rows()
    values["seed_graph"][0]["sample_row_ids"].append("sixth")
    with pytest.raises(ValueError, match="snapshot_rows_invalid"):
        normalize(values)


def test_duplicate_candidate_identity_and_excess_evidence_rows_refuse():
    values = rows()
    values["seed_graph"].append(copy.deepcopy(values["seed_graph"][0]))
    with pytest.raises(ValueError, match="snapshot_rows_invalid"):
        normalize(values)
    values = rows()
    values["raw_content"].extend(
        [
            {**row("raw_content", row_id="raw"), "text": "third"},
            {**row("raw_content", row_id="raw"), "text": "fourth"},
            {**row("raw_content", row_id="raw"), "text": "fifth"},
        ]
    )
    with pytest.raises(ValueError, match="snapshot_rows_invalid"):
        normalize(values)


def test_physical_tampering_changes_physical_and_material_digests():
    first = normalize(rows())
    changed = rows()
    changed["enriched_content"][0]["text"] = "changed synthetic text"
    second = normalize(changed)
    assert (
        first["physical_table_digests"]["enriched_content"]
        != second["physical_table_digests"]["enriched_content"]
    )
    assert first["material_digest"] != second["material_digest"]


def scaling_rows(size):
    graph = []
    evidence = []
    for offset in range(0, size, 5):
        ids = [f"sample_{index:04d}" for index in range(offset, min(size, offset + 5))]
        graph_row = row("seed_graph", row_id=f"graph_{offset:04d}", samples=ids)
        graph_row["term"] = f"term_{offset:04d}"
        graph.append(graph_row)
        evidence.extend(row("enriched_content", row_id=row_id) for row_id in ids)
    return {
        "event_ledger": [],
        "seed_graph": graph,
        "seed_candidates": [],
        "enriched_content": evidence,
        "raw_content": [],
    }


def normalization_line_events(size):
    values = scaling_rows(size)
    count = 0

    def trace(frame, event, arg):
        nonlocal count
        if event == "line" and frame.f_code.co_filename == subject.__file__:
            count += 1
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        result = normalize(values)
    finally:
        sys.settrace(previous)
    assert len(result["resolved_refs"]) == size
    assert result["resolved_refs"][0]["row_id"] == "sample_0000"
    assert result["resolved_refs"][-1]["row_id"] == f"sample_{size - 1:04d}"
    return count


def test_resolved_reference_join_scales_linearly_by_python_operations():
    small = normalization_line_events(100)
    large = normalization_line_events(200)
    assert large * 10 <= small * 22


def test_backdated_capture_and_changed_reviewed_metadata_refuse():
    values = rows()
    with pytest.raises(ValueError, match="snapshot_rows_invalid"):
        subject.normalize_snapshot_rows(
            CUTOFF,
            source_as_of=SOURCE_AS_OF,
            captured_at=SOURCE_AS_OF - timedelta(seconds=1),
            market_scope=MARKETS,
            reviewed_metadata=metadata_bytes(),
            rows_by_table=values,
        )
    changed = json.loads(metadata_bytes())
    changed["tables"]["trends_v2_dev.raw_content"]["schema"]["fields"][0]["type"] = "BYTES"
    with pytest.raises(ValueError, match="snapshot_rows_invalid"):
        subject.normalize_snapshot_rows(
            CUTOFF,
            source_as_of=SOURCE_AS_OF,
            captured_at=CAPTURED_AT,
            market_scope=MARKETS,
            reviewed_metadata=json.dumps(changed).encode(),
            rows_by_table=values,
        )
