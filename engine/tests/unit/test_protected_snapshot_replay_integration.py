import base64
import copy
import json
import socket
import subprocess
import sys
from datetime import datetime, timedelta

import pytest
from google.cloud import bigquery
from scripts.staging import replay_open_intelligence as subject
from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.contracts.open_intelligence import ResolvedScope

from tests.unit import test_production_snapshot_storage as storage_fixture
from tests.unit import test_protected_snapshot_integration as capture_fixture
from tests.unit.test_provenance_replay_v3 import Provider

_REAL_CLIENT = bigquery.Client


class NativeLedgerHTTP:
    is_mtls = False

    def __init__(self, result, approval, *, wrong_identity=False):
        self.result = result
        self.approval = approval
        self.wrong_identity = wrong_identity
        self.jobs = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        payload = json.loads(kwargs["data"]) if kwargs.get("data") else None
        self.calls.append((method, url, payload))
        if self.wrong_identity:
            return storage_fixture.HTTP.response(
                method,
                url,
                403,
                b'{"error":{"code":403,"message":"execution_approval_identity_invalid"}}',
            )
        if method == "POST":
            query = payload["configuration"]["query"]
            sql = query["query"]
            assert sql.startswith(
                "CALL `ogilvy-trends-v2.trends_v2_staging_approvals.sp_read_open_intelligence_execution_"
            )
            parameter = query["queryParameters"][0]["parameterValue"]["value"]
            if "execution_result_v1`" in sql:
                assert parameter == self.result["consumption_id"]
                row = self.result
            else:
                assert "execution_approval_v1`" in sql
                assert parameter == self.approval["manifest_sha256"]
                row = self.approval
            self.jobs[payload["jobReference"]["jobId"]] = (payload, row)
            response = {**payload, "status": {"state": "DONE"}}
        else:
            payload, row = self.jobs[url.split("?", 1)[0].rsplit("/", 1)[1]]
            if "/queries/" in url:
                fields = [
                    {"name": key, "type": "TIMESTAMP" if isinstance(value, datetime) else "STRING"}
                    for key, value in row.items()
                ]
                values = [
                    str(int(value.timestamp() * 1_000_000))
                    if isinstance(value, datetime)
                    else value
                    for value in row.values()
                ]
                response = {
                    "jobReference": payload["jobReference"],
                    "jobComplete": True,
                    "schema": {"fields": fields},
                    "totalRows": "1",
                    "rows": [{"f": [{"v": value} for value in values]}],
                }
            else:
                response = {**payload, "status": {"state": "DONE"}}
        return storage_fixture.HTTP.response(method, url, 200, canonical_bytes(response))


def completed_capture(monkeypatch):
    from dataclasses import asdict

    http, storage_http, args, view = capture_fixture.joined(monkeypatch)
    payload, status = capture_fixture.operation_fixture.subject()._execute_validated_operation(
        **args
    )
    assert status == "succeeded"
    completed_at = capture_fixture.OBJECT_CREATED + timedelta(seconds=1)
    # The result row is the v1 record a historical capture left, built directly from the
    # payload the validated body returned (retained view, successor 1).
    result = view.result_record(
        payload,
        status="succeeded",
        completed_at=completed_at,
        reference=view.consumption.execution_name + "#source-snapshot",
    )
    stored_rows = [asdict(result)]
    for name, raw in args["artifacts"].items():
        storage_http.seed(f"inputs/{subject.hashlib.sha256(raw).hexdigest()}/{name}.json", raw)
    ledger = NativeLedgerHTTP(stored_rows[0], asdict(view.approval))
    credentials = capture_fixture.creation_fixture.CreationCredentials()
    monkeypatch.setattr(execution_approval, "_runtime_credentials", lambda: credentials)
    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: _REAL_CLIENT(**kwargs, _http=ledger))
    scope = ResolvedScope(
        "ogilvy_default",
        ("ke", "ng", "za"),
        "ogilvy",
        (),
        "open_intelligence",
        "protected_replay_synthetic",
        "2.0.0",
    )
    inputs = {
        "manifest_sha256": result.manifest_sha256,
        "consumption_id": result.consumption_id,
        "result_id": result.result_id,
        "result_digest": result.result_digest,
        "scope": scope,
        "objects": args["objects"],
        "semantic_provider": Provider(),
    }
    return http, storage_http, ledger, inputs


