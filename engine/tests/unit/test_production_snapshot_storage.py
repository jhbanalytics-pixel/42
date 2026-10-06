import copy
import hashlib
import io
import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import requests
import urllib3
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage
from google.cloud.storage import blob as storage_blob
from src.analysis.open_intelligence import production_snapshot_storage as subject

from tests.unit import test_production_snapshot_capture as capture_fixture

BUCKET = "ogilvy-trends-v2-oi-source-artifacts-staging"
MANIFEST = "a" * 64
CAPTURED_AT = "2026-09-08T00:00:30+00:00"
CREATED_AT = "2026-09-08T00:01:00.000Z"
LANES = ("event_ledger", "seed_graph", "seed_candidates", "enriched_content", "raw_content")


def bucket_metadata():
    return {
        "name": BUCKET,
        "location": "US",
        "iamConfiguration": {
            "uniformBucketLevelAccess": {"enabled": True},
            "publicAccessPrevention": "enforced",
        },
        "lifecycle": {
            "rule": [
                {
                    "action": {"type": "Delete"},
                    "condition": {"age": 90, "matchesPrefix": ["captures/"]},
                }
            ]
        },
        "softDeletePolicy": {"retentionDurationSeconds": "604800"},
    }


def artifact():
    return {
        "contract_version": "open_intelligence_protected_source_capture_artifact_v1",
        "initial_manifest_sha256": MANIFEST,
        "capture_plan": {"contract_version": "open_intelligence_protected_capture_plan_v1"},
        "capture": {
            "assembly": {
                "snapshot": {"captured_at": CAPTURED_AT},
            }
        },
        "creation_evidence": [{"lane": lane} for lane in LANES],
    }


class HTTP:
    is_mtls = False

    def __init__(self):
        self.calls = []
        self.objects = {}
        self.counter = 0
        self.metadata = bucket_metadata()
        self.next_upload_raw = None
        self.lose_upload_ack = False

    def seed(self, name, raw, *, generation=None, created=CREATED_AT):
        self.counter = max(self.counter + 1, generation or 0)
        generation = generation or self.counter
        self.objects[(name, generation)] = {
            "raw": raw,
            "generation": generation,
            "created": created,
        }
        self.objects[name] = self.objects[(name, generation)]
        return generation

    @staticmethod
    def response(method, url, status, content, content_type="application/json"):
        response = requests.Response()
        response.status_code = status
        response.request = requests.Request(method, url).prepare()
        response._content = content
        response.headers["Content-Type"] = content_type
        response.headers["Content-Length"] = str(len(content))
        response.raw = urllib3.response.HTTPResponse(
            body=io.BytesIO(content),
            headers=response.headers,
            status=status,
            preload_content=False,
        )
        return response

    def object_json(self, name, item):
        return {
            "bucket": BUCKET,
            "name": name,
            "generation": str(item["generation"]),
            "size": str(len(item["raw"])),
            "timeCreated": item["created"],
        }

    def request(self, method, url, data=None, headers=None, **kwargs):
        self.calls.append((method, url, data, headers, kwargs))
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if "/upload/storage/" in parsed.path:
            assert query["ifGenerationMatch"] == ["0"]
            match = re.search(rb'"name"\s*:\s*"([^"]+)"', bytes(data))
            assert match is not None
            name = match.group(1).decode()
            if name in self.objects:
                return self.response(method, url, 412, b'{"error":{"code":412}}')
            assert self.next_upload_raw is not None
            assert self.next_upload_raw in bytes(data)
            generation = self.seed(name, self.next_upload_raw)
            body = json.dumps(self.object_json(name, self.objects[(name, generation)])).encode()
            if self.lose_upload_ack:
                self.lose_upload_ack = False
                return self.response(method, url, 503, b'{"error":{"code":503}}')
            return self.response(method, url, 200, body)
        if parsed.path.endswith("/b/" + BUCKET):
            return self.response(method, url, 200, json.dumps(self.metadata).encode())
        encoded = parsed.path.split("/o/", 1)[1]
        name = unquote(encoded)
        generation = int(query["generation"][0]) if "generation" in query else None
        item = (
            self.objects.get((name, generation))
            if generation is not None
            else self.objects.get(name)
        )
        if item is None:
            return self.response(method, url, 404, b'{"error":{"code":404}}')
        if query.get("alt") == ["media"]:
            return self.response(method, url, 200, item["raw"], "application/octet-stream")
        return self.response(method, url, 200, json.dumps(self.object_json(name, item)).encode())


