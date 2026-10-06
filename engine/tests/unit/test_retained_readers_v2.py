import ast
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import (
    execution_approval,
    execution_origins,
)
from src.analysis.open_intelligence import (
    general_question_context_queries as queries,
)
from src.analysis.open_intelligence import (
    general_question_release_admission as release_admission,
)
from src.analysis.open_intelligence import (
    general_question_release_material as release_material,
)
from src.analysis.open_intelligence import (
    production_snapshot_tables as tables,
)
from src.analysis.open_intelligence.brain_contract import canonical_bytes

from tests.unit.test_execution_records_v2 import RESOURCE_SHA
from tests.unit.test_execution_records_v2 import chain as v2_chain
from tests.unit.test_open_intelligence_execution_approval_contract import (
    APPROVED_BY,
    source_snapshot_manifest,
)

ENGINE_ROOT = Path(__file__).resolve().parents[2]
MODULES = (
    "general_question_release_admission",
    "general_question_context_queries",
    "general_question_snapshot",
    "production_snapshot_capture",
    "general_question_context_admission",
    "production_snapshot_tables",
    "general_question_release_material",
)
OLD_APPLY_JOB = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-apply-staging"
)
NEW_APPLY_JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-apply-staging"
SOURCE_SNAPSHOT_AT = datetime(2026, 9, 9, 12, tzinfo=UTC)
V1_CODE = "retained_reader_test"


def registry():
    return tables.retained_origin_registry()


def encode(record):
    return canonical_bytes(asdict(record)).decode()


def v1_source_snapshot_chain(*, job_resource=None, status="succeeded"):
    payload = source_snapshot_manifest()
    selected = registry()
    manifest_json = execution_approval.canonical_manifest_bytes(
        payload, mode="historical_read", registry=selected
    ).decode()
    digest = hashlib.sha256(manifest_json.encode()).hexdigest()
    expires_at = datetime(2026, 9, 9, 23, 59, 59, tzinfo=UTC)
    phrase = (
        "I approve one staging execution of source_snapshot_capture for manifest SHA256 "
        f"{digest}. Production remains unchanged."
    )
    approval = execution_approval.ExecutionApproval(
        approval_contract_version="open_intelligence_execution_approval_v1",
        approval_id=execution_approval.approval_id(digest, APPROVED_BY, SOURCE_SNAPSHOT_AT),
        manifest_version="open_intelligence_execution_manifest_v1",
        operation="source_snapshot_capture",
        contract_sha256=payload["contract_sha256"],
        manifest_sha256=digest,
        canonical_manifest_json=manifest_json,
        approved_by=APPROVED_BY,
        approved_at=SOURCE_SNAPSHOT_AT,
        expires_at=expires_at,
        approval_phrase_sha256=hashlib.sha256(phrase.encode()).hexdigest(),
    )
    job = payload["job_resource"] if job_resource is None else job_resource
    execution_name = job + "/executions/retained-reader"
    consumed_at = SOURCE_SNAPSHOT_AT + timedelta(minutes=1)
    consumption = {
        "consumption_contract_version": "open_intelligence_execution_consumption_v1",
        "consumption_id": execution_approval.consumption_id(
            approval.approval_id, execution_name, consumed_at
        ),
        "approval_id": approval.approval_id,
        "manifest_sha256": digest,
        "operation": "source_snapshot_capture",
        "execution_name": execution_name,
        "job_resource": job,
        "source_sha": payload["source_sha"],
        "image_uri": payload["image_uri"],
        "consumed_at": consumed_at,
    }
    result_json = canonical_bytes(
        {
            "contract_version": "open_intelligence_protected_source_snapshot_v1",
            "cutoff_date": "2026-09-07",
        }
    ).decode()
    result_digest = hashlib.sha256(result_json.encode()).hexdigest()
    completed_at = consumed_at + timedelta(seconds=1)
    reference = execution_name + "#source-snapshot"
    result = {
        "result_contract_version": "open_intelligence_execution_result_v1",
        "result_id": execution_approval.result_id(
            consumption["consumption_id"], reference, result_digest, status, completed_at
        ),
        "consumption_id": consumption["consumption_id"],
        "approval_id": approval.approval_id,
        "manifest_sha256": digest,
        "operation": "source_snapshot_capture",
        "execution_name": execution_name,
        "result_reference": reference,
        "canonical_result_json": result_json,
        "result_digest": result_digest,
        "status": status,
        "completed_at": completed_at,
    }
    return approval, consumption, result


