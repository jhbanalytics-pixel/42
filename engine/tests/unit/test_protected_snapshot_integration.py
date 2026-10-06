import ast
import copy
import hashlib
import json
import socket
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests
from google.cloud import bigquery
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.production_snapshot import _deserialize_plan
from src.analysis.open_intelligence.production_snapshot_capture import (
    validate_captured_production_snapshot,
)

from tests.unit import test_pipeline_production_snapshot as semantic_fixture
from tests.unit import test_production_snapshot_capture as capture_fixture
from tests.unit import test_production_snapshot_storage as storage_fixture
from tests.unit import test_production_snapshot_tables as creation_fixture
from tests.unit import test_protected_production_snapshot as operation_fixture
from tests.unit.test_open_intelligence_execution_approval_runtime import (
    SOURCE_SNAPSHOT_NOW,
    _consumed_authority,
    retained_capture_view,
)

OPERATION_NOW = SOURCE_SNAPSHOT_NOW + timedelta(seconds=2)
OBJECT_CREATED = SOURCE_SNAPSHOT_NOW + timedelta(seconds=3)
PAYLOAD_FIELDS = {
    "contract_version",
    "cutoff_date",
    "client_scope_id",
    "market_scope",
    "source_as_of",
    "captured_at",
    "snapshot_plan_digest",
    "snapshot_digest",
    "capture_receipt_digest",
    "creation_records",
    "artifact_attempt",
    "stored_artifact",
    "query_count",
    "total_bytes_billed",
    "limitations",
    "missing_checks",
}


class JoinedBigQueryHTTP:
    is_mtls = False

    def __init__(self, plan, physical_rows):
        self.plan = plan
        self.creation = creation_fixture.CreationHTTP(plan)
        self.capture = capture_fixture.HTTP()
        self.capture.rows = physical_rows

    def request(self, method, url, **kwargs):
        path = urlparse(url).path
        data = json.loads(kwargs["data"]) if kwargs.get("data") else None
        if "/tables/" in path:
            lane = next(lane for lane in self.capture.rows if path.endswith("_" + lane))
            resource = creation_fixture.snapshot_metadata(self.plan)[lane]
            resource.update(
                location="US",
                numRows=str(len(self.capture.rows[lane])),
                numBytes="1024",
                creationTime=str(int(OPERATION_NOW.timestamp() * 1000)),
                expirationTime=str(
                    int((self.plan.source_as_of + timedelta(days=90)).timestamp() * 1000)
                ),
            )
            return storage_fixture.HTTP.response(method, url, 200, canonical_bytes(resource))
        if "/jobs/oi_v3_snapshot_" in path or (
            method == "POST"
            and data["configuration"]["query"]["query"].startswith("CREATE SNAPSHOT TABLE")
        ):
            return self.creation.request(method, url, **kwargs)
        return self.capture.request(method, url, **kwargs)


class JoinedStorageHTTP(storage_fixture.HTTP):
    def __init__(self, *, lose_ack=False, hide_readback=False):
        super().__init__()
        self.lose_upload_ack = lose_ack
        self.hide_readback = hide_readback

    def seed(self, name, raw, *, generation=None, created=None):
        return super().seed(
            name,
            raw,
            generation=generation,
            created=OBJECT_CREATED.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        )

    def request(self, method, url, data=None, headers=None, **kwargs):
        if "/upload/storage/" in urlparse(url).path:
            content_type = next(
                value for key, value in headers.items() if key.lower() == "content-type"
            )
            if isinstance(content_type, str):
                content_type = content_type.encode()
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + content_type + b"\r\nMIME-Version: 1.0\r\n\r\n" + bytes(data)
            )
            parts = list(message.iter_parts())
            assert len(parts) == 2
            self.next_upload_raw = parts[1].get_payload(decode=True)
            assert canonical_bytes(json.loads(self.next_upload_raw)) == self.next_upload_raw
        elif self.hide_readback and "/o/" in urlparse(url).path:
            self.calls.append((method, url, data, headers, kwargs))
            return self.response(method, url, 404, b'{"error":{"code":404}}')
        return super().request(method, url, data=data, headers=headers, **kwargs)


