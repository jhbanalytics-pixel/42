from dataclasses import fields, replace
from pathlib import Path

import pytest
from src.analysis.open_intelligence import execution_approval as runtime

from tests.unit import test_daily_authority as daily
from tests.unit import test_execution_runtime_v2 as native


@pytest.fixture
def capture_generation(monkeypatch):
    """Supply an isolated capture identity without changing the pinned files, under the
    amendment e generation, whose capture policy takes the v2 plan these walls cover. The
    fixture it returns is a v2 plan capture under that generation."""
    fx = native.amendment_e_capture_fixture(monkeypatch)
    generation = native.generations().active_generation()
    resource = dict(generation.resource_manifest)
    resource["identities"] = dict(resource["identities"])
    resource["identities"]["execution_source_snapshot_capture"] = (
        "//iam.googleapis.com/projects/ogilvy-trends-v2/serviceAccounts/"
        "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    amended = replace(generation, resource_manifest=resource)
    original = native.generations().require_active_generation

    def selected(*pair):
        original(*pair)
        return amended

    monkeypatch.setattr(native.generations(), "require_active_generation", selected)
    return fx


def test_missing_capture_identity_refuses(monkeypatch):
    fx = native.fixture("source_snapshot_capture")
    generation = native.generations().active_generation()
    resource = dict(generation.resource_manifest)
    resource["identities"] = dict(resource["identities"])
    resource["identities"].pop("execution_source_snapshot_capture", None)
    original = native.generations().require_active_generation

    def missing_identity(*pair):
        original(*pair)
        return replace(generation, resource_manifest=resource)

    monkeypatch.setattr(native.generations(), "require_active_generation", missing_identity)
    with native.refusal("execution_approval_identity_invalid"):
        capture_load(fx)


def capture_load(fx, **overrides):
    for task in (fx.execution["template"], fx.job["template"]["template"]):
        task["containers"][0]["resources"] = {"limits": {"cpu": "1", "memory": "1Gi"}}
    kwargs = {
        "mode": "new_consume",
        "execution_reader": lambda: {"execution": fx.execution, "job": fx.job},
        "approval_reader": lambda digest: fx.approval,
        "build_reader": lambda name: fx.build,
        "artifact_reader": lambda name: fx.contents[name],
        "now": lambda: native.NOW,
    }
    kwargs.update(overrides)
    return runtime._load_source_snapshot_authority(**kwargs)


def capture_query(fx, calls, mutation=None):
    def query(name, parameters):
        values = {item.name: item.value for item in parameters}
        calls.append((name, values))
        request = {
            key: values[key]
            for key in (
                "manifest_sha256",
                "execution_name",
                "job_resource",
                "source_sha",
                "image_uri",
                "origin_registry_sha256",
                "resource_manifest_sha256",
            )
        }
        request.update(approval_id=fx.approval.approval_id, operation=fx.operation)
        row = native.consumption_row(fx, request, resource_sha=fx.approval.resource_manifest_sha256)
        for key in ("job_resource", "source_sha", "image_uri"):
            row.pop(key)
        if mutation:
            replacements = {
                "consumption_id": "exc_" + "f" * 64,
                "approval_id": "exa_" + "f" * 64,
                "manifest_sha256": "f" * 64,
                "operation": "r3_apply",
                "execution_name": request["job_resource"] + "/executions/other-1",
                "origin_registry_sha256": "f" * 64,
                "resource_manifest_sha256": "f" * 64,
            }
            row[mutation] = replacements[mutation]
            if mutation in {"approval_id", "manifest_sha256", "execution_name", "operation"}:
                row["consumption_id"] = runtime.consumption_id_v2(
                    row["approval_id"],
                    row["execution_name"],
                    row["consumed_at"],
                    origin_registry_sha256=row["origin_registry_sha256"],
                    resource_manifest_sha256=row["resource_manifest_sha256"],
                )
        return row

    return query


def test_real_consumption_dataclass_keeps_identity_and_plain_record():
    fx = native.fixture("collection_exposure_issue")
    issued = native.load(fx)
    consumed = native.consume(fx, issued)

    class Reservation:
        def reserve(self, invocation, manifest):
            return issued

        def consume(self, authority):
            return consumed

    ledger = daily.FakeLedger()
    receipt = daily.authority(reservation=Reservation(), ledger=ledger).issue(
        "collect", daily.manifest("collect", daily.grant())
    )
    assert receipt["execution"]["consumption"] is consumed
    assert (
        runtime._require_consumed_execution(
            issued, receipt["execution"]["consumption"], fx.operation
        )
        is consumed
    )
    assert receipt["reservation"] == daily.daily_authority._plain(
        {field.name: getattr(consumed, field.name) for field in fields(consumed)}
    )
    assert next(iter(ledger.records.values()))["reservation"] == receipt["reservation"]
    daily.canonical_bytes(next(iter(ledger.records.values())))


def test_capture_uses_dedicated_routine_and_keeps_generic_guard(capture_generation):
    fx = capture_generation
    with native.refusal("execution_approval_manifest_invalid"):
        native.load(fx)
    issued = capture_load(fx)
    calls = []
    consumed = runtime._consume_source_snapshot_authority(
        issued, artifact_reader=lambda name: fx.contents[name], query=capture_query(fx, calls)
    )
    assert calls[0][0] == "sp_consume_open_intelligence_source_snapshot_v2"
    for artifact in ("capture_plan", "recovery_context", "storage_policy"):
        assert calls[0][1][artifact + "_json"] == fx.contents[artifact].decode()
    assert runtime._require_consumed_execution(issued, consumed, fx.operation) is consumed
    with native.refusal("execution_approval_consumed"):
        runtime._consume_source_snapshot_authority(
            issued, artifact_reader=lambda name: fx.contents[name], query=capture_query(fx, calls)
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field",
    [
        "consumption_id",
        "approval_id",
        "manifest_sha256",
        "operation",
        "execution_name",
        "origin_registry_sha256",
        "resource_manifest_sha256",
    ],
)
def test_capture_refuses_returned_binding_mismatch(field, capture_generation):
    fx = capture_generation
    issued = capture_load(fx)
    with native.refusal("execution_approval_schema_mismatch"):
        runtime._consume_source_snapshot_authority(
            issued,
            artifact_reader=lambda name: fx.contents[name],
            query=capture_query(fx, [], field),
        )


@pytest.mark.parametrize("artifact", ["capture_plan", "recovery_context", "storage_policy"])
def test_capture_refuses_changed_approved_bytes_before_query(artifact, capture_generation):
    fx = capture_generation
    issued = capture_load(fx)
    fx.contents[artifact] = b"{}"
    calls = []
    with native.refusal("execution_approval_artifact_mismatch"):
        runtime._consume_source_snapshot_authority(
            issued, artifact_reader=lambda name: fx.contents[name], query=capture_query(fx, calls)
        )
    assert calls == []


def test_dedicated_loader_keeps_runtime_job_gate():
    fx = native.fixture("source_snapshot_capture")
    fx.execution["job"] = "wrong"
    with native.refusal("execution_approval_execution_mismatch"):
        capture_load(fx)


def test_sql_independently_checks_omitted_return_fields():
    sql = (
        Path(__file__).parents[2]
        / "infra/bigquery_routines/sp_consume_open_intelligence_source_snapshot_v2.sql"
    ).read_text()
    for field in ("job_resource", "source_sha", "image_uri"):
        assert f"open_intelligence_execution_consumptions_v2.{field} = v_{field}" in sql
    assert sql.index("COMMIT TRANSACTION;") < sql.index(
        "SELECT 'open_intelligence_execution_consumption_v2'"
    )


@pytest.mark.parametrize("mutation", ["source", "image", "build", "origin"])
def test_capture_build_binds_independent_native_response(capture_generation, mutation):
    fx = capture_generation
    if mutation == "source":
        fx.build["source"]["connectedRepository"]["revision"] = "b" * 40
    elif mutation == "image":
        fx.build["results"]["images"][0]["digest"] = "sha256:" + "f" * 64
    elif mutation == "build":
        old = fx.build["id"]
        fx.build["id"] = "99999999-9999-9999-9999-999999999999"
        fx.build["name"] = fx.build["name"].replace(old, fx.build["id"])
    else:
        fx.build["source"]["connectedRepository"]["repository"] = "wrong-origin"
    with pytest.raises(
        ValueError, match=r"execution_approval_(artifact_mismatch|build_provenance_invalid)"
    ):
        capture_load(fx)


def test_default_capture_artifacts_use_approved_digest_objects(capture_generation, monkeypatch):
    from google.cloud import storage
    from src.analysis.open_intelligence import production_snapshot_storage as objects

    fx = capture_generation
    calls = []

    class Client:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))

        def bucket(self, name):
            assert name == objects.BUCKET_NAME
            return object()

        def close(self):
            calls.append(("close",))

    class Reader:
        def __init__(self, bucket):
            pass

        def read_input(self, name, digest, *, timeout):
            assert (
                digest
                == {item["name"]: item["sha256"] for item in fx.payload["input_artifacts"]}[name]
            )
            assert timeout == 30
            calls.append(("read", name))
            return fx.contents[name]

    monkeypatch.setattr(storage, "Client", Client)
    monkeypatch.setattr(objects, "SourceCaptureObjects", Reader)
    monkeypatch.setattr(runtime, "_runtime_credentials", lambda: "attached")
    issued = capture_load(fx, artifact_reader=None)
    runtime._consume_source_snapshot_authority(issued, query=capture_query(fx, []))
    assert [call for call in calls if call[0] == "close"] == [("close",), ("close",)]
    assert len([call for call in calls if call[0] == "read"]) == 8


@pytest.mark.parametrize("field", ["source_sha", "image_uri", "operation", "generation"])
def test_capture_rejects_mutated_duplicate_authority_slots_before_effects(
    capture_generation, field
):
    fx = capture_generation
    issued = capture_load(fx)
    if field == "generation":
        issued.generation = replace(issued.generation, resource_manifest={})
    else:
        setattr(
            issued,
            field,
            {
                "source_sha": "f" * 40,
                "image_uri": issued.image_uri.rsplit("@", 1)[0] + "@sha256:" + "f" * 64,
                "operation": "r3_apply",
            }[field],
        )
    reads = []
    calls = []

    def reader(name):
        reads.append(name)
        return fx.contents[name]

    with native.refusal("execution_approval_identity_invalid"):
        runtime._consume_source_snapshot_authority(
            issued, artifact_reader=reader, query=capture_query(fx, calls)
        )
    assert reads == []
    assert calls == []