def v1_rows(approval, consumption, result):
    return [
        {
            "approval_count": 1,
            "consumption_count": 1,
            "result_count": 1,
            "approval_json": encode(approval),
            "consumption_json": canonical_bytes(consumption).decode(),
            "result_json": canonical_bytes(result).decode(),
        }
    ]


def v2_rows(approval, consumption, result, **counts):
    row = {
        "approval_count": 1,
        "consumption_count": 1,
        "result_count": 1,
        "approval_json": encode(approval),
        "consumption_json": encode(consumption),
        "result_json": encode(result),
    }
    row.update(counts)
    return [row]


class FakeCatalogue:
    def __init__(self, registry_value, pair):
        self.registry = registry_value
        self.pair = pair
        self.calls = []

    def __call__(self, origin_registry_sha256, resource_manifest_sha256):
        self.calls.append((origin_registry_sha256, resource_manifest_sha256))
        if (origin_registry_sha256, resource_manifest_sha256) != self.pair:
            raise execution_origins.OriginRefusal("execution_generation_unknown")
        return SimpleNamespace(registry=self.registry)


def v2_fixture():
    selected, approval, consumption, result = v2_chain("r3_apply")
    catalogue = FakeCatalogue(selected, (selected.sha256, RESOURCE_SHA))
    return selected, approval, consumption, result, catalogue


def rewrite(record_json, **changes):
    value = json.loads(record_json)
    value.update(changes)
    return json.dumps(value)


def test_retained_registry_is_the_packaged_sealed_registry_with_pinned_digest():
    selected = registry()
    assert type(selected) is execution_origins.OriginRegistry
    assert selected.sha256 == tables.RETAINED_ORIGIN_REGISTRY_SHA256
    assert (
        hashlib.sha256(tables.RETAINED_ORIGIN_REGISTRY_PATH.read_bytes()).hexdigest()
        == "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179"
    )
    assert tables.HISTORICAL_MODES == ("historical_read", "historical_replay")


@pytest.mark.parametrize("mode", ["historical_read", "historical_replay"])
def test_retained_v1_result_reconstructs_under_each_historical_mode(mode):
    _approval, _consumption, result = v1_source_snapshot_chain()
    reconstructed = tables.retained_v1_result(result, V1_CODE, mode=mode, registry=registry())
    assert reconstructed.result_id == result["result_id"]
    assert reconstructed.execution_name == result["execution_name"]


@pytest.mark.parametrize("mode", ["new_consume", "new_approval", None, "historical"])
def test_retained_v1_readers_refuse_every_non_historical_mode(mode):
    approval, _consumption, result = v1_source_snapshot_chain()
    with pytest.raises(ValueError):
        tables.retained_v1_result(result, V1_CODE, mode=mode, registry=registry())
    with pytest.raises(ValueError):
        tables.retained_v1_approval(approval, V1_CODE, mode=mode, registry=registry())


def test_retained_v1_readers_refuse_a_registry_that_is_not_the_sealed_type():
    approval, _consumption, result = v1_source_snapshot_chain()
    for fake in (None, {}, SimpleNamespace(sha256=tables.RETAINED_ORIGIN_REGISTRY_SHA256)):
        with pytest.raises(ValueError):
            tables.retained_v1_result(result, V1_CODE, mode="historical_replay", registry=fake)
        with pytest.raises(ValueError):
            tables.retained_v1_approval(approval, V1_CODE, mode="historical_read", registry=fake)


def test_retained_v1_readers_have_no_default_mode_or_registry():
    _approval, _consumption, result = v1_source_snapshot_chain()
    with pytest.raises(TypeError):
        tables.retained_v1_result(result, V1_CODE)
    with pytest.raises(TypeError):
        tables.retained_v1_result(result, V1_CODE, mode="historical_read")


def test_retained_v1_approval_returns_the_manifest_validated_against_the_selected_registry():
    approval, _consumption, _result = v1_source_snapshot_chain()
    revalidated, manifest = tables.retained_v1_approval(
        approval, V1_CODE, mode="historical_read", registry=registry()
    )
    assert revalidated == approval
    assert manifest.operation == "source_snapshot_capture"
    assert manifest.job_resource == json.loads(approval.canonical_manifest_json)["job_resource"]


def test_new_job_under_v1_refuses_in_the_result_reader_and_the_context_decoder():
    approval, consumption, result = v1_source_snapshot_chain(job_resource=NEW_APPLY_JOB)
    with pytest.raises(ValueError):
        tables.retained_v1_result(result, V1_CODE, mode="historical_replay", registry=registry())
    with pytest.raises(ValueError):
        queries.decode_context_result_rows(
            v1_rows(approval, consumption, result),
            consumption_id=consumption["consumption_id"],
        )


