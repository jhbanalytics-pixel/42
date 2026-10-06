from __future__ import annotations

import importlib
import importlib.util
import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from google.api_core.exceptions import PreconditionFailed

from src.api import investigations


def store_module():
    name = "src.api.investigation_store"
    assert importlib.util.find_spec(name) is not None, "investigation store module is missing"
    return importlib.import_module(name)


class ReadBlob:
    def __init__(self, data: bytes, error=None):
        self.data = data
        self.error = error

    def download_as_bytes(self):
        if self.error is not None:
            raise self.error
        return self.data


class WriteBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name

    def upload_from_string(self, data, *, content_type, if_generation_match):
        if self.bucket.upload_error is not None:
            raise self.bucket.upload_error
        self.bucket.uploads.append(
            {
                "name": self.name,
                "data": data,
                "content_type": content_type,
                "if_generation_match": if_generation_match,
            }
        )
        if self.name in self.bucket.objects and if_generation_match == 0:
            raise PreconditionFailed("already exists")
        self.bucket.objects[self.name] = data if isinstance(data, bytes) else data.encode("utf-8")


class Bucket:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.blob_error = None
        self.get_error = None
        self.upload_error = None
        self.download_error = None

    def blob(self, name):
        if self.blob_error is not None:
            raise self.blob_error
        return WriteBlob(self, name)

    def get_blob(self, name):
        if self.get_error is not None:
            raise self.get_error
        data = self.objects.get(name)
        return ReadBlob(data, self.download_error) if data is not None else None


def frame():
    return investigations.InvestigationFrame(
        client_scope_id="ogilvy_default",
        market_scope=("za", "ng", "ke"),
        brand_config_id=None,
        audience_lens_ids=(),
        theme_id=None,
        run_id="investigation_run_001",
        contract_version="2.1.0",
        decision_question="Which emerging behaviour should the brand act on?",
        time_horizon_days=42,
        brand_context=None,
        known_assumptions=(),
        change_my_mind_if=(),
        research_role_id=None,
        research_role_version=None,
        output_mode="internal_working_paper",
    )


def record():
    current_frame = frame()
    response = investigations.build_plan_ready(
        frame=current_frame,
        plan=investigations.default_plan_for_frame(current_frame),
        investigation_id=investigations.investigation_id_for_frame(current_frame),
        created_at=datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
        active_role_versions={},
    )
    return investigations.build_investigation_storage_record(current_frame, response)


def test_create_writes_canonical_json_once_with_generation_precondition():
    module = store_module()
    bucket = Bucket()

    result = module.create_investigation(
        bucket=bucket,
        prefix="open-intelligence/v2/staging/",
        record=record(),
    )

    expected_name = (
        "open-intelligence/v2/staging/investigations/" + record()["response"]["investigation_id"] + ".json"
    )
    assert result == module.InvestigationWriteResult(expected_name, created=True, unchanged=False)
    assert bucket.uploads == [
        {
            "name": expected_name,
            "data": module.canonical_investigation_bytes(record()),
            "content_type": "application/json",
            "if_generation_match": 0,
        }
    ]
    assert json.loads(bucket.objects[expected_name]) == record()


def test_identical_retry_is_unchanged_and_conflicting_retry_fails_closed():
    module = store_module()
    bucket = Bucket()
    first = record()
    module.create_investigation(bucket=bucket, prefix="open-intelligence/v2/staging/", record=first)

    retry = module.create_investigation(
        bucket=bucket,
        prefix="open-intelligence/v2/staging/",
        record=dict(reversed(tuple(first.items()))),
    )
    assert retry.created is False
    assert retry.unchanged is True

    changed = deepcopy(first)
    changed["response"]["research_plan"]["credit_ceiling"] = 1
    with pytest.raises(module.InvestigationConflict, match="canonical content conflict"):
        module.create_investigation(
            bucket=bucket,
            prefix="open-intelligence/v2/staging/",
            record=changed,
        )
    assert json.loads(next(iter(bucket.objects.values()))) == first


def test_read_returns_exact_object_and_missing_returns_none():
    module = store_module()
    bucket = Bucket()
    module.create_investigation(
        bucket=bucket,
        prefix="open-intelligence/v2/staging/",
        record=record(),
    )

    investigation_id = record()["response"]["investigation_id"]
    assert module.read_investigation(
        bucket=bucket,
        prefix="open-intelligence/v2/staging/",
        investigation_id=investigation_id,
    ) == record()
    assert (
        module.read_investigation(
            bucket=bucket,
            prefix="open-intelligence/v2/staging/",
            investigation_id="inv_missing",
        )
        is None
    )


@pytest.mark.parametrize(
    ("prefix", "investigation_id", "message"),
    [
        ("", "inv_valid", "prefix is invalid"),
        ("../outside/", "inv_valid", "prefix is invalid"),
        ("cache/", "inv_valid", "prefix must be exact staging namespace"),
        ("open-intelligence/v2/staging/", "../secret", "investigation id is invalid"),
        ("open-intelligence/v2/staging/", "research_001", "investigation id is invalid"),
    ],
)
def test_object_name_rejects_namespace_escape(prefix, investigation_id, message):
    module = store_module()
    with pytest.raises(ValueError, match=message):
        module.investigation_object_name(prefix, investigation_id)


