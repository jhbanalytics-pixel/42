"""The v2 source snapshot capture result reader and the registry entry derived from it."""

import copy
import hashlib
import json
from dataclasses import fields
from datetime import UTC, date, datetime, timedelta

import pytest
from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence import production_snapshot_tables as tables
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.staging_source_profile import SOURCE_LANES

from tests.unit.test_execution_manifest_origins import (
    AMENDMENT_E_DAILY_CONTRACT,
    AMENDMENT_E_REGISTRY_PATH,
    load_registry,
)
from tests.unit.test_execution_manifest_origins import manifest as v2_manifest
from tests.unit.test_execution_records_v2 import RESOURCE_SHA, rebuild
from tests.unit.test_execution_records_v2 import chain as v2_chain
from tests.unit.test_execution_runtime_v2 import AMENDMENT_E_CAPTURE_ARTIFACTS
from tests.unit.test_retained_readers_v2 import FakeCatalogue, v2_rows

CUTOFF = date(2026, 9, 13)
WINDOW_END = datetime(2026, 9, 14, tzinfo=UTC)
SNAPSHOT_AS_OF = WINDOW_END + timedelta(minutes=30)
APPROVED_AT = WINDOW_END + timedelta(minutes=35)
CONSUMED_AT = WINDOW_END + timedelta(minutes=40)
CAPTURED_AT = WINDOW_END + timedelta(minutes=45)
COMPLETED_AT = WINDOW_END + timedelta(minutes=50)
CODE = "capture_result_v2_test"


def destination(lane, cutoff=CUTOFF):
    return f"ogilvy-trends-v2.intelligence_42_sources_staging.staging_source_{cutoff:%Y%m%d}_{lane}"


def payload(**overrides):
    value = {
        "contract_version": "open_intelligence_protected_source_snapshot_v2",
        "cutoff_date": CUTOFF.isoformat(),
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ke", "ng", "za"],
        "profile_id": f"staging-{CUTOFF.isoformat()}-{'a' * 16}",
        "profile_version": "42_staging_source_v2",
        "projection_version": "native_id_bound_v1",
        "source_dataset": "intelligence_42_sources_staging",
        "observation_window_end": WINDOW_END.isoformat(),
        "snapshot_as_of": SNAPSHOT_AS_OF.isoformat(),
        "source_estate_digest": "7" * 64,
        "grant_id": "source_capture_grant_2026_09_v2",
        "captured_at": CAPTURED_AT.isoformat(),
        "snapshot_plan_digest": "5" * 64,
        "snapshot_digest": "6" * 64,
        "capture_receipt_digest": "8" * 64,
        "creation_records": [
            {
                "destination": destination(lane),
                "job_id": f"oi_v2_snapshot_{lane}",
                "lane": lane,
                "native_job_digest": "9" * 64,
                "state": "succeeded",
            }
            for lane in SOURCE_LANES
        ],
        "artifact_attempt": {"uri": "gs://example/capture.json"},
        "stored_artifact": {"uri": "gs://example/capture.json"},
        "query_count": 4,
        "total_bytes_billed": 150994944,
        "limitations": ["upstream_collection_completeness_unproven"],
        "missing_checks": [],
    }
    value.update(overrides)
    return value


def reseal(result, registry, value, **changes):
    raw = canonical_bytes(value).decode() if not isinstance(value, str) else value
    digest = hashlib.sha256(raw.encode()).hexdigest()
    values = {
        "canonical_result_json": raw,
        "result_digest": digest,
        **changes,
    }
    values["result_id"] = execution_approval.result_id_v2(
        result.consumption_id,
        values.get("result_reference", result.result_reference),
        digest,
        values.get("status", result.status),
        result.completed_at,
        origin_registry_sha256=result.origin_registry_sha256,
        resource_manifest_sha256=result.resource_manifest_sha256,
    )
    return rebuild(result, registry, **values)