def reseal_result(row, payload):
    row["canonical_result_json"] = canonical_bytes(payload).decode()
    row["result_digest"] = canonical_digest(payload)
    row["result_id"] = execution_approval.result_id(
        row["consumption_id"],
        row["result_reference"],
        row["result_digest"],
        row["status"],
        row["completed_at"],
    )


def test_cross_process_native_result_and_exact_generation_replay_without_recapture(monkeypatch):
    http, storage_http, ledger, inputs = completed_capture(monkeypatch)
    before_creations, before_selects = len(http.creation.calls), len(http.capture.calls)
    uploads = len([call for call in storage_http.calls if call[0] == "POST"])
    replay = subject._protected_snapshot_result_to_replay(**inputs)
    assert replay["production_snapshot"]["source_authority"] is False
    assert replay["human_review_state"] == "pending"
    assert replay["artifact_version"] == "open_intelligence_production_replay_v1"
    assert len(replay["components_by_candidate"]) == 1
    assert len(http.creation.calls) == before_creations
    assert len(http.capture.calls) == before_selects
    assert len([call for call in storage_http.calls if call[0] == "POST"]) == uploads
    assert len([call for call in ledger.calls if call[0] == "POST"]) == 2
    encoded = subject.serialize_production_replay_input(replay)
    assert (
        subject.serialize_production_replay_input(
            subject.validate_production_replay_input(
                json.loads(encoded), semantic_provider=Provider()
            )
        )
        == encoded
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "result_id",
        "result_digest",
        "manifest",
        "approval",
        "failed",
        "generation",
        "scope",
        "time",
        "snapshot_digest",
        "missing_checks",
    ],
)
def test_substituted_native_results_and_bound_payloads_refuse(monkeypatch, mutation):
    _http, _storage, ledger, inputs = completed_capture(monkeypatch)
    row = ledger.result
    payload = json.loads(row["canonical_result_json"])
    if mutation == "result_id":
        inputs["result_id"] = "exr_" + "1" * 64
    elif mutation == "result_digest":
        inputs["result_digest"] = "1" * 64
    elif mutation == "manifest":
        inputs["manifest_sha256"] = "1" * 64
    elif mutation == "approval":
        ledger.approval["approval_id"] = "exa_" + "1" * 64
    elif mutation == "failed":
        row["status"] = "failed"
    elif mutation == "generation":
        payload["stored_artifact"]["generation"] += 100
    elif mutation == "scope":
        payload["client_scope_id"] = "foreign_scope"
    elif mutation == "time":
        payload["captured_at"] = "2026-09-09T12:00:01+00:00"
    elif mutation == "snapshot_digest":
        payload["snapshot_digest"] = "1" * 64
    else:
        payload["missing_checks"] = ["snapshot_capture_incomplete"]
    if mutation in {"failed", "generation", "scope", "time", "snapshot_digest", "missing_checks"}:
        reseal_result(row, payload)
        inputs.update(result_id=row["result_id"], result_digest=row["result_digest"])
    with pytest.raises(ValueError):
        subject._protected_snapshot_result_to_replay(**inputs)


def test_guarded_native_reader_refuses_wrong_identity_before_private_artifact_read(monkeypatch):
    _http, storage_http, ledger, inputs = completed_capture(monkeypatch)
    ledger.wrong_identity = True
    before = len(storage_http.calls)
    with pytest.raises(ValueError, match="execution_approval_identity_invalid"):
        subject._protected_snapshot_result_to_replay(**inputs)
    assert len(storage_http.calls) == before