def test_store_rejects_wrong_contract_or_lifecycle_before_bucket_access():
    module = store_module()
    bucket = Bucket()

    with pytest.raises(ValueError, match="contract version is invalid"):
        module.create_investigation(
            bucket=bucket,
            prefix="open-intelligence/v2/staging/",
            record={**record(), "storage_version": "2.0.0"},
        )
    with pytest.raises(ValueError, match="status is invalid for creation"):
        module.create_investigation(
            bucket=bucket,
            prefix="open-intelligence/v2/staging/",
            record={**record(), "response": {**record()["response"], "status": "complete"}},
        )
    assert bucket.uploads == []


def test_store_has_no_delete_surface():
    module = store_module()
    assert not hasattr(module, "delete_investigation")


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        ("blob_error", PermissionError("blob denied")),
        ("upload_error", TimeoutError("upload timeout")),
    ],
)
def test_create_wraps_storage_transport_failures(operation, error):
    module = store_module()
    bucket = Bucket()
    setattr(bucket, operation, error)

    with pytest.raises(module.InvestigationStoreError, match="investigation create failed"):
        module.create_investigation(
            bucket=bucket,
            prefix="open-intelligence/v2/staging/",
            record=record(),
        )


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        ("get_error", TimeoutError("lookup timeout")),
        ("download_error", PermissionError("download denied")),
    ],
)
def test_read_wraps_storage_transport_failures(operation, error):
    module = store_module()
    bucket = Bucket()
    module.create_investigation(
        bucket=bucket,
        prefix="open-intelligence/v2/staging/",
        record=record(),
    )
    setattr(bucket, operation, error)

    with pytest.raises(module.InvestigationStoreError, match="investigation read failed"):
        module.read_investigation(
            bucket=bucket,
            prefix="open-intelligence/v2/staging/",
            investigation_id=record()["response"]["investigation_id"],
        )


def test_read_rejects_tampered_scope_or_deterministic_identity():
    module = store_module()
    bucket = Bucket()
    stored = record()
    module.create_investigation(
        bucket=bucket,
        prefix="open-intelligence/v2/staging/",
        record=stored,
    )
    name = next(iter(bucket.objects))
    tampered = deepcopy(stored)
    tampered["frame"]["market_scope"] = ["za"]
    bucket.objects[name] = json.dumps(
        tampered, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")

    with pytest.raises(module.InvestigationStoreError, match="stored investigation is invalid"):
        module.read_investigation(
            bucket=bucket,
            prefix="open-intelligence/v2/staging/",
            investigation_id=stored["response"]["investigation_id"],
        )


@pytest.mark.parametrize("tamper", ["questions_string", "bad_timestamp"])
def test_read_rejects_invalid_plan_types_and_timestamp(tamper):
    module = store_module()
    bucket = Bucket()
    stored = record()
    module.create_investigation(
        bucket=bucket,
        prefix="open-intelligence/v2/staging/",
        record=stored,
    )
    name = next(iter(bucket.objects))
    tampered = deepcopy(stored)
    if tamper == "questions_string":
        tampered["response"]["research_plan"]["questions"] = "abc"
    else:
        tampered["response"]["created_at"] = "yesterday"
        tampered["response"]["updated_at"] = "yesterday"
    bucket.objects[name] = json.dumps(
        tampered, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")

    with pytest.raises(module.InvestigationStoreError, match="stored investigation is invalid"):
        module.read_investigation(
            bucket=bucket,
            prefix="open-intelligence/v2/staging/",
            investigation_id=stored["response"]["investigation_id"],
        )


# The staging grants: objectCreator and objectViewer, no replace


@pytest.mark.parametrize("taken", ["precondition", "forbidden"])
def test_a_retry_on_the_staging_grants_reads_back_instead_of_failing(taken):
    from tests.unit.object_creator_bucket import ObjectCreatorBucket

    module = store_module()
    bucket = ObjectCreatorBucket(taken)
    prefix = "open-intelligence/v2/staging/"
    first = record()
    created = module.create_investigation(bucket=bucket, prefix=prefix, record=first)
    retry = module.create_investigation(bucket=bucket, prefix=prefix, record=deepcopy(first))
    assert (created.created, retry.created, retry.unchanged) == (True, False, True)
    later = deepcopy(first)
    later["response"]["created_at"] = "2026-08-26T08:00:01Z"
    later["response"]["updated_at"] = "2026-08-26T08:00:01Z"
    with pytest.raises(module.InvestigationConflict):
        module.create_investigation(bucket=bucket, prefix=prefix, record=later)
    assert bucket.overwrites == []


def test_a_create_the_identity_may_not_make_is_storage_failure_not_a_conflict():
    from tests.unit.object_creator_bucket import ObjectCreatorBucket

    module = store_module()
    bucket = ObjectCreatorBucket("forbidden")
    bucket.create_denied = True
    with pytest.raises(module.InvestigationStoreError, match="investigation create failed"):
        module.create_investigation(
            bucket=bucket, prefix="open-intelligence/v2/staging/", record=record()
        )
