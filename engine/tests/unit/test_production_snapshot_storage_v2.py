import hashlib
import json
from copy import deepcopy

import pytest
from src.analysis.open_intelligence.production_snapshot_storage import SourceCaptureStorageError

from tests.unit.test_production_snapshot_storage import HTTP, MANIFEST, objects
from tests.unit.test_source_estate_bridge_contract import facts, profile

V2 = "open_intelligence_protected_source_capture_artifact_v2"


class Transport(HTTP):
    def seed(self, name, raw, *, generation=None, created="2026-09-21T00:30:00.000Z"):
        return super().seed(name, raw, generation=generation, created=created)


def artifact():
    return {
        "contract_version": V2,
        "initial_manifest_sha256": MANIFEST,
        "capture_plan": {
            "contract_version": "open_intelligence_protected_capture_plan_v3",
            "snapshot_plan": profile(),
        },
        "capture": facts(),
        "creation_evidence": [],
    }


def prepared():
    service, http = objects(Transport())
    raw, attempt = service.prepare_capture(
        artifact(), initial_manifest_sha256=MANIFEST, artifact_version=V2
    )
    http.next_upload_raw = raw
    return service, http, raw, attempt


@pytest.mark.parametrize("lost_ack", [False, True])
def test_actual_sdk_v2_store_read_and_recovery(lost_ack):
    service, http, raw, attempt = prepared()
    assert attempt["captured_at"] == facts()["captured_at"]
    http.lose_upload_ack = lost_ack
    stored = service.store_capture(raw, attempt, timeout=5, artifact_version=V2)
    assert stored["created_at"] == "2026-09-21T00:30:00.000000Z"
    assert type(stored["generation"]) is int
    assert stored["generation"] > 0
    assert service.read_capture(attempt, stored, timeout=5, artifact_version=V2) == (raw, stored)
    assert len([call for call in http.calls if call[0] == "POST"]) == 1


def test_v2_requires_explicit_selector_and_refuses_mismatched_artifact():
    service, http = objects(Transport())
    with pytest.raises(SourceCaptureStorageError):
        service.prepare_capture(artifact(), initial_manifest_sha256=MANIFEST)
    value = artifact()
    value["contract_version"] = "open_intelligence_protected_source_capture_artifact_v1"
    with pytest.raises(SourceCaptureStorageError):
        service.prepare_capture(value, initial_manifest_sha256=MANIFEST, artifact_version=V2)
    assert http.calls == []


@pytest.mark.parametrize("field", ["captured_at", "extra", "nested"])
def test_v2_capture_shape_and_canonical_time_before_io(field):
    service, http = objects(Transport())
    value = artifact()
    if field == "captured_at":
        value["capture"][field] = "2026-09-21T00:25:00+00:00"
    elif field == "extra":
        value["capture"][field] = True
    else:
        value["capture"] = {
            "assembly": {"snapshot": {"captured_at": "2026-09-21T00:25:00.000000Z"}}
        }
    with pytest.raises(SourceCaptureStorageError):
        service.prepare_capture(value, initial_manifest_sha256=MANIFEST, artifact_version=V2)
    assert http.calls == []


@pytest.mark.parametrize("defect", ["generation", "size", "hash", "time", "bytes", "capture_time"])
def test_v2_readback_preserves_transport_guards(defect):
    service, http, raw, attempt = prepared()
    stored = service.store_capture(raw, attempt, timeout=5, artifact_version=V2)
    if defect == "generation":
        stored["generation"] += 1
    elif defect == "size":
        stored["size_bytes"] += 1
    elif defect == "hash":
        stored["sha256"] = "0" * 64
    elif defect == "time":
        stored["created_at"] = "2026-09-20T00:30:00.000000Z"
    elif defect == "capture_time":
        attempt = deepcopy(attempt)
        attempt["captured_at"] = "2026-09-21T00:26:00.000000Z"
    else:
        name = attempt["uri"].split("/", 3)[3]
        http.objects[(name, stored["generation"])]["raw"] = raw + b" "
    with pytest.raises(SourceCaptureStorageError):
        service.read_capture(attempt, stored, timeout=5, artifact_version=V2)


def test_downloaded_version_must_match_even_with_matching_hash_and_size():
    service, http, raw, attempt = prepared()
    value = json.loads(raw)
    value["contract_version"] = "open_intelligence_protected_source_capture_artifact_v1"
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    attempt.update(
        sha256=digest,
        size_bytes=len(raw),
        uri=f"gs://{http.metadata['name']}/captures/{MANIFEST}/{digest}/capture.json",
    )
    http.seed(f"captures/{MANIFEST}/{digest}/capture.json", raw)
    with pytest.raises(SourceCaptureStorageError, match="artifact_conflict"):
        service.read_capture(attempt, timeout=5, artifact_version=V2)
    assert any("alt=media" in call[1] for call in http.calls)


def test_upload_conflict_reconciles_without_overwrite():
    service, http, raw, attempt = prepared()
    name = attempt["uri"].split("/", 3)[3]
    generation = http.seed(name, raw)
    stored = service.store_capture(raw, attempt, timeout=5, artifact_version=V2)
    assert stored["generation"] == generation
    assert len([call for call in http.calls if call[0] == "POST"]) == 1