def amendment_e_capture_manifest():
    """The v2 plan capture this reader derives from: the amendment e daily policy.

    The bridge registry's capture reads through the bridge ledger instead.
    """
    payload = v2_manifest("source_snapshot_capture")
    payload["contract_sha256"] = AMENDMENT_E_DAILY_CONTRACT
    payload["input_artifacts"] = [
        {"name": name, "sha256": "d" * 64} for name in AMENDMENT_E_CAPTURE_ARTIFACTS
    ]
    return payload


def capture_chain(value=None, **changes):
    registry, approval, consumption, result = v2_chain(
        "source_snapshot_capture",
        approved_at=APPROVED_AT,
        consumed_at=CONSUMED_AT,
        completed_at=COMPLETED_AT,
        registry=load_registry(AMENDMENT_E_REGISTRY_PATH),
        payload=amendment_e_capture_manifest(),
    )
    changes.setdefault("result_reference", result.execution_name + "#source-snapshot")
    result = reseal(result, registry, payload() if value is None else value, **changes)
    return (
        registry,
        approval,
        consumption,
        result,
        FakeCatalogue(registry, (registry.sha256, RESOURCE_SHA)),
    )


def row(result):
    return {field.name: getattr(result, field.name) for field in fields(result)}


def read(result, catalogue, cutoff=CUTOFF):
    return tables.retained_v2_capture_result(
        row(result), CODE, cutoff=cutoff, generation_loader=catalogue
    )


def test_completed_v2_capture_reads_under_its_reviewed_generation():
    _registry, _approval, _consumption, result, catalogue = capture_chain()
    checked, value = read(result, catalogue)
    assert type(checked) is execution_approval.ExecutionResultV2
    assert checked.result_id == result.result_id
    assert checked.result_digest == hashlib.sha256(canonical_bytes(value)).hexdigest()
    assert value["cutoff_date"] == CUTOFF.isoformat()
    assert catalogue.calls == [(result.origin_registry_sha256, RESOURCE_SHA)]
    text = row(result)
    text["completed_at"] = result.completed_at.isoformat()
    again, _ = tables.retained_v2_capture_result(
        text, CODE, cutoff=CUTOFF, generation_loader=catalogue
    )
    assert again == checked


def test_unreviewed_generation_refuses_before_the_record_is_typed():
    registry, _approval, _consumption, result, _catalogue = capture_chain()
    stranger = FakeCatalogue(registry, (registry.sha256, "f" * 64))
    with pytest.raises(ValueError, match=f"^{CODE}$"):
        read(result, stranger)


def test_reader_selects_historical_replay_and_takes_no_mode():
    import inspect

    assert "mode" not in inspect.signature(tables.retained_v2_capture_result).parameters
    _registry, _approval, _consumption, result, catalogue = capture_chain()
    with pytest.raises(TypeError):
        tables.retained_v2_capture_result(
            row(result), CODE, cutoff=CUTOFF, mode="new_consume", generation_loader=catalogue
        )


def test_pinned_cutoff_is_the_callers_not_the_payloads():
    _registry, _approval, _consumption, result, catalogue = capture_chain()
    with pytest.raises(ValueError, match=f"^{CODE}$"):
        read(result, catalogue, cutoff=CUTOFF + timedelta(days=1))
    with pytest.raises(ValueError, match=f"^{CODE}$"):
        read(result, catalogue, cutoff=CUTOFF.isoformat())


def test_digest_is_recomputed_not_read():
    _registry, _approval, _consumption, result, catalogue = capture_chain()
    value = row(result)
    value["canonical_result_json"] = canonical_bytes(payload(query_count=5)).decode()
    with pytest.raises(ValueError, match=f"^{CODE}$"):
        tables.retained_v2_capture_result(value, CODE, cutoff=CUTOFF, generation_loader=catalogue)