def test_v2_record_under_the_v1_context_decoder_is_a_wrong_family_refusal():
    _registry, approval, consumption, result, _catalogue = v2_fixture()
    with pytest.raises(ValueError):
        queries.decode_context_result_rows(
            v2_rows(approval, consumption, result), consumption_id=consumption.consumption_id
        )
    with pytest.raises(ValueError):
        tables.retained_v1_result(
            json.loads(encode(result)), V1_CODE, mode="historical_read", registry=registry()
        )


def test_v1_context_decoder_carries_the_selected_retained_registry():
    approval, consumption, result = v1_source_snapshot_chain()
    decoded = queries.decode_context_result_rows(
        v1_rows(approval, consumption, result), consumption_id=consumption["consumption_id"]
    )
    assert decoded["consumption"].consumption_id == consumption["consumption_id"]
    assert type(decoded["registry"]) is execution_origins.OriginRegistry
    assert decoded["registry"].sha256 == tables.RETAINED_ORIGIN_REGISTRY_SHA256


def test_v2_context_decoder_admits_a_chain_only_through_its_recorded_pair():
    selected, approval, consumption, result, catalogue = v2_fixture()
    decoded = queries.decode_context_result_rows_v2(
        v2_rows(approval, consumption, result),
        consumption_id=consumption.consumption_id,
        operation="r3_apply",
        generation_loader=catalogue,
    )
    assert catalogue.calls == [(selected.sha256, RESOURCE_SHA)]
    assert type(decoded["approval"]) is execution_approval.ExecutionApprovalV2
    assert type(decoded["consumption"]) is execution_approval.ExecutionConsumptionV2
    assert decoded["result"]["result_id"] == result.result_id
    assert decoded["result"]["origin_registry_sha256"] == selected.sha256
    assert decoded["registry"] is selected
    assert decoded["generation"] == (selected.sha256, RESOURCE_SHA)


def test_v2_context_decoder_has_no_default_operation_and_never_infers_it_from_the_row():
    _registry, approval, consumption, result, catalogue = v2_fixture()
    rows = v2_rows(approval, consumption, result)
    with pytest.raises(TypeError):
        queries.decode_context_result_rows_v2(
            rows, consumption_id=consumption.consumption_id, generation_loader=catalogue
        )
    with pytest.raises(ValueError):
        queries.decode_context_result_rows_v2(
            rows,
            consumption_id=consumption.consumption_id,
            operation="r3_release",
            generation_loader=catalogue,
        )


def test_v1_record_under_the_v2_path_is_a_wrong_family_refusal_before_any_catalogue_lookup():
    approval, consumption, result = v1_source_snapshot_chain()
    _registry, _a, _c, _r, catalogue = v2_fixture()
    with pytest.raises(ValueError):
        queries.decode_context_result_rows_v2(
            v1_rows(approval, consumption, result),
            consumption_id=consumption["consumption_id"],
            operation="source_snapshot_capture",
            generation_loader=catalogue,
        )
    assert catalogue.calls == []


def test_old_job_under_v2_refuses():
    _registry, approval, consumption, result, catalogue = v2_fixture()
    rows = v2_rows(approval, consumption, result)
    old_execution = OLD_APPLY_JOB + "/executions/example"
    rows[0]["consumption_json"] = rewrite(
        rows[0]["consumption_json"], job_resource=OLD_APPLY_JOB, execution_name=old_execution
    )
    rows[0]["result_json"] = rewrite(rows[0]["result_json"], execution_name=old_execution)
    with pytest.raises(ValueError):
        queries.decode_context_result_rows_v2(
            rows,
            consumption_id=consumption.consumption_id,
            operation="r3_apply",
            generation_loader=catalogue,
        )


@pytest.mark.parametrize("field", ["approval_count", "consumption_count", "result_count"])
def test_two_rows_for_one_immutable_key_refuse_instead_of_picking_the_first(field):
    _registry, approval, consumption, result, catalogue = v2_fixture()
    rows = v2_rows(approval, consumption, result, **{field: 2})
    with pytest.raises(ValueError):
        queries.decode_context_result_rows_v2(
            rows,
            consumption_id=consumption.consumption_id,
            operation="r3_apply",
            generation_loader=catalogue,
        )
    assert catalogue.calls == []
    duplicated = v2_rows(approval, consumption, result) * 2
    with pytest.raises(ValueError):
        queries.decode_context_result_rows_v2(
            duplicated,
            consumption_id=consumption.consumption_id,
            operation="r3_apply",
            generation_loader=catalogue,
        )
    assert catalogue.calls == []