def test_shared_reader_replays_historical_completion_and_returns_exact_binding(monkeypatch):
    from src.analysis.open_intelligence import production_snapshot_capture

    _http, _storage, ledger, inputs = completed_capture(monkeypatch)
    monkeypatch.setattr(
        production_snapshot_capture,
        "_utc_now",
        lambda: capture_fixture.OPERATION_NOW + timedelta(days=365),
    )
    value = production_snapshot_capture._read_protected_capture(
        **{
            name: inputs[name]
            for name in (
                "manifest_sha256",
                "consumption_id",
                "result_id",
                "result_digest",
                "objects",
            )
        },
        client_scope_id="ogilvy_default",
        market_scope=("ke", "ng", "za"),
    )
    assert set(value) == {"capture", "binding"}
    assert value["capture"]["assembly"]["source_authority"] is False
    binding = value["binding"]
    assert binding["recovery_context"] is None
    assert set(binding) == {
        "operation",
        "manifest_sha256",
        "consumption_id",
        "result_id",
        "result_digest",
        "approval_id",
        "execution_name",
        "source_sha",
        "image_uri",
        "stored_artifact",
        "cutoff_date",
        "source_as_of",
        "captured_at",
        "snapshot_digest",
        "capture_receipt_digest",
        "snapshot_plan_digest",
        "client_scope_id",
        "market_scope",
        "recovery_context",
    }
    assert binding["result_digest"] == inputs["result_digest"]
    assert binding["captured_at"] == capture_fixture.OPERATION_NOW.isoformat()
    assert (
        binding["stored_artifact"]
        == json.loads(ledger.result["canonical_result_json"])["stored_artifact"]
    )


@pytest.mark.parametrize(
    "mutation", ["before_approval", "after_expiry", "foreign_execution", "wrong_reference"]
)
def test_native_completed_interval_and_execution_reference_must_match_manifest(
    monkeypatch, mutation
):
    _http, _storage, ledger, inputs = completed_capture(monkeypatch)
    row = ledger.result
    if mutation == "before_approval":
        row["completed_at"] = ledger.approval["approved_at"] - timedelta(seconds=1)
    elif mutation == "after_expiry":
        row["completed_at"] = ledger.approval["expires_at"] + timedelta(seconds=1)
    elif mutation == "foreign_execution":
        row["execution_name"] = row["execution_name"].replace(
            "source-snapshot-staging", "apply-staging"
        )
    else:
        row["result_reference"] += ":changed"
    reseal_result(row, json.loads(row["canonical_result_json"]))
    inputs.update(result_id=row["result_id"], result_digest=row["result_digest"])
    with pytest.raises(ValueError):
        subject._protected_snapshot_result_to_replay(**inputs)


def _fresh_process_replay(value):
    patch = pytest.MonkeyPatch()
    patch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network prohibited"))
    for field in ("approved_at", "expires_at"):
        value["approval"][field] = datetime.fromisoformat(value["approval"][field])
    value["result"]["completed_at"] = datetime.fromisoformat(value["result"]["completed_at"])
    ledger = NativeLedgerHTTP(value["result"], value["approval"])
    credentials = capture_fixture.creation_fixture.CreationCredentials()
    objects, storage_http = storage_fixture.objects()
    for entry in value["objects"]:
        storage_http.seed(
            entry["name"],
            base64.b64decode(entry["raw"]),
            generation=entry["generation"],
            created=entry["created"],
        )
    patch.setattr(execution_approval, "_runtime_credentials", lambda: credentials)
    patch.setattr(bigquery, "Client", lambda **kwargs: _REAL_CLIENT(**kwargs, _http=ledger))
    scope = ResolvedScope(
        "ogilvy_default",
        ("ke", "ng", "za"),
        "ogilvy",
        (),
        "open_intelligence",
        "protected_replay_fresh",
        "2.0.0",
    )
    try:
        replay = subject._protected_snapshot_result_to_replay(
            **value["pins"], scope=scope, objects=objects, semantic_provider=Provider()
        )
        return {
            "snapshot_digest": replay["production_snapshot"]["snapshot"]["source_digest"],
            "source_authority": replay["production_snapshot"]["source_authority"],
            "human_review_state": replay["human_review_state"],
            "components": len(replay["components_by_candidate"]),
            "uploads": len([call for call in storage_http.calls if call[0] == "POST"]),
        }
    finally:
        patch.undo()