def lane_defect(kind):
    records = copy.deepcopy(payload()["creation_records"])
    if kind == "state":
        records[0]["state"] = "failed"
    elif kind == "destination":
        records[0]["destination"] = records[0]["destination"].replace(
            "intelligence_42_sources_staging", "trends_v2_staging"
        )
    elif kind == "order":
        records.reverse()
    elif kind == "missing":
        records.pop()
    else:
        records[0] = json.dumps(records[0])
    return payload(creation_records=records)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(
            payload(
                captured_at=None,
                snapshot_digest=None,
                capture_receipt_digest=None,
                artifact_attempt=None,
                stored_artifact=None,
                query_count=0,
            ),
            id="never_captured",
        ),
        pytest.param(payload(missing_checks=["streaming_buffer"]), id="missing_checks"),
        pytest.param(payload(stored_artifact=None), id="unstored"),
        pytest.param(payload(total_bytes_billed=None), id="unmetered"),
        pytest.param(
            payload(captured_at=COMPLETED_AT.isoformat().replace("00:50:00", "00:55:00")),
            id="captured_after_completion",
        ),
        pytest.param(
            payload(contract_version="open_intelligence_protected_source_snapshot_v1"),
            id="v1_payload",
        ),
        pytest.param(lane_defect("state"), id="failed_lane"),
        pytest.param(lane_defect("destination"), id="foreign_destination"),
        pytest.param(lane_defect("order"), id="lane_order"),
        pytest.param(lane_defect("missing"), id="missing_lane"),
        pytest.param(lane_defect("serialized"), id="serialized_record"),
    ],
)
def test_incomplete_or_foreign_capture_payload_refuses(value):
    _registry, _approval, _consumption, result, catalogue = capture_chain(value)
    with pytest.raises(ValueError, match=f"^{CODE}$"):
        read(result, catalogue)


@pytest.mark.parametrize("change", ["failed", "reference"])
def test_resealed_failed_or_misreferenced_result_refuses(change):
    registry, _approval, _consumption, result, catalogue = capture_chain()
    changes = (
        {"status": "failed"}
        if change == "failed"
        else {"result_reference": result.execution_name + "#unrelated"}
    )
    changed = reseal(result, registry, result.canonical_result_json, **changes)
    with pytest.raises(ValueError, match=f"^{CODE}$"):
        read(changed, catalogue)


def test_foreign_operation_and_v1_rows_refuse():
    registry, _approval, _consumption, result = v2_chain("r3_apply")
    catalogue = FakeCatalogue(registry, (registry.sha256, RESOURCE_SHA))
    foreign = reseal(
        result,
        registry,
        payload(),
        result_reference=result.execution_name + "#source-snapshot",
    )
    with pytest.raises(ValueError, match=f"^{CODE}$"):
        read(foreign, catalogue)
    from tests.unit.test_retained_readers_v2 import v1_source_snapshot_chain

    _v1_approval, _v1_consumption, v1_result = v1_source_snapshot_chain()
    with pytest.raises(ValueError, match=f"^{CODE}$"):
        tables.retained_v2_capture_result(
            dict(v1_result),
            CODE,
            cutoff=CUTOFF,
            generation_loader=catalogue,
        )


def test_reader_has_no_default_cutoff():
    _registry, _approval, _consumption, result, _catalogue = capture_chain()
    with pytest.raises(TypeError):
        tables.retained_v2_capture_result(row(result), CODE)


# Registry entry derived from the v2 ledger chain


def chain_receipt(value=None, **changes):
    _registry, approval, consumption, result, catalogue = capture_chain(value, **changes)
    return v2_rows(approval, consumption, result)[0], result, catalogue


def test_renderer_derives_a_v2_entry_from_the_ledger_chain():
    from scripts.staging.render_protected_context_entry import render_entry

    receipt, result, catalogue = chain_receipt()
    entry = json.loads(render_entry(receipt, generation_loader=catalogue))
    assert entry == {
        "consumption_id": result.consumption_id,
        "cutoff_date": "2026-09-13",
        "manifest_sha256": result.manifest_sha256,
        "market_scope": ["ke", "ng", "za"],
        "profile_id": "protected_context_20260913_v2",
        "result_contract_version": "open_intelligence_execution_result_v2",
        "result_digest": result.result_digest,
        "result_id": result.result_id,
        "snapshot_tables": [destination(lane) for lane in SOURCE_LANES],
        "source_as_of": "2026-09-14T00:00:00+00:00",
    }
    assert render_entry(receipt, generation_loader=catalogue).encode() == canonical_bytes(entry)