def test_cross_generation_reuse_refuses_before_the_catalogue_is_consulted():
    _registry, approval, consumption, result, catalogue = v2_fixture()
    rows = v2_rows(approval, consumption, result)
    rows[0]["consumption_json"] = rewrite(
        rows[0]["consumption_json"], origin_registry_sha256="f" * 64
    )
    with pytest.raises(ValueError):
        queries.decode_context_result_rows_v2(
            rows,
            consumption_id=consumption.consumption_id,
            operation="r3_apply",
            generation_loader=catalogue,
        )
    assert catalogue.calls == []


def test_unknown_recorded_pair_refuses_and_the_active_pair_is_never_substituted():
    selected, approval, consumption, result, _catalogue = v2_fixture()
    active_only = FakeCatalogue(selected, (selected.sha256, "a" * 64))
    with pytest.raises(ValueError):
        queries.decode_context_result_rows_v2(
            v2_rows(approval, consumption, result),
            consumption_id=consumption.consumption_id,
            operation="r3_apply",
            generation_loader=active_only,
        )
    assert active_only.calls == [(selected.sha256, RESOURCE_SHA)]


@pytest.mark.parametrize("digest", ["F" * 64, "e" * 63, "", 7])
def test_malformed_recorded_digest_refuses_before_the_catalogue_is_consulted(digest):
    _registry, approval, consumption, result, catalogue = v2_fixture()
    rows = v2_rows(approval, consumption, result)
    for key in ("approval_json", "consumption_json", "result_json"):
        rows[0][key] = rewrite(rows[0][key], resource_manifest_sha256=digest)
    with pytest.raises(ValueError):
        queries.decode_context_result_rows_v2(
            rows,
            consumption_id=consumption.consumption_id,
            operation="r3_apply",
            generation_loader=catalogue,
        )
    assert catalogue.calls == []


def test_catalogue_registry_that_disagrees_with_the_recorded_pair_refuses():
    selected, approval, consumption, result, _catalogue = v2_fixture()
    lying = FakeCatalogue(registry(), (selected.sha256, RESOURCE_SHA))
    rows = v2_rows(approval, consumption, result)
    for key in ("approval_json", "consumption_json", "result_json"):
        rows[0][key] = rewrite(rows[0][key], origin_registry_sha256="b" * 64)
    lying.pair = ("b" * 64, RESOURCE_SHA)
    with pytest.raises(ValueError):
        queries.decode_context_result_rows_v2(
            rows,
            consumption_id=consumption.consumption_id,
            operation="r3_apply",
            generation_loader=lying,
        )


def test_v2_approval_decoder_admits_and_refuses_like_the_result_decoder():
    selected, approval, _consumption, _result, catalogue = v2_fixture()
    rows = [{"approval_count": 1, "approval_json": encode(approval)}]
    decoded = queries.decode_context_approval_rows_v2(
        rows,
        manifest_sha256=approval.manifest_sha256,
        operation="r3_apply",
        generation_loader=catalogue,
    )
    assert decoded["approval"].approval_id == approval.approval_id
    assert decoded["registry"] is selected
    with pytest.raises(ValueError):
        queries.decode_context_approval_rows_v2(
            [{"approval_count": 2, "approval_json": encode(approval)}],
            manifest_sha256=approval.manifest_sha256,
            operation="r3_apply",
            generation_loader=catalogue,
        )
    v1_approval, _c, _r = v1_source_snapshot_chain()
    with pytest.raises(ValueError):
        queries.decode_context_approval_rows_v2(
            [{"approval_count": 1, "approval_json": encode(v1_approval)}],
            manifest_sha256=v1_approval.manifest_sha256,
            operation="source_snapshot_capture",
            generation_loader=catalogue,
        )


def test_v1_and_v2_context_queries_are_separate_named_statements():
    args = queries_inputs()
    v1 = queries.build_context_result_query(*args, consumption_id="exc_" + "1" * 64)
    v2 = queries.build_context_result_query_v2(*args, consumption_id="exc_" + "1" * 64)
    assert v1.template_id == "protected_context_result_v1"
    assert v2.template_id == "protected_context_result_v2"
    assert "_v2`" not in v1.sql
    assert "UNION" not in v1.sql
    assert "_v1`" not in v2.sql
    assert "UNION" not in v2.sql
    for version in ("approval", "consumption", "result"):
        assert f"open_intelligence_execution_{version}_v2'" in v2.sql
    assert "origin_registry_sha256" in v2.sql
    assert "resource_manifest_sha256" in v2.sql
    assert v1.sql_digest != v2.sql_digest
    approval_v2 = queries.build_context_approval_query_v2(*args, manifest_sha256="2" * 64)
    assert approval_v2.template_id == "protected_context_approval_v2"
    assert "open_intelligence_execution_approvals_v2`" in approval_v2.sql
    assert "approval_contract_version" not in approval_v2.sql
    assert "_v1`" not in approval_v2.sql


