import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from scripts.staging.replay_open_intelligence import _digest
from src.analysis.open_intelligence import production_snapshot as subject
from src.analysis.open_intelligence.brain_contract import canonical_bytes

from tests.unit import test_production_snapshot_rows as row_fixture
from tests.unit import test_production_snapshot_tables as table_fixture

CLIENT_SCOPE_ID = "ogilvy_default"
COVERAGE_REFS = (
    {
        "kind": "pipeline_run",
        "receipt_id": "run_synthetic_1",
        "receipt_digest": "2" * 64,
    },
    {
        "kind": "source_window",
        "receipt_id": "window_synthetic_1",
        "receipt_digest": "1" * 64,
    },
)


def inputs():
    plan = table_fixture.build_plan()
    native_rows = row_fixture.rows()
    snapshot_metadata = table_fixture.snapshot_metadata(plan)
    readback_rows = table_fixture.readback(plan)
    for lane in subject.LANES:
        verified_full_snapshot_row_count = len(native_rows[lane])
        snapshot_metadata[lane]["numRows"] = str(verified_full_snapshot_row_count)
        next(item for item in readback_rows if item["lane"] == lane)["row_count"] = (
            verified_full_snapshot_row_count
        )
    return {
        "cutoff_date": row_fixture.CUTOFF,
        "captured_at": row_fixture.CAPTURED_AT,
        "client_scope_id": CLIENT_SCOPE_ID,
        "market_scope": row_fixture.MARKETS,
        "reviewed_metadata": table_fixture.source_metadata_bytes(),
        "plan": plan,
        "snapshot_metadata": snapshot_metadata,
        "readback_rows": readback_rows,
        "rows_by_table": native_rows,
        "coverage_receipt_refs": COVERAGE_REFS,
    }


def build(**changes):
    values = inputs()
    values.update(changes)
    return subject.build_production_snapshot(**values)


def reseal(value):
    value = copy.deepcopy(value)
    snapshot = value["snapshot"]
    snapshot.pop("source_digest", None)
    snapshot["source_digest"] = _digest(snapshot)
    value.pop("assembly_digest", None)
    value["assembly_digest"] = _digest(
        {"domain": "open_intelligence_production_snapshot_assembly_v1", "value": value}
    )
    return value


def test_builds_serializable_structural_snapshot_without_granting_authority():
    result = build()

    assert list(result) == [
        "assembly_contract_version",
        "snapshot",
        "source_metadata_utf8",
        "snapshot_plan",
        "snapshot_metadata",
        "readback_rows",
        "row_material",
        "collection_complete",
        "source_authority",
        "external_authority_required",
        "assembly_digest",
    ]
    snapshot = result["snapshot"]
    assert list(snapshot) == [
        "snapshot_contract_version",
        "snapshot_id",
        "cutoff_date",
        "source_as_of",
        "captured_at",
        "client_scope_id",
        "market_scope",
        "source_relation_set_id",
        "row_counts_by_table",
        "section_counts",
        "section_identity_sets",
        "table_digests",
        "rows_by_table",
        "table_snapshot_receipts",
        "physical_capture_complete",
        "coverage_receipt_refs",
        "coverage_limitations",
        "source_digest",
    ]
    assert snapshot["snapshot_contract_version"] == (
        "open_intelligence_source_snapshot_v3_production_v1"
    )
    assert snapshot["source_as_of"] == "2030-01-03T00:00:00+00:00"
    assert snapshot["captured_at"] == "2030-01-03T00:05:00+00:00"
    assert snapshot["market_scope"] == ["ng", "za"]
    assert snapshot["physical_capture_complete"] is False
    assert result["collection_complete"] is False
    assert result["source_authority"] is False
    assert result["external_authority_required"] == [
        "normalized_row_query_execution_proof_required",
        "snapshot_creation_execution_proof_required",
        "snapshot_storage_execution_proof_required",
    ]
    unsigned = dict(snapshot)
    unsigned.pop("source_digest")
    assert snapshot["source_digest"] == _digest(unsigned)
    assert json.loads(json.dumps(result)) == result


def test_rows_counts_identities_and_receipts_derive_from_validated_helpers():
    result = build()
    snapshot = result["snapshot"]
    normalized = row_fixture.normalize(row_fixture.rows())

    assert snapshot["rows_by_table"] == normalized["adapted_rows_by_table"]
    assert snapshot["table_digests"] == normalized["adapted_table_digests"]
    assert snapshot["row_counts_by_table"] == {
        lane: len(rows) for lane, rows in normalized["adapted_rows_by_table"].items()
    }
    assert snapshot["section_counts"] == snapshot["row_counts_by_table"]
    assert snapshot["section_identity_sets"]["event_ledger"] == [
        "00000000:" + _digest(normalized["adapted_rows_by_table"]["event_ledger"][0])
    ]
    assert [receipt["lane"] for receipt in snapshot["table_snapshot_receipts"]] == list(
        subject.LANES
    )
    assert all(
        receipt["execution_authority"] is False for receipt in snapshot["table_snapshot_receipts"]
    )
    assert snapshot["coverage_receipt_refs"] == list(COVERAGE_REFS)
    assert "upstream_collection_completeness_unproven" in snapshot["coverage_limitations"]
    assert result["row_material"]["collection_complete"] is False