def joined(monkeypatch, *, lose_ack=False, hide_readback=False):
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network prohibited"))
    monkeypatch.setattr(operation_fixture, "NOW", OPERATION_NOW)
    artifacts = operation_fixture.artifacts()
    plan = _deserialize_plan(json.loads(artifacts["capture_plan"])["snapshot_plan"])
    physical_rows = copy.deepcopy(
        semantic_fixture.prepared()["row_material"]["physical_rows_by_table"]
    )
    for lane, rows in physical_rows.items():
        schema = semantic_fixture.rows_fixture.field_schema(lane)
        for row in rows:
            for name, field in schema.items():
                if row.get(name) is not None and field["type"] == "DATE":
                    row[name] = operation_fixture.CUTOFF
                elif row.get(name) is not None and field["type"] == "TIMESTAMP":
                    row[name] = plan.source_as_of - timedelta(hours=1)
    http = JoinedBigQueryHTTP(plan, physical_rows)
    monkeypatch.setattr(
        capture_fixture, "CREATOR", creation_fixture.CreationCredentials.service_account_email
    )
    monkeypatch.setattr(capture_fixture.fixture.row_fixture, "CAPTURED_AT", OPERATION_NOW)
    monkeypatch.setattr(capture_fixture.module(), "_utc_now", lambda: OPERATION_NOW)
    monkeypatch.setattr(capture_fixture.module(), "monotonic", lambda: 0)
    view = retained_capture_view(artifacts=artifacts)
    recheck, claim = creation_fixture.retained_closures(view)
    client = bigquery.Client(
        project=creation_fixture.subject.PROJECT,
        location="US",
        credentials=creation_fixture.CreationCredentials(),
        _http=http,
    )
    storage_http = JoinedStorageHTTP(lose_ack=lose_ack, hide_readback=hide_readback)
    objects, _ = storage_fixture.objects(storage_http)
    return (
        http,
        storage_http,
        {
            "artifacts": artifacts,
            "cutoff": operation_fixture.CUTOFF,
            "mode": "initial",
            "manifest": view.manifest,
            "consumption": view.consumption,
            "recheck": recheck,
            "claim": claim,
            "client": client,
            "objects": objects,
            "now": OPERATION_NOW,
        },
        view,
    )


@pytest.mark.parametrize("lose_ack", [False, True])
def test_real_joined_initial_capture_stores_exact_private_generation(monkeypatch, lose_ack):
    http, storage_http, args, _view = joined(monkeypatch, lose_ack=lose_ack)
    payload, status = operation_fixture.subject()._execute_validated_operation(**args)
    assert status == "succeeded", payload["missing_checks"]
    assert set(payload) == PAYLOAD_FIELDS
    assert payload["missing_checks"] == []
    assert len(payload["creation_records"]) == 5
    assert all(record["state"] == "succeeded" for record in payload["creation_records"])
    assert len([call for call in http.creation.calls if call[0] == "POST"]) == 5
    selects = [call for call in http.capture.calls if call[0] == "POST"]
    assert len(selects) == payload["query_count"] == 4
    assert payload["total_bytes_billed"] == 400
    assert len([call for call in storage_http.calls if call[0] == "POST"]) == 1
    stored = payload["stored_artifact"]
    assert stored["generation"] == 1
    assert stored["created_at"] == OBJECT_CREATED.isoformat()
    assert payload["captured_at"] == OPERATION_NOW.isoformat()
    raw, reloaded = args["objects"].read_capture(payload["artifact_attempt"], stored, timeout=5)
    assert reloaded == stored
    assert hashlib.sha256(raw).hexdigest() == stored["sha256"]
    assert len(raw) == stored["size_bytes"]
    artifact = json.loads(raw)
    assert artifact["initial_manifest_sha256"] == args["consumption"].manifest_sha256
    assert artifact["capture_plan"] == json.loads(args["artifacts"]["capture_plan"])
    capture = validate_captured_production_snapshot(
        artifact["capture"],
        cutoff_date=args["cutoff"],
        client_scope_id="ogilvy_default",
        market_scope=("ke", "ng", "za"),
        reviewed_metadata=args["artifacts"]["source_metadata"],
        expected_creator_email=creation_fixture.CreationCredentials.service_account_email,
    )
    assert capture == artifact["capture"]
    assert canonical_digest(capture) == payload["capture_receipt_digest"]
    assert capture["execution_authority"] is False
    assert capture["assembly"]["source_authority"] is False
    assert capture["assembly"]["snapshot"]["source_digest"] == payload["snapshot_digest"]
    assert len(capture["assembly"]["row_material"]["resolved_refs"]) == 2
    assert len(artifact["creation_evidence"]) == 5