class ResumableHTTP(HTTP):
    def __init__(self):
        super().__init__()
        self.upload_name = None
        self.uploaded = bytearray()
        self.progress_responses = 0
        self.created_at = "2030-01-03T00:06:00.000Z"
        self.lost_final_acknowledgements = 0

    def request(self, method, url, data=None, headers=None, **kwargs):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if method == "POST" and query.get("uploadType") == ["resumable"]:
            self.calls.append((method, url, data, headers, kwargs))
            assert query["ifGenerationMatch"] == ["0"]
            metadata = json.loads(bytes(data))
            self.upload_name = metadata["name"]
            session = (
                f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o"
                "?upload_id=synthetic-session"
            )
            response = self.response(method, url, 200, b"")
            response.headers["Location"] = session
            return response
        if method == "PUT" and query.get("upload_id") == ["synthetic-session"]:
            self.calls.append((method, url, data, headers, kwargs))
            assert self.upload_name is not None
            self.uploaded.extend(bytes(data))
            assert self.next_upload_raw is not None
            if len(self.uploaded) < len(self.next_upload_raw):
                self.progress_responses += 1
                response = self.response(method, url, 308, b"")
                response.headers["Range"] = f"bytes=0-{len(self.uploaded) - 1}"
                assert "Location" not in response.headers
                return response
            assert bytes(self.uploaded) == self.next_upload_raw
            generation = self.seed(self.upload_name, bytes(self.uploaded), created=self.created_at)
            body = json.dumps(
                self.object_json(self.upload_name, self.objects[(self.upload_name, generation)])
            ).encode()
            if self.lose_upload_ack:
                self.lose_upload_ack = False
                self.lost_final_acknowledgements += 1
                return self.response(method, url, 503, b'{"error":{"code":503}}')
            return self.response(method, url, 200, body)
        return super().request(method, url, data=data, headers=headers, **kwargs)


def objects(http=None):
    http = http or HTTP()
    client = storage.Client(
        project="ogilvy-trends-v2", credentials=AnonymousCredentials(), _http=http
    )
    # The storage client warms its bucket metadata cache from a daemon thread on the
    # first blob operation, which lands a GET in the recorded calls at a scheduling
    # dependent moment; the fixture disables the cache so call counts are exact.
    client._bucket_metadata_cache = None
    return subject.SourceCaptureObjects(client.bucket(BUCKET)), http


def prepared(service):
    return service.prepare_capture(artifact(), initial_manifest_sha256=MANIFEST)


def test_preflight_requires_native_private_us_bucket_and_exact_output_expiry():
    service, http = objects()
    assert service.preflight(timeout=5) is None
    assert len(http.calls) == 1
    assert http.calls[0][0] == "GET"
    assert http.calls[0][4]["timeout"] == 5


@pytest.mark.parametrize(
    "mutation", ["location", "ubla", "pap", "retention", "age", "prefix", "early_delete"]
)
def test_preflight_refuses_policy_drift_without_writing(mutation):
    service, http = objects()
    if mutation == "location":
        http.metadata["location"] = "EU"
    elif mutation == "ubla":
        http.metadata["iamConfiguration"]["uniformBucketLevelAccess"]["enabled"] = False
    elif mutation == "pap":
        http.metadata["iamConfiguration"]["publicAccessPrevention"] = "inherited"
    elif mutation == "retention":
        http.metadata["retentionPolicy"] = {"isLocked": True}
    elif mutation == "age":
        http.metadata["lifecycle"]["rule"][0]["condition"]["age"] = 91
    elif mutation == "prefix":
        http.metadata["lifecycle"]["rule"][0]["condition"]["matchesPrefix"] = ["inputs/"]
    else:
        http.metadata["lifecycle"]["rule"].append(
            {"action": {"type": "Delete"}, "condition": {"age": 30}}
        )
    with pytest.raises(subject.SourceCaptureStorageError, match="storage_preflight_invalid"):
        service.preflight(timeout=5)
    assert all(call[0] == "GET" for call in http.calls)