def test_persisted_capture_replays_in_fresh_process_without_original_capability(monkeypatch):
    _http, storage_http, ledger, inputs = completed_capture(monkeypatch)
    value = {
        "pins": {
            name: inputs[name]
            for name in ("manifest_sha256", "consumption_id", "result_id", "result_digest")
        },
        "result": ledger.result,
        "approval": ledger.approval,
        "objects": [
            {
                "name": name,
                "raw": base64.b64encode(item["raw"]).decode(),
                "generation": item["generation"],
                "created": item["created"],
            }
            for name, item in storage_http.objects.items()
            if type(name) is str
        ],
    }
    code = "import json,sys; from tests.unit.test_protected_snapshot_replay_integration import _fresh_process_replay; print(json.dumps(_fresh_process_replay(json.load(sys.stdin))))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(value, default=lambda item: item.isoformat()),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "snapshot_digest": json.loads(ledger.result["canonical_result_json"])["snapshot_digest"],
        "source_authority": False,
        "human_review_state": "pending",
        "components": 1,
        "uploads": 0,
    }


def completed_replacement_capture(monkeypatch, failed_index=2, *, continuation=False):
    from src.analysis.open_intelligence.production_snapshot_capture import _read_protected_capture

    from tests.unit.test_open_intelligence_execution_approval_runtime import (
        retained_capture_view,
    )

    http, storage_http, args, view = capture_fixture.joined(monkeypatch)
    module = capture_fixture.operation_fixture.subject()
    elapsed = [0.0]
    monkeypatch.setattr(capture_fixture.creation_fixture.subject, "monotonic", lambda: elapsed[0])

    def sleep(seconds):
        elapsed[0] += seconds

    monkeypatch.setattr(capture_fixture.creation_fixture.subject, "sleep", sleep)
    lane = capture_fixture.creation_fixture.subject.LANES[failed_index]

    def fail_job(native):
        if native["jobReference"]["jobId"].endswith("_" + lane):
            native["status"]["errorResult"] = {"reason": "accessDenied"}
            native["statistics"]["query"].pop("ddlOperationPerformed")
        return native

    http.creation.mutate_job = fail_job
    failed, status = module._execute_validated_operation(**args)
    assert status == "failed"
    failed["creation_records"][-1].update(state="unresolved", native_job_digest=None)

    def row_for(payload, status, consumption, completed):
        reference = consumption.execution_name + "#source-snapshot"
        digest = canonical_digest(payload)
        return {
            "result_contract_version": "open_intelligence_execution_result_v1",
            "result_id": execution_approval.result_id(
                consumption.consumption_id, reference, digest, status, completed
            ),
            "consumption_id": consumption.consumption_id,
            "approval_id": consumption.approval_id,
            "manifest_sha256": consumption.manifest_sha256,
            "operation": "source_snapshot_capture",
            "execution_name": consumption.execution_name,
            "result_reference": reference,
            "canonical_result_json": canonical_bytes(payload).decode(),
            "result_digest": digest,
            "status": status,
            "completed_at": completed,
        }

    original = row_for(failed, status, args["consumption"], capture_fixture.OPERATION_NOW)
    prior_results = {original["consumption_id"]: original}
    prior_approvals = {original["manifest_sha256"]: view.approval}
    recovery = {
        "contract_version": "open_intelligence_source_capture_recovery_v2",
        "failed_creation_job_digest": canonical_digest(
            http.creation.jobs[failed["creation_records"][-1]["job_id"]]
        ),
        **{
            "initial_" + key: original[key]
            for key in (
                "manifest_sha256",
                "consumption_id",
                "execution_name",
                "result_id",
                "result_digest",
            )
        },
    }
    artifacts = {**args["artifacts"], "recovery_context": canonical_bytes(recovery)}
    view = retained_capture_view(artifacts=artifacts, mode="recover")
    recheck, claim = capture_fixture.creation_fixture.retained_closures(view)
    http.creation.mutate_job = lambda value: value
    original_request = http.request
    reject_predecessor_metadata = [continuation]
    reject_latest_metadata = [False]

    def request(method, url, **kwargs):
        if (
            "/tables/" in url
            and url.split("?", 1)[0].endswith("_" + lane)
            and not any(
                job_id.endswith("_" + lane) and not job["status"].get("errorResult")
                for job_id, job in http.creation.jobs.items()
            )
        ):
            return storage_fixture.HTTP.response(method, url, 404, b'{"error":{"code":404}}')
        response = original_request(method, url, **kwargs)
        if (
            (reject_predecessor_metadata[0] or reject_latest_metadata[0])
            and "/tables/" in url
            and url.split("?", 1)[0].endswith(
                "_enriched_content" if reject_latest_metadata[0] else "_" + lane
            )
            and response.status_code == 200
        ):
            native = response.json()
            native["creationTime"] = "0"
            response._content = canonical_bytes(native)
        return response

    http.request = request
    payload, status = module._execute_validated_operation(
        **{
            **args,
            "artifacts": artifacts,
            "mode": "recover",
            "manifest": view.manifest,
            "consumption": view.consumption,
            "recheck": recheck,
            "claim": claim,
            "initial_result_reader": lambda _: original,
        }
    )
    predecessor, predecessor_approval = None, None
    if continuation:
        assert status == "failed"
        payload["creation_records"][-1]["native_job_digest"] = None
        predecessor = row_for(payload, status, view.consumption, capture_fixture.OPERATION_NOW)
        predecessor_approval = view.approval
        prior_results[predecessor["consumption_id"]] = predecessor
        prior_approvals[predecessor["manifest_sha256"]] = predecessor_approval
        recovery = {
            "contract_version": "open_intelligence_source_capture_recovery_v3",
            "ancestor_recovery_context": recovery,
            **{
                "initial_" + key: predecessor[key]
                for key in (
                    "manifest_sha256",
                    "consumption_id",
                    "execution_name",
                    "result_id",
                    "result_digest",
                )
            },
        }
        artifacts = {**artifacts, "recovery_context": canonical_bytes(recovery)}
        view = retained_capture_view(artifacts=artifacts, mode="recover")
        recheck, claim = capture_fixture.creation_fixture.retained_closures(view)
        reject_predecessor_metadata[0] = False
        reject_latest_metadata[0] = continuation == 4
        baseline_rows = {original["result_id"]: original, predecessor["result_id"]: predecessor}
        payload, status = module._execute_validated_operation(
            **{
                **args,
                "artifacts": artifacts,
                "mode": "recover",
                "manifest": view.manifest,
                "consumption": view.consumption,
                "recheck": recheck,
                "claim": claim,
                "initial_result_reader": baseline_rows.get,
            }
        )
        if continuation == 4:
            assert status == "failed"
            assert len(payload["creation_records"]) == 4
            latest = row_for(payload, status, view.consumption, capture_fixture.OPERATION_NOW)
            prior_results[latest["consumption_id"]] = latest
            prior_approvals[latest["manifest_sha256"]] = view.approval
            recovery = {
                "contract_version": "open_intelligence_source_capture_recovery_v4",
                "ancestor_recovery_context": recovery,
                **{
                    "initial_" + key: latest[key]
                    for key in (
                        "manifest_sha256",
                        "consumption_id",
                        "execution_name",
                        "result_id",
                        "result_digest",
                    )
                },
            }
            artifacts = {**artifacts, "recovery_context": canonical_bytes(recovery)}
            view = retained_capture_view(artifacts=artifacts, mode="recover")
            recheck, claim = capture_fixture.creation_fixture.retained_closures(view)
            reject_latest_metadata[0] = False
            baseline_rows[latest["result_id"]] = latest
            payload, status = module._execute_validated_operation(
                **{
                    **args,
                    "artifacts": artifacts,
                    "mode": "recover",
                    "manifest": view.manifest,
                    "consumption": view.consumption,
                    "recheck": recheck,
                    "claim": claim,
                    "initial_result_reader": baseline_rows.get,
                }
            )
    assert status == "succeeded", payload["missing_checks"]
    result = row_for(
        payload, status, view.consumption, capture_fixture.OBJECT_CREATED + timedelta(seconds=1)
    )
    prior_results[result["consumption_id"]] = result
    prior_approvals[result["manifest_sha256"]] = view.approval
    for name, raw in artifacts.items():
        storage_http.seed(f"inputs/{subject.hashlib.sha256(raw).hexdigest()}/{name}.json", raw)
    kwargs = {
        "manifest_sha256": result["manifest_sha256"],
        "consumption_id": result["consumption_id"],
        "result_id": result["result_id"],
        "result_digest": result["result_digest"],
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ke", "ng", "za"],
        "objects": args["objects"],
        "approval_reader": prior_approvals.get,
        "result_reader": lambda cid: [prior_results[cid]],
    }
    if continuation:
        return (
            _read_protected_capture,
            kwargs,
            storage_http,
            result,
            payload,
            {
                "predecessor_approval": predecessor_approval,
                "artifacts": artifacts,
                "view": view,
                "consumption": view.consumption,
            },
        )
    return _read_protected_capture, kwargs, storage_http, result, payload