def test_selected_physical_rows_cannot_exceed_verified_full_snapshot_rows():
    plan = table_fixture.build_plan()
    native_rows = row_fixture.rows()
    selected_physical_row_count = len(native_rows["event_ledger"])
    verified_full_snapshot_row_count = 0
    assert selected_physical_row_count > verified_full_snapshot_row_count

    with pytest.raises(ValueError, match="production_snapshot_invalid"):
        subject.build_production_snapshot(
            cutoff_date=row_fixture.CUTOFF,
            captured_at=row_fixture.CAPTURED_AT,
            client_scope_id=CLIENT_SCOPE_ID,
            market_scope=row_fixture.MARKETS,
            reviewed_metadata=table_fixture.source_metadata_bytes(),
            plan=plan,
            snapshot_metadata=table_fixture.snapshot_metadata(plan),
            readback_rows=table_fixture.readback(plan),
            rows_by_table=native_rows,
            coverage_receipt_refs=COVERAGE_REFS,
        )


def test_prepared_validation_reconstructs_inputs_and_returns_a_detached_copy():
    value = build()
    result = subject.validate_prepared_production_snapshot(
        value,
        cutoff_date=row_fixture.CUTOFF,
        client_scope_id=CLIENT_SCOPE_ID,
        market_scope=row_fixture.MARKETS,
    )
    assert result == value
    result["snapshot"]["market_scope"].append("ke")
    assert value["snapshot"]["market_scope"] == ["ng", "za"]


def test_canonical_persistence_roundtrip_rebuilds_logical_row_field_order():
    value = build()
    persisted = json.loads(canonical_bytes(value))

    result = subject.validate_prepared_production_snapshot(
        persisted,
        cutoff_date=row_fixture.CUTOFF,
        client_scope_id=CLIENT_SCOPE_ID,
        market_scope=row_fixture.MARKETS,
    )

    assert result == value
    assert result["snapshot"]["source_digest"] == value["snapshot"]["source_digest"]
    assert result["assembly_digest"] == value["assembly_digest"]
    for lane in subject.LANES:
        for actual, expected in zip(
            result["snapshot"]["rows_by_table"][lane],
            value["snapshot"]["rows_by_table"][lane],
            strict=True,
        ):
            assert tuple(actual) == tuple(expected)


@pytest.mark.parametrize(
    "mutation",
    [
        "adapted_row",
        "physical_row",
        "row_count",
        "section_identity",
        "table_digest",
        "table_receipt",
        "captured_at",
        "client_scope",
        "market_scope",
        "physical_complete",
        "collection_complete",
        "source_authority",
        "extra_field",
    ],
)
def test_resealed_snapshot_or_structural_material_tampering_refuses(mutation):
    value = build()
    if mutation == "adapted_row":
        value["snapshot"]["rows_by_table"]["enriched_content"][0]["text"] = "changed"
    elif mutation == "physical_row":
        value["row_material"]["physical_rows_by_table"]["raw_content"][0]["text"] = "changed"
    elif mutation == "row_count":
        value["snapshot"]["row_counts_by_table"]["event_ledger"] += 1
    elif mutation == "section_identity":
        value["snapshot"]["section_identity_sets"]["event_ledger"][0] = "00000000:" + "a" * 64
    elif mutation == "table_digest":
        value["snapshot"]["table_digests"]["seed_graph"] = "a" * 64
    elif mutation == "table_receipt":
        value["snapshot"]["table_snapshot_receipts"][0]["row_count"] += 1
    elif mutation == "captured_at":
        value["snapshot"]["captured_at"] = "2030-01-03T00:06:00+00:00"
    elif mutation == "client_scope":
        value["snapshot"]["client_scope_id"] = "foreign"
    elif mutation == "market_scope":
        value["snapshot"]["market_scope"] = ["za"]
    elif mutation == "physical_complete":
        value["snapshot"]["physical_capture_complete"] = True
    elif mutation == "collection_complete":
        value["collection_complete"] = True
    elif mutation == "source_authority":
        value["source_authority"] = True
    else:
        value["snapshot"]["unexpected"] = None

    with pytest.raises(ValueError, match="production_snapshot_invalid"):
        subject.validate_prepared_production_snapshot(
            reseal(value),
            cutoff_date=row_fixture.CUTOFF,
            client_scope_id=CLIENT_SCOPE_ID,
            market_scope=row_fixture.MARKETS,
        )