def test_joined_unconsumed_authority_refuses_before_any_io(monkeypatch):
    """The public entry keeps its guard. Strengthened under the seam: a real issued and
    consumed v2 authority of another operation refuses execution_approval_identity_invalid
    at _require_consumed_execution, whether it carries an unbound consumption object or its
    own consumption, before any transport sees a call."""
    http, storage_http, args, _view = joined(monkeypatch)
    foreign, foreign_consumption = _consumed_authority("brain_read")
    entry = {
        key: value for key, value in args.items() if key not in {"manifest", "recheck", "claim"}
    }
    for consumption in (object(), foreign_consumption):
        entry["consumption"] = consumption
        with pytest.raises(ValueError, match=r"^execution_approval_identity_invalid$"):
            operation_fixture.subject()._run_operation(**entry, authority=foreign)
    assert http.creation.calls == []
    assert http.capture.calls == []
    assert storage_http.calls == []


def test_joined_unknown_storage_ack_preserves_attempt_and_known_capture(monkeypatch):
    http, storage_http, args, _view = joined(monkeypatch, lose_ack=True, hide_readback=True)
    payload, status = operation_fixture.subject()._execute_validated_operation(**args)
    assert status == "failed"
    assert set(payload) == PAYLOAD_FIELDS
    assert payload["missing_checks"] == ["snapshot_artifact_unavailable"]
    assert payload["stored_artifact"] is None
    assert payload["artifact_attempt"]["captured_at"] == OPERATION_NOW.isoformat()
    assert payload["capture_receipt_digest"] is not None
    assert payload["query_count"] == 4
    assert payload["total_bytes_billed"] == 400
    assert len([call for call in storage_http.calls if call[0] == "POST"]) == 1
    assert len([call for call in http.creation.calls if call[0] == "POST"]) == 5
    assert len([call for call in http.capture.calls if call[0] == "POST"]) == 4


@pytest.mark.parametrize("phase", ["capture", "storage"])
def test_source_bearing_requests_never_follow_foreign_redirects(monkeypatch, phase):
    http, storage_http, args, _view = joined(monkeypatch)
    sent = []

    class RedirectAdapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            sent.append(request.url)
            response = requests.Response()
            response.request = request
            response._content = b'{"error":{"code":403,"message":"synthetic refusal"}}'
            if urlparse(request.url).hostname == "foreign.invalid":
                response.status_code = 403
            else:
                response.status_code = 307
                response.headers["location"] = "https://foreign.invalid/leak"
            return response

        def close(self):
            pass

    session = requests.Session()
    session.mount("https://", RedirectAdapter())
    target = http.capture if phase == "capture" else storage_http
    original = target.request

    def redirect_post(method, url, **kwargs):
        if method == "POST":
            return session.request(method, url, **kwargs)
        return original(method, url, **kwargs)

    monkeypatch.setattr(target, "request", redirect_post)
    payload, status = operation_fixture.subject()._execute_validated_operation(**args)
    assert status == "failed"
    assert payload["stored_artifact"] is None
    assert sent
    assert all(urlparse(url).hostname != "foreign.invalid" for url in sent)