@pytest.mark.parametrize("failed_index", [0, 2])
def test_shared_reader_admits_v3_three_owner_chain(monkeypatch, failed_index):
    reader, kwargs, _storage, result, _payload, _state = completed_replacement_capture(
        monkeypatch, failed_index=failed_index, continuation=True
    )
    value = reader(**kwargs)
    assert value["binding"]["manifest_sha256"] == result["manifest_sha256"]


@pytest.mark.parametrize("wrong_bridge", [None, 0, 1])
def test_v4_shared_reader_checks_both_predecessor_approvals(monkeypatch, wrong_bridge):
    reader, kwargs, _storage, result, _payload, state = completed_replacement_capture(
        monkeypatch, failed_index=0, continuation=4
    )
    context = json.loads(state["artifacts"]["recovery_context"])
    predecessors = [
        context["initial_manifest_sha256"],
        context["ancestor_recovery_context"]["initial_manifest_sha256"],
    ]
    calls = []
    original = kwargs["approval_reader"]

    def read_approval(digest):
        calls.append(digest)
        return (
            state["view"].approval
            if wrong_bridge is not None and digest == predecessors[wrong_bridge]
            else original(digest)
        )

    kwargs["approval_reader"] = read_approval
    if wrong_bridge is None:
        assert reader(**kwargs)["binding"]["manifest_sha256"] == result["manifest_sha256"]
        assert calls == [result["manifest_sha256"], *predecessors]
    else:
        with pytest.raises(ValueError):
            reader(**kwargs)