def test_changed_original_rows_or_reviewed_metadata_refuses_full_validation():
    value = build()
    changed_rows = row_fixture.rows()
    changed_rows["raw_content"][0]["text"] = "changed"
    changed_metadata = json.loads(table_fixture.source_metadata_bytes())
    changed_metadata["tables"]["trends_v2_dev.event_ledger"]["etag"] = "changed"

    for changes in (
        {"rows_by_table": changed_rows},
        {"reviewed_metadata": json.dumps(changed_metadata).encode("utf-8")},
    ):
        with pytest.raises(ValueError, match="production_snapshot_invalid"):
            subject.validate_production_snapshot(value, **{**inputs(), **changes})


@pytest.mark.parametrize(
    "refs",
    [
        ({"kind": "unknown", "receipt_id": "x", "receipt_digest": "1" * 64},),
        ({"kind": "source_window", "receipt_id": "x", "receipt_digest": "bad"},),
        (
            {"kind": "source_window", "receipt_id": "x", "receipt_digest": "1" * 64},
            {"kind": "source_window", "receipt_id": "x", "receipt_digest": "1" * 64},
        ),
        tuple(reversed(COVERAGE_REFS)),
    ],
)
def test_invalid_duplicate_or_unsorted_coverage_references_refuse(refs):
    with pytest.raises(ValueError, match="production_snapshot_invalid"):
        build(coverage_receipt_refs=refs)


def test_old_nine_field_snapshot_is_not_relabelled_as_production():
    old = {
        "fixture_scope": "synthetic_test_only",
        "snapshot_id": "legacy",
        "captured_at": "2030-01-02T12:00:00+00:00",
        "row_counts_by_table": {},
        "section_counts": {},
        "section_identity_sets": {},
        "table_digests": {},
        "rows_by_table": {},
        "source_digest": "0" * 64,
    }
    with pytest.raises(ValueError, match="production_snapshot_invalid"):
        subject.validate_prepared_production_snapshot(
            old,
            cutoff_date=row_fixture.CUTOFF,
            client_scope_id=CLIENT_SCOPE_ID,
            market_scope=row_fixture.MARKETS,
        )


def test_expected_context_must_match_embedded_cutoff_scope_and_market():
    value = build()
    cases = (
        {"cutoff_date": row_fixture.CUTOFF.replace(day=1)},
        {"client_scope_id": "foreign"},
        {"market_scope": ("za",)},
    )
    for changes in cases:
        kwargs = {
            "cutoff_date": row_fixture.CUTOFF,
            "client_scope_id": CLIENT_SCOPE_ID,
            "market_scope": row_fixture.MARKETS,
            **changes,
        }
        with pytest.raises(ValueError, match="production_snapshot_invalid"):
            subject.validate_prepared_production_snapshot(value, **kwargs)


def test_captured_at_must_be_actual_aware_time_at_or_after_cutoff_close():
    for captured_at in (
        datetime(2030, 1, 2, 23, 59, tzinfo=UTC),
        datetime(2030, 1, 3, 0, 5),
        True,
    ):
        with pytest.raises(ValueError, match="production_snapshot_invalid"):
            build(captured_at=captured_at)
    assert build(captured_at=row_fixture.SOURCE_AS_OF)["snapshot"]["captured_at"] == (
        "2030-01-03T00:00:00+00:00"
    )


def test_embedded_metadata_plan_and_native_readback_are_revalidated():
    value = build()
    cases = []
    metadata = copy.deepcopy(value)
    parsed = json.loads(metadata["source_metadata_utf8"])
    parsed["tables"]["trends_v2_dev.event_ledger"]["etag"] = "changed"
    metadata["source_metadata_utf8"] = json.dumps(parsed)
    cases.append(metadata)
    plan = copy.deepcopy(value)
    plan["snapshot_plan"]["plan_digest"] = "a" * 64
    cases.append(plan)
    native = copy.deepcopy(value)
    native["snapshot_metadata"]["event_ledger"]["numRows"] = "2"
    cases.append(native)
    readback = copy.deepcopy(value)
    readback["readback_rows"][0]["row_count"] = 2
    cases.append(readback)

    for changed in cases:
        with pytest.raises(ValueError, match="production_snapshot_invalid"):
            subject.validate_prepared_production_snapshot(
                reseal(changed),
                cutoff_date=row_fixture.CUTOFF,
                client_scope_id=CLIENT_SCOPE_ID,
                market_scope=row_fixture.MARKETS,
            )