def test_runner_entries_guard_before_io_and_the_validated_body_is_private():
    """Source level audit of the capture runner in the idiom of the tables audit. The
    guarded entries _run_operation and _execute_operation call _require_consumed_execution
    on the live authority before anything else; _run_operation builds the recheck and claim
    closures over that authority and hands them down; _execute_operation hands only the
    manifest, the consumption and the closures to the validated body; the validated body
    and the operation body it wraps in the transport guard name no authority, no invocation
    registry and no live guard, call the tables validated body exactly once with the
    manifest, the consumption and both closures, and are referenced by no other engine
    module; main() still wires the guarded entry as the operation runner."""
    module = operation_fixture.subject()
    module_path = Path(module.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}

    def call_name(node):
        target = node.func
        return target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")

    def calls(node):
        return [call_name(item) for item in ast.walk(node) if isinstance(item, ast.Call)]

    def top_level_calls(function):
        return [
            call_name(item)
            for statement in function.body
            if not isinstance(statement, ast.FunctionDef)
            for item in ast.walk(statement)
            if isinstance(item, ast.Call)
        ]

    def names(node):
        return {
            item.id if isinstance(item, ast.Name) else item.attr
            for item in ast.walk(node)
            if isinstance(item, (ast.Name, ast.Attribute))
        }

    def parameters(function):
        return [arg.arg for arg in function.args.args + function.args.kwonlyargs]

    def keywords(function, callee):
        call = next(
            item
            for item in ast.walk(function)
            if isinstance(item, ast.Call) and call_name(item) == callee
        )
        return {keyword.arg: keyword.value for keyword in call.keywords}

    run = functions["_run_operation"]
    assert top_level_calls(run)[0] == "_require_consumed_execution"
    assert top_level_calls(run).count("_execute_operation") == 1
    closures = {node.name: node for node in run.body if isinstance(node, ast.FunctionDef)}
    assert set(closures) == {"recheck", "claim"}
    assert calls(closures["recheck"]) == ["_require_consumed_execution"]
    assert {"authority", "_CREATION_INVOCATIONS", "_CREATION_LOCK"} <= names(closures["claim"])
    handed = keywords(run, "_execute_operation")
    assert all(
        isinstance(handed[key], ast.Name) and handed[key].id == key
        for key in ("authority", "consumption", "recheck", "claim")
    )

    execute = functions["_execute_operation"]
    assert top_level_calls(execute)[0] == "_require_consumed_execution"
    assert "authority" in parameters(execute)
    handed = keywords(execute, "_execute_validated_operation")
    assert "authority" not in handed
    assert isinstance(handed["manifest"], ast.Attribute)
    assert handed["manifest"].value.id == "authority"
    assert handed["manifest"].attr == "manifest"
    assert all(
        isinstance(handed[key], ast.Name) and handed[key].id == key
        for key in ("consumption", "recheck", "claim")
    )

    validated = functions["_execute_validated_operation"]
    assert "authority" not in parameters(validated)
    assert isinstance(validated.body[0], ast.With)
    assert calls(validated.body[0].items[0].context_expr) == ["_operation_transport"]
    assert calls(validated.body[0]).count("_validated_operation") == 1

    body = functions["_validated_operation"]
    assert "authority" not in parameters(body)
    assert {"manifest", "consumption", "recheck", "claim"} <= set(parameters(body))
    for function in (validated, body):
        assert not {"authority", "_CREATION_INVOCATIONS", "_CREATION_LOCK"} & names(function)
        assert "_require_consumed_execution" not in calls(function)
    body_calls = calls(body)
    assert body_calls.count("_execute_validated_snapshot_creations") == 1
    assert "execute_protected_snapshot_creations" not in body_calls
    handed = keywords(body, "_execute_validated_snapshot_creations")
    assert all(
        isinstance(handed[key], ast.Name) and handed[key].id == key
        for key in ("manifest", "consumption", "recheck", "claim")
    )
    assert "authority" not in handed

    main_keywords = keywords(functions["main"], "_main_impl")
    assert isinstance(main_keywords["operation_runner"], ast.Name)
    assert main_keywords["operation_runner"].id == "_run_operation"
    assert keywords(functions["_main_impl"], "operation_runner")["authority"].id == "authority"

    engine_root = module_path.parents[2]
    referencing = sorted(
        path.relative_to(engine_root).as_posix()
        for folder in ("src", "scripts")
        for path in (engine_root / folder).rglob("*.py")
        if "_validated_operation" in path.read_text(encoding="utf-8")
    )
    assert referencing == ["scripts/staging/capture_protected_production_snapshot.py"]