def test_renderer_cutoff_comes_from_the_approved_manifest(monkeypatch):
    from scripts.staging.render_protected_context_entry import render_entry

    later = CUTOFF + timedelta(days=1)
    value = payload(
        cutoff_date=later.isoformat(),
        profile_id=f"staging-{later.isoformat()}-{'a' * 16}",
        observation_window_end=(WINDOW_END + timedelta(days=1)).isoformat(),
        snapshot_as_of=(SNAPSHOT_AS_OF + timedelta(days=1)).isoformat(),
        captured_at=(CAPTURED_AT + timedelta(days=1)).isoformat(),
        creation_records=[
            {**record, "destination": destination(record["lane"], later)}
            for record in payload()["creation_records"]
        ],
    )
    receipt, _result, catalogue = chain_receipt(value)
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        render_entry(receipt, generation_loader=catalogue)


@pytest.mark.parametrize("defect", ["count", "result_json", "generation", "incomplete"])
def test_renderer_refuses_an_unproven_chain(defect):
    from scripts.staging.render_protected_context_entry import render_entry

    if defect == "incomplete":
        receipt, _result, catalogue = chain_receipt(payload(missing_checks=["unproven"]))
    else:
        receipt, _result, catalogue = chain_receipt()
    if defect == "count":
        receipt["result_count"] = 2
    elif defect == "result_json":
        value = json.loads(receipt["result_json"])
        value["result_digest"] = "f" * 64
        receipt["result_json"] = json.dumps(value)
    elif defect == "generation":
        catalogue = FakeCatalogue(catalogue.registry, (catalogue.registry.sha256, "f" * 64))
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        render_entry(receipt, generation_loader=catalogue)


def test_renderer_accepts_the_bq_json_rendering_of_the_chain_row():
    from scripts.staging.render_protected_context_entry import render_entry

    receipt, _result, catalogue = chain_receipt()
    exported = [
        {key: str(value) if key.endswith("_count") else value for key, value in receipt.items()}
    ]
    assert render_entry(exported, generation_loader=catalogue) == render_entry(
        receipt, generation_loader=catalogue
    )
    exported[0]["result_count"] = "01"
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        render_entry(exported, generation_loader=catalogue)


def test_merged_registry_is_canonical_ordered_and_loadable(monkeypatch, tmp_path):
    from scripts.staging.render_protected_context_entry import render_registry
    from src.analysis.open_intelligence import protected_context_registry as registry

    from tests.unit.test_protected_context_registry import fixture_document, local

    receipt, result, catalogue = chain_receipt()
    # Merged into the committed rows a v2 capture can sit beside: no bridge row is newer.
    (tmp_path / "base").mkdir()
    local(monkeypatch, tmp_path / "base", fixture_document())
    before = registry.REGISTRY_PATH.read_bytes()
    merged = render_registry(receipt, before, generation_loader=catalogue)
    document = json.loads(merged)
    assert merged == canonical_bytes(document)
    assert [entry["cutoff_date"] for entry in document["entries"]] == [
        "2026-09-07",
        "2026-09-08",
        "2026-09-13",
    ]
    assert document["entries"][:2] == json.loads(before)["entries"]
    assert registry.REGISTRY_PATH.read_bytes() == before
    path = tmp_path / "registry.json"
    path.write_bytes(merged)
    monkeypatch.setattr(registry, "REGISTRY_PATH", path)
    entry = registry.profile_for("2026-09-13")
    assert entry.result_id == result.result_id
    assert entry.result_contract_version == "open_intelligence_execution_result_v2"
    assert registry.allowed_cutoffs() == ("2026-09-07",)
    assert render_registry(receipt, merged, generation_loader=catalogue) == merged
    changed = json.loads(merged)
    changed["entries"][2]["result_digest"] = "0" * 64
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        render_registry(receipt, canonical_bytes(changed), generation_loader=catalogue)
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        render_registry(
            receipt, json.dumps(json.loads(before), indent=1).encode(), generation_loader=catalogue
        )