def test_two_approval_rows_for_one_manifest_digest_differing_only_in_version_refuse():
    selected, approval, _consumption, _result, catalogue = v2_fixture()
    current = encode(approval)
    other = rewrite(current, approval_contract_version="open_intelligence_execution_approval_v3")
    family = [json.loads(current), json.loads(other)]
    assert {row["manifest_sha256"] for row in family} == {approval.manifest_sha256}
    assert len({row["approval_contract_version"] for row in family}) == 2
    matching = [row for row in family if row["manifest_sha256"] == approval.manifest_sha256]
    projected = [{"approval_count": len(matching), "approval_json": current}]
    assert projected[0]["approval_count"] == 2
    with pytest.raises(ValueError, match="context_authority_invalid"):
        queries.decode_context_approval_rows_v2(
            projected,
            manifest_sha256=approval.manifest_sha256,
            operation="r3_apply",
            generation_loader=catalogue,
        )
    assert catalogue.calls == []
    with pytest.raises(ValueError, match="context_authority_invalid"):
        queries.decode_context_approval_rows_v2(
            [{"approval_count": 1, "approval_json": other}],
            manifest_sha256=approval.manifest_sha256,
            operation="r3_apply",
            generation_loader=catalogue,
        )
    assert catalogue.calls == [(selected.sha256, RESOURCE_SHA)]


def queries_inputs():
    from tests.unit.test_general_question_context_queries import inputs

    return inputs()[:3]


def test_release_material_reads_only_retained_v1_tables_and_admission_refuses_a_v2_chain():
    sql = release_material.build_release_material_query("run_20260903_dynamic_apply_v2_r16").sql
    for table in ("approvals", "consumptions", "results"):
        assert f"open_intelligence_execution_{table}_v1`" in sql
        assert f"open_intelligence_execution_{table}_v2" not in sql
    _registry, approval, consumption, result, _catalogue = v2_fixture()
    receipt = SimpleNamespace(run_id="run_20260903_dynamic_apply_v2_r16")
    chains = [
        {
            "operation": operation,
            "chain_count": 1,
            "rows": [
                {
                    "operation": operation,
                    "approval_json": encode(approval),
                    "consumption_json": encode(consumption),
                    "result_json": encode(result),
                    "approval_identity_count": 1,
                    "consumption_identity_count": 1,
                    "result_identity_count": 1,
                }
            ],
        }
        for operation in ("r3_apply", "r3_proof_issue", "r3_release")
    ]
    with pytest.raises(ValueError, match="release_admission_invalid"):
        release_admission._chains(chains, receipt=receipt, as_of=datetime.now(UTC))


def test_every_retained_reader_call_selects_a_literal_historical_mode_and_a_registry():
    names = {
        "validate_execution_manifest",
        "canonical_manifest_bytes",
        "manifest_sha256",
        "retained_v1_result",
        "retained_v1_approval",
        "ExecutionApprovalV2",
        "ExecutionConsumptionV2",
        "ExecutionResultV2",
        "validate_execution_chain_v2",
    }
    helpers = {"retained_v1_result", "retained_v1_approval", "_retained_v1_origin"}
    seen = {}
    for module in MODULES:
        path = ENGINE_ROOT / "src/analysis/open_intelligence" / f"{module}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                name = (
                    target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                )
                if name not in names:
                    continue
                keywords = {keyword.arg: keyword.value for keyword in node.keywords}
                assert "mode" in keywords, (module, function.name, name)
                assert "registry" in keywords, (module, function.name, name)
                mode = keywords["mode"]
                if function.name in helpers and module == "production_snapshot_tables":
                    assert isinstance(mode, ast.Name)
                    assert mode.id == "mode"
                else:
                    assert isinstance(mode, ast.Constant), (module, function.name, name)
                    assert mode.value in tables.HISTORICAL_MODES, (module, function.name)
                    seen.setdefault(module, set()).add(mode.value)
    assert seen["general_question_release_admission"] == {"historical_read"}
    assert seen["general_question_context_queries"] == {"historical_read"}
    assert seen["general_question_snapshot"] == {"historical_read", "historical_replay"}
    assert seen["production_snapshot_capture"] == {"historical_replay"}
    assert seen["general_question_context_admission"] == {"historical_read"}
    assert seen["production_snapshot_tables"] == {"historical_replay"}