@pytest.mark.parametrize("mutation", ["stripped_failure", "swapped_owner"])
def test_v4_shared_reader_refuses_resealed_evidence_changes(monkeypatch, mutation):
    reader, kwargs, _storage, result, payload, _state = completed_replacement_capture(
        monkeypatch, failed_index=0, continuation=4
    )
    raw, _ = kwargs["objects"].read_capture(
        payload["artifact_attempt"], payload["stored_artifact"], timeout=30
    )
    artifact = json.loads(raw)
    if mutation == "stripped_failure":
        artifact["creation_evidence"].pop(0)
    else:
        entry = artifact["creation_evidence"][4]
        latest = artifact["creation_evidence"][-1]
        for field in ("manifest_sha256", "consumption_id", "execution_name"):
            entry[field] = latest[field]
        job_id = f"oi_v3_snapshot_{latest['manifest_sha256']}_enriched_content"
        entry["native_job"]["jobReference"]["jobId"] = job_id
        payload["creation_records"][3].update(
            job_id=job_id, native_job_digest=canonical_digest(entry["native_job"])
        )
    raw, attempt = kwargs["objects"].prepare_capture(
        artifact, initial_manifest_sha256=artifact["initial_manifest_sha256"]
    )
    stored = kwargs["objects"].store_capture(raw, attempt, timeout=30)
    payload.update(artifact_attempt=attempt, stored_artifact=stored)
    reseal_result(result, payload)
    kwargs.update(result_id=result["result_id"], result_digest=result["result_digest"])
    with pytest.raises(ValueError):
        reader(**kwargs)