def test_merged_registry_refuses_to_overwrite_a_v1_cutoff():
    from scripts.staging.render_protected_context_entry import render_registry
    from src.analysis.open_intelligence import protected_context_registry as registry

    document = json.loads(registry.REGISTRY_PATH.read_bytes())
    document["entries"][1]["cutoff_date"] = "2026-09-13"
    document["entries"][1]["profile_id"] = "protected_context_20260913_v1"
    document["entries"][1]["source_as_of"] = "2026-09-14T00:00:00+00:00"
    document["entries"][1]["snapshot_tables"] = [
        name.replace("20260908", "20260913") for name in document["entries"][1]["snapshot_tables"]
    ]
    receipt, _result, catalogue = chain_receipt()
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        render_registry(receipt, canonical_bytes(document), generation_loader=catalogue)


def test_v1_receipt_merges_into_the_registry_with_the_same_bytes():
    from scripts.staging.render_protected_context_entry import render_registry
    from src.analysis.open_intelligence import protected_context_registry as registry

    from tests.unit.test_protected_context_registry import retained_result_row

    before = registry.REGISTRY_PATH.read_bytes()
    assert render_registry(retained_result_row(), before) == before


def test_query_printed_for_the_operator_is_the_read_only_v2_chain_statement():
    from scripts.staging.render_protected_context_entry import chain_query
    from src.analysis.open_intelligence import general_question_context_queries as queries

    text = chain_query("exc_" + "1" * 64)
    assert text["sql"] == queries._RESULT_SQL_V2
    assert text["parameters"] == [
        {
            "name": "consumption_id",
            "parameterType": {"type": "STRING"},
            "parameterValue": {"value": "exc_" + "1" * 64},
        }
    ]
    assert "INSERT" not in text["sql"].upper()
    assert "MERGE" not in text["sql"].upper()
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        chain_query("exc_" + "1" * 63)
    assert canonical_digest(text) == canonical_digest(chain_query("exc_" + "1" * 64))


def test_renderer_cli_reads_inputs_and_prints_the_merged_registry(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    from src.analysis.open_intelligence import protected_context_registry as registry

    from tests.unit.test_protected_context_registry import retained_result_row

    script = (
        Path(__file__).resolve().parents[2] / "scripts/staging/render_protected_context_entry.py"
    )
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(canonical_bytes(retained_result_row()))
    before = registry.REGISTRY_PATH.read_bytes()
    merged = subprocess.run(
        [sys.executable, str(script), str(receipt), "--registry", str(registry.REGISTRY_PATH)],
        capture_output=True,
        check=True,
    )
    assert merged.stdout == before
    assert registry.REGISTRY_PATH.read_bytes() == before
    query = subprocess.run(
        [sys.executable, str(script), "--chain-query", "exc_" + "1" * 64],
        capture_output=True,
        check=True,
    )
    from scripts.staging.render_protected_context_entry import chain_query

    assert json.loads(query.stdout) == chain_query("exc_" + "1" * 64)
    refused = subprocess.run(
        [sys.executable, str(script), str(receipt), "--registry"], capture_output=True
    )
    assert refused.returncode != 0
    assert b"capture_receipt_invalid" in refused.stderr


def test_merging_holds_beside_a_committed_bridge_row(monkeypatch, tmp_path):
    from tests.unit.test_protected_context_registry import rehearse_pin

    rehearse_pin(monkeypatch, tmp_path)
    test_merged_registry_is_canonical_ordered_and_loadable(monkeypatch, tmp_path)