def test_read_input_preserves_exact_markdown_bytes_despite_json_suffix():
    service, http = objects()
    raw = b"# Synthetic capture contract\n\nExact markdown bytes.\n"
    digest = hashlib.sha256(raw).hexdigest()
    name = f"inputs/{digest}/capture_contract.json"
    http.seed(name, raw)

    assert service.read_input("capture_contract", digest, timeout=5) == raw
    download = next(call for call in http.calls if "alt=media" in call[1])
    assert "ifGenerationMatch=" in download[1]


def test_prepare_capture_uses_canonical_bytes_and_fixed_content_address():
    service, _http = objects()
    raw, attempt = prepared(service)
    expected = json.dumps(
        artifact(), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(expected).hexdigest()
    assert raw == expected
    assert attempt == {
        "uri": f"gs://{BUCKET}/captures/{MANIFEST}/{digest}/capture.json",
        "size_bytes": len(raw),
        "sha256": digest,
        "captured_at": CAPTURED_AT,
    }


def test_prepare_capture_accepts_actual_validated_collector_output(monkeypatch):
    service, _http = objects()
    captured = capture_fixture.validate_capture(
        capture_fixture.run(monkeypatch, capture_fixture.HTTP())
    )
    value = artifact()
    value["capture"] = captured

    raw, attempt = service.prepare_capture(value, initial_manifest_sha256=MANIFEST)

    assert json.loads(raw)["capture"] == captured
    assert attempt["captured_at"] == captured["assembly"]["snapshot"]["captured_at"]


def test_actual_storage_sdk_create_only_upload_and_exact_generation_readback():
    service, http = objects()
    raw, attempt = prepared(service)
    http.next_upload_raw = raw

    stored = service.store_capture(raw, attempt, timeout=5)
    recovered_raw, recovered = service.read_capture(attempt, stored_artifact=stored, timeout=5)

    assert recovered_raw == raw
    assert recovered == stored
    assert stored == {
        "uri": attempt["uri"],
        "generation": 1,
        "size_bytes": len(raw),
        "sha256": attempt["sha256"],
        "created_at": "2026-09-08T00:01:00+00:00",
    }
    posts = [call for call in http.calls if call[0] == "POST"]
    assert len(posts) == 1
    assert "ifGenerationMatch=0" in posts[0][1]


def test_unknown_upload_ack_reconciles_exact_object_without_second_upload():
    service, http = objects()
    raw, attempt = prepared(service)
    http.next_upload_raw = raw
    http.lose_upload_ack = True

    stored = service.store_capture(raw, attempt, timeout=5)

    assert stored["sha256"] == attempt["sha256"]
    assert len([call for call in http.calls if call[0] == "POST"]) == 1


def test_real_sdk_resumable_upload_accepts_progress_and_reconciles_lost_final_ack(
    monkeypatch,
):
    capture_http = capture_fixture.HTTP()
    capture_http.rows["enriched_content"][0]["text"] = "x" * (9 * 1024 * 1024)
    captured = capture_fixture.validate_capture(capture_fixture.run(monkeypatch, capture_http))
    value = artifact()
    value["capture"] = captured
    resumable = ResumableHTTP()
    service, _http = objects(resumable)
    raw, attempt = service.prepare_capture(value, initial_manifest_sha256=MANIFEST)
    assert len(raw) > storage_blob._MAX_MULTIPART_SIZE
    resumable.next_upload_raw = raw
    resumable.lose_upload_ack = True
    monkeypatch.setattr(storage_blob, "_DEFAULT_CHUNKSIZE", 4 * 1024 * 1024)
    bigquery_client = SimpleNamespace(_http=SimpleNamespace(request=lambda *args, **kwargs: None))

    from scripts.staging.capture_protected_production_snapshot import _operation_transport

    with _operation_transport(bigquery_client, service):
        stored = service.store_capture(raw, attempt, timeout=30)

    assert stored["sha256"] == attempt["sha256"]
    assert resumable.progress_responses >= 1
    assert (
        len(
            [
                call
                for call in resumable.calls
                if call[0] == "POST" and "uploadType=resumable" in call[1]
            ]
        )
        == 1
    )
    puts = [call for call in resumable.calls if call[0] == "PUT" and "upload_id=" in call[1]]
    assert len(puts) == (len(raw) + 4 * 1024 * 1024 - 1) // (4 * 1024 * 1024)
    assert all(
        urlparse(call[1]).netloc == "storage.googleapis.com"
        and parse_qs(urlparse(call[1]).query) == {"upload_id": ["synthetic-session"]}
        for call in puts
    )
    assert resumable.lost_final_acknowledgements == 1


def test_existing_conflicting_object_refuses_and_is_never_overwritten():
    service, http = objects()
    raw, attempt = prepared(service)
    name = attempt["uri"].removeprefix(f"gs://{BUCKET}/")
    http.seed(name, b'{"different":true}')
    http.next_upload_raw = raw

    with pytest.raises(subject.SourceCaptureStorageError, match="artifact_conflict"):
        service.store_capture(raw, attempt, timeout=5)
    assert len([call for call in http.calls if call[0] == "POST"]) == 1


def test_readback_refuses_content_copied_under_a_different_manifest_prefix():
    service, http = objects()
    raw, attempt = prepared(service)
    foreign_manifest = "b" * 64
    foreign = {
        **attempt,
        "uri": attempt["uri"].replace(MANIFEST, foreign_manifest),
    }
    name = foreign["uri"].removeprefix(f"gs://{BUCKET}/")
    http.seed(name, raw)

    with pytest.raises(subject.SourceCaptureStorageError, match="artifact_conflict"):
        service.read_capture(foreign, timeout=5)


def test_read_capture_refuses_wrong_generation_hash_size_or_creation_time():
    service, http = objects()
    raw, attempt = prepared(service)
    http.next_upload_raw = raw
    stored = service.store_capture(raw, attempt, timeout=5)
    for field, value in (
        ("generation", stored["generation"] + 1),
        ("sha256", "f" * 64),
        ("size_bytes", stored["size_bytes"] + 1),
        ("created_at", "2026-09-08T00:02:00+00:00"),
    ):
        changed = {**stored, field: value}
        with pytest.raises(subject.SourceCaptureStorageError):
            service.read_capture(attempt, stored_artifact=changed, timeout=5)


@pytest.mark.parametrize(
    "change",
    [
        "manifest",
        "contract",
        "fields",
        "timestamp",
        "missing_timestamp",
        "nan",
        "non_json",
        "empty",
    ],
)
def test_invalid_capture_artifact_refuses_before_upload(change):
    service, http = objects()
    value = artifact()
    manifest = MANIFEST
    if change == "manifest":
        manifest = "b" * 64
    elif change == "contract":
        value["contract_version"] = "other"
    elif change == "fields":
        value["extra"] = None
    elif change == "timestamp":
        value["capture"]["assembly"]["snapshot"]["captured_at"] = "2026-09-08T02:00:30+02:00"
    elif change == "missing_timestamp":
        value["capture"]["assembly"]["snapshot"].pop("captured_at")
    elif change == "nan":
        value["capture"]["value"] = float("nan")
    elif change == "non_json":
        value["capture"]["value"] = {"bad"}
    else:
        value = {}
    with pytest.raises(subject.SourceCaptureStorageError, match="artifact_invalid"):
        service.prepare_capture(value, initial_manifest_sha256=manifest)
    assert not http.calls


def test_closed_input_names_digests_limits_and_bucket_are_enforced(monkeypatch):
    service, http = objects()
    with pytest.raises(subject.SourceCaptureStorageError):
        service.read_input("other", "a" * 64, timeout=5)
    with pytest.raises(subject.SourceCaptureStorageError):
        service.read_input("capture_plan", "bad", timeout=5)
    with pytest.raises(subject.SourceCaptureStorageError):
        subject.SourceCaptureObjects(storage.Client.create_anonymous_client().bucket("other"))
    monkeypatch.setattr(subject, "MAX_ARTIFACT_BYTES", 32)
    with pytest.raises(subject.SourceCaptureStorageError, match="artifact_too_large"):
        prepared(service)
    assert not http.calls