@pytest.mark.parametrize(
    "mutation",
    ["strip_failed_entry", "preserved_owner", "strip_ancestor_context", "wrong_ancestor_hash"],
)
def test_v3_shared_reader_rejects_resealed_ancestry_and_owner_tampering(monkeypatch, mutation):
    from tests.unit.test_open_intelligence_execution_approval_runtime import (
        retained_capture_view,
    )

    reader, kwargs, storage_http, result, payload, state = completed_replacement_capture(
        monkeypatch, failed_index=0, continuation=True
    )
    raw, _ = kwargs["objects"].read_capture(
        payload["artifact_attempt"], payload["stored_artifact"], timeout=30
    )
    artifact = json.loads(raw)
    if mutation == "strip_failed_entry":
        artifact["creation_evidence"].pop(0)
    elif mutation == "preserved_owner":
        entry = artifact["creation_evidence"][1]
        entry.update(
            manifest_sha256=result["manifest_sha256"],
            consumption_id=result["consumption_id"],
            execution_name=result["execution_name"],
        )
        job_id = f"oi_v3_snapshot_{result['manifest_sha256']}_event_ledger"
        entry["native_job"]["jobReference"]["jobId"] = job_id
        payload["creation_records"][0].update(
            job_id=job_id, native_job_digest=canonical_digest(entry["native_job"])
        )
    else:
        context = json.loads(state["artifacts"]["recovery_context"])
        baseline_reader = kwargs["result_reader"]
        if mutation == "strip_ancestor_context":
            context.pop("ancestor_recovery_context")
        else:
            ancestor_context = context["ancestor_recovery_context"]
            ancestor = copy.deepcopy(baseline_reader(ancestor_context["initial_consumption_id"])[0])
            ancestor["result_reference"] += "-alternate"
            ancestor["result_id"] = execution_approval.result_id(
                ancestor["consumption_id"],
                ancestor["result_reference"],
                ancestor["result_digest"],
                ancestor["status"],
                ancestor["completed_at"],
            )
            ancestor_context["initial_result_id"] = ancestor["result_id"]
            previous_reader = baseline_reader

            def baseline_reader(cid):
                return [ancestor] if cid == ancestor["consumption_id"] else previous_reader(cid)

        artifacts = {**state["artifacts"], "recovery_context": canonical_bytes(context)}
        view = retained_capture_view(artifacts=artifacts, mode="recover")
        consumption = view.consumption
        previous_manifest = result["manifest_sha256"]
        for index, entry in enumerate(artifact["creation_evidence"][1:]):
            if entry["manifest_sha256"] == previous_manifest:
                entry.update(
                    manifest_sha256=consumption.manifest_sha256,
                    consumption_id=consumption.consumption_id,
                    execution_name=consumption.execution_name,
                )
                job_id = f"oi_v3_snapshot_{consumption.manifest_sha256}_{entry['lane']}"
                entry["native_job"]["jobReference"]["jobId"] = job_id
                payload["creation_records"][index].update(
                    job_id=job_id, native_job_digest=canonical_digest(entry["native_job"])
                )
        result.update(
            manifest_sha256=consumption.manifest_sha256,
            consumption_id=consumption.consumption_id,
            approval_id=consumption.approval_id,
            execution_name=consumption.execution_name,
        )
        kwargs.update(
            manifest_sha256=consumption.manifest_sha256, consumption_id=consumption.consumption_id
        )
        old_approval_reader = kwargs["approval_reader"]
        kwargs["approval_reader"] = lambda digest: (
            view.approval if digest == consumption.manifest_sha256 else old_approval_reader(digest)
        )
        kwargs["result_reader"] = lambda cid: (
            [result] if cid == consumption.consumption_id else baseline_reader(cid)
        )
        storage_http.seed(
            f"inputs/{canonical_digest(context)}/recovery_context.json",
            artifacts["recovery_context"],
        )
    raw, attempt = kwargs["objects"].prepare_capture(
        artifact, initial_manifest_sha256=artifact["initial_manifest_sha256"]
    )
    stored = kwargs["objects"].store_capture(raw, attempt, timeout=30)
    payload.update(artifact_attempt=attempt, stored_artifact=stored)
    reseal_result(result, payload)
    kwargs.update(result_id=result["result_id"], result_digest=result["result_digest"])
    with pytest.raises(ValueError):
        reader(**kwargs)


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "missing_failure",
        "lane",
        "owner",
        "running",
        "success",
        "metadata",
        "replacement_owner",
        "prefix_owner",
        "extra_failure",
        "strip_and_rewrite",
    ],
)
def test_shared_reader_independently_validates_failed_creation_prefix(monkeypatch, mutation):
    reader, kwargs, _storage_http, result, payload = completed_replacement_capture(monkeypatch)
    raw, _ = kwargs["objects"].read_capture(
        payload["artifact_attempt"], payload["stored_artifact"], timeout=30
    )
    artifact = json.loads(raw)
    if mutation is None:
        assert reader(**kwargs)["binding"]["manifest_sha256"] == result["manifest_sha256"]
        return
    failed = artifact["creation_evidence"][0]
    if mutation == "strip_and_rewrite":
        artifact["creation_evidence"].pop(0)
        entry = artifact["creation_evidence"][2]
        for field in ("manifest_sha256", "consumption_id", "execution_name"):
            entry[field] = failed[field]
        job_id = failed["native_job"]["jobReference"]["jobId"]
        entry["native_job"]["jobReference"]["jobId"] = job_id
        payload["creation_records"][2].update(
            job_id=job_id, native_job_digest=canonical_digest(entry["native_job"])
        )
    elif mutation == "missing_failure":
        artifact["creation_evidence"].pop(0)
    elif mutation == "lane":
        failed["lane"] = "raw_content"
    elif mutation == "owner":
        failed["manifest_sha256"] = result["manifest_sha256"]
    elif mutation == "running":
        failed["native_job"]["status"]["state"] = "RUNNING"
    elif mutation == "success":
        failed["native_job"]["status"].pop("errorResult")
    elif mutation == "metadata":
        failed["snapshot_metadata"] = {}
    elif mutation == "extra_failure":
        artifact["creation_evidence"].insert(0, copy.deepcopy(failed))
    else:
        index = 3 if mutation == "replacement_owner" else 1
        entry = artifact["creation_evidence"][index]
        other = failed if mutation == "replacement_owner" else artifact["creation_evidence"][-1]
        for field in ("manifest_sha256", "consumption_id", "execution_name"):
            entry[field] = other[field]
    raw, attempt = kwargs["objects"].prepare_capture(
        artifact, initial_manifest_sha256=artifact["initial_manifest_sha256"]
    )
    stored = kwargs["objects"].store_capture(raw, attempt, timeout=30)
    payload.update(artifact_attempt=attempt, stored_artifact=stored)
    reseal_result(result, payload)
    kwargs.update(result_id=result["result_id"], result_digest=result["result_digest"])
    with pytest.raises(ValueError):
        reader(**kwargs)


@pytest.mark.parametrize("private", [False, True])
def test_creation_diagnostic_preserves_safe_reason_with_unchanged_result_missing_check(
    monkeypatch, private
):
    http, _storage_http, args, _view = capture_fixture.joined(monkeypatch)
    events = []

    def mutate(native):
        if private:
            raise ValueError("snapshot_creation_private secret text")
        native["configuration"]["query"]["destinationTable"] = {"tableId": "foreign"}
        return native

    http.creation.mutate_job = mutate
    payload, status = capture_fixture.operation_fixture.subject()._execute_validated_operation(
        **args, diagnostics=events.append
    )
    assert status == "failed"
    assert payload["missing_checks"] == ["snapshot_creation_incomplete"]
    assert events[-1]["code"] == (
        "snapshot_creation_incomplete" if private else "snapshot_creation_job_invalid"
    )
    assert "secret text" not in json.dumps(events)


def test_protected_capture_default_readers_are_the_historical_replay_readers(monkeypatch):
    """_read_protected_capture binds its default result and approval readers to the retained
    v1 record literals under historical_replay, the same binding the capture command builds
    for its partials; a reader called without version and mode is not reachable, which the
    keyword only spies enforce. The replay half of every test in this suite that reads
    through the default readers depends on this binding."""
    reader, kwargs, _storage_http, result, _payload = completed_replacement_capture(monkeypatch)
    bound = []

    def default_result_reader(consumption_id, *, version, mode, **_options):
        bound.append(("result", version, mode))
        return kwargs["result_reader"](consumption_id)

    def default_approval_reader(manifest_sha256, *, version, mode, **_options):
        bound.append(("approval", version, mode))
        return kwargs["approval_reader"](manifest_sha256)

    monkeypatch.setattr(execution_approval, "_default_result_reader", default_result_reader)
    monkeypatch.setattr(execution_approval, "_default_approval_reader", default_approval_reader)
    arguments = {
        key: value
        for key, value in kwargs.items()
        if key not in {"result_reader", "approval_reader"}
    }
    value = reader(**arguments)
    assert value["capture"]["assembly"]["source_authority"] is False
    assert value["binding"]["result_digest"] == result["result_digest"]
    assert bound[0] == ("result", execution_approval._RESULT_VERSION, "historical_replay")
    assert bound[1] == ("approval", execution_approval._APPROVAL_VERSION, "historical_replay")
    assert {entry[2] for entry in bound} == {"historical_replay"}
    assert all(entry[1].endswith("_v1") for entry in bound)
