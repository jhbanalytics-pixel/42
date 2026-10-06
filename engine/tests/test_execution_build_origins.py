import inspect
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from src.analysis.open_intelligence import execution_approval, execution_origins

ENGINE_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ENGINE_ROOT / "configs/open_intelligence/execution_origins_v1.json"
CONNECTED_BUILD_FIXTURE = (
    ENGINE_ROOT / "tests/fixtures/open_intelligence/cloud_build_connected_repository_v1.json"
)
REGISTRY_SHA256 = "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179"
MANIFEST_V1 = "open_intelligence_execution_manifest_v1"
MANIFEST_V2 = "open_intelligence_execution_manifest_v2"
RETAINED_V1_CONTRACT = "250367b37ec1c086082c0863e31ac938870aae0b63a5d5def9e75127cc29d528"
V1_IMAGE_REPOSITORY = "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine"
SOURCE_SHA = "d" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
BUILD_ID = "11111111-1111-4111-8111-111111111111"
BUILD_RESOURCE = f"projects/ogilvy-trends-v2/locations/us-central1/builds/{BUILD_ID}"
COMMON_FIELDS = ("name", "id", "projectId", "status", "results", "finishTime")


def registry():
    return execution_origins.load_origin_registry(
        REGISTRY_PATH,
        expected_sha256=REGISTRY_SHA256,
        contract_root=ENGINE_ROOT,
    )


def build_payload(origin, *, native=True, legacy=None):
    payload = {
        "name": BUILD_RESOURCE,
        "id": BUILD_ID,
        "projectId": "ogilvy-trends-v2",
        "status": "SUCCESS",
        "results": {
            "images": [
                {
                    "name": f"{origin.image_repository}:{SOURCE_SHA}",
                    "digest": IMAGE_DIGEST,
                }
            ]
        },
        "finishTime": "2026-08-31T12:00:00.000000Z",
    }
    if native:
        payload["source"] = {
            "connectedRepository": {
                "repository": origin.connected_repo,
                "revision": SOURCE_SHA,
            }
        }
    if legacy is not None:
        payload["sourceProvenance"] = legacy
    return payload


def provenance(payload, origin, mode):
    return execution_approval.build_provenance_from_response(
        payload,
        origin.contract_sha256,
        manifest_version=origin.manifest_version,
        mode=mode,
        registry=registry(),
    )


def refusal(code):
    return pytest.raises(
        (execution_approval.ApprovalRefusal, execution_origins.OriginRefusal), match=f"^{code}$"
    )


def test_build_interfaces_require_the_selected_origin_as_keywords():
    module = execution_approval
    assert (
        str(inspect.signature(module._connected_repository_sha))
        == "(source: object, *, origin) -> str | None"
    )
    assert (
        str(inspect.signature(module.normalize_cloud_build_response))
        == "(payload: object, *, origin) -> dict[str, object]"
    )
    assert str(inspect.signature(module.build_provenance_from_response)) == (
        "(payload: object, operation_contract_sha256: str, *, manifest_version, mode, registry) "
        "-> src.analysis.open_intelligence.execution_approval.BuildProvenanceReceipt"
    )
    assert str(inspect.signature(module.canonical_build_provenance_bytes)) == (
        "(receipt: src.analysis.open_intelligence.execution_approval.BuildProvenanceReceipt, *, origin) -> bytes"
    )


@pytest.mark.parametrize(
    "source_kind", ["resolvedRepoSource", "resolvedGitSource", "resolvedConnectedRepository"]
)
def test_every_retained_v1_pair_accepts_its_historical_legacy_source(source_kind):
    revision_field = "commitSha" if source_kind == "resolvedRepoSource" else "revision"
    rows = [row for row in registry().values() if row.manifest_version == MANIFEST_V1]
    assert len(rows) == 5
    for origin in rows:
        payload = build_payload(
            origin, native=False, legacy={source_kind: {revision_field: SOURCE_SHA}}
        )
        receipt = provenance(payload, origin, "historical_read")
        assert receipt.resolved_source_sha == SOURCE_SHA
        assert receipt.image_uri == f"{origin.image_repository}@{IMAGE_DIGEST}"


@pytest.mark.parametrize("source_value", [None, {}], ids=["null", "empty_mapping"])
def test_every_retained_v1_pair_preserves_empty_native_source_and_receipt_bytes(source_value):
    rows = [row for row in registry().values() if row.manifest_version == MANIFEST_V1]
    assert len(rows) == 5
    for origin in rows:
        legacy = {"resolvedRepoSource": {"commitSha": SOURCE_SHA}}
        omitted = build_payload(origin, native=False, legacy=legacy)
        expected = execution_approval.canonical_build_provenance_bytes(
            provenance(omitted, origin, "historical_read"),
            origin=origin,
        )

        payload = build_payload(origin, native=False, legacy=legacy)
        payload["source"] = deepcopy(source_value)
        normalized = execution_approval.normalize_cloud_build_response(payload, origin=origin)
        assert normalized == {
            "name": BUILD_RESOURCE,
            "id": BUILD_ID,
            "projectId": "ogilvy-trends-v2",
            "status": "SUCCESS",
            "sourceProvenance": {"resolvedRepoSource": {"commitSha": SOURCE_SHA}},
            "results": {
                "images": [
                    {
                        "name": f"{V1_IMAGE_REPOSITORY}:{SOURCE_SHA}",
                        "digest": IMAGE_DIGEST,
                    }
                ]
            },
            "finishTime": "2026-08-31T12:00:00.000000Z",
        }
        receipt = provenance(payload, origin, "historical_read")
        assert (
            execution_approval.canonical_build_provenance_bytes(
                receipt,
                origin=origin,
            )
            == expected
        )


@pytest.mark.parametrize("source_value", [None, {}], ids=["null", "empty_mapping"])
def test_every_v2_pair_refuses_empty_native_source(source_value):
    rows = [row for row in registry().values() if row.manifest_version == MANIFEST_V2]
    assert len(rows) == 4
    for origin in rows:
        payload = build_payload(
            origin,
            native=False,
            legacy={"resolvedRepoSource": {"commitSha": SOURCE_SHA}},
        )
        payload["source"] = deepcopy(source_value)
        with refusal("execution_approval_build_provenance_invalid"):
            provenance(payload, origin, "new_consume")


def test_every_retained_v1_pair_still_refuses_malformed_nonempty_native_source():
    rows = [row for row in registry().values() if row.manifest_version == MANIFEST_V1]
    assert len(rows) == 5
    for origin in rows:
        payload = build_payload(
            origin,
            native=False,
            legacy={"resolvedRepoSource": {"commitSha": SOURCE_SHA}},
        )
        payload["source"] = {"storageSource": {"bucket": "untrusted"}}
        with refusal("execution_approval_build_provenance_invalid"):
            provenance(payload, origin, "historical_read")


def test_current_v2_requires_and_preserves_connected_repository_without_manufacturing_provenance():
    origin = next(row for row in registry().values() if row.manifest_version == MANIFEST_V2)
    payload = build_payload(origin, legacy={})
    normalized = execution_approval.normalize_cloud_build_response(payload, origin=origin)
    assert tuple(normalized) == (*COMMON_FIELDS, "source", "sourceProvenance")
    assert normalized["source"] == payload["source"]
    assert normalized["sourceProvenance"] == {}
    receipt = provenance(payload, origin, "new_consume")
    assert receipt.resolved_source_sha == SOURCE_SHA
    assert receipt.image_uri == f"{origin.image_repository}@{IMAGE_DIGEST}"

    absent = build_payload(origin)
    normalized = execution_approval.normalize_cloud_build_response(absent, origin=origin)
    assert tuple(normalized) == (*COMMON_FIELDS, "source")
    assert "sourceProvenance" not in normalized


def test_archived_connected_repository_response_preserves_actual_empty_provenance():
    origin = registry()[(MANIFEST_V1, RETAINED_V1_CONTRACT)]
    payload = json.loads(CONNECTED_BUILD_FIXTURE.read_bytes())
    normalized = execution_approval.normalize_cloud_build_response(payload, origin=origin)
    assert normalized["sourceProvenance"] == {}
    assert normalized["source"] == payload["source"]
    assert provenance(payload, origin, "historical_read").resolved_source_sha == (
        "dddafd6a7af284c39c42afe1b1eb387e2aafb2b6"
    )


def test_agreeing_dual_sources_are_retained_and_mismatch_refuses():
    origin = next(row for row in registry().values() if row.manifest_version == MANIFEST_V2)
    payload = build_payload(origin, legacy={"resolvedRepoSource": {"commitSha": SOURCE_SHA}})
    normalized = execution_approval.normalize_cloud_build_response(payload, origin=origin)
    assert normalized["sourceProvenance"] == payload["sourceProvenance"]

    payload["sourceProvenance"]["resolvedRepoSource"]["commitSha"] = "c" * 40
    with refusal("execution_approval_build_provenance_invalid"):
        provenance(payload, origin, "new_consume")


def test_wrong_repository_with_right_sha_and_mixed_origin_fields_refuse():
    rows = list(registry().values())
    old = next(row for row in rows if row.manifest_version == MANIFEST_V1)
    new = next(row for row in rows if row.manifest_version == MANIFEST_V2)

    wrong_repository = build_payload(new)
    wrong_repository["source"]["connectedRepository"]["repository"] = old.connected_repo
    with refusal("execution_approval_build_provenance_invalid"):
        provenance(wrong_repository, new, "new_consume")

    mixed_image = build_payload(new)
    mixed_image["results"]["images"][0]["name"] = f"{old.image_repository}:{SOURCE_SHA}"
    with refusal("execution_approval_build_provenance_invalid"):
        provenance(mixed_image, new, "new_consume")


def test_selector_and_mode_refuse_before_payload_normalization():
    reg = registry()
    with refusal("execution_origin_pair_invalid"):
        execution_approval.build_provenance_from_response(
            object(),
            "f" * 64,
            manifest_version=MANIFEST_V2,
            mode="new_consume",
            registry=reg,
        )

    historical = next(row for row in reg.values() if row.manifest_version == MANIFEST_V1)
    with refusal("execution_origin_mode_forbidden"):
        execution_approval.build_provenance_from_response(
            object(),
            historical.contract_sha256,
            manifest_version=MANIFEST_V1,
            mode="new_consume",
            registry=reg,
        )


def test_v2_refuses_sha_only_legacy_envelope():
    origin = next(row for row in registry().values() if row.manifest_version == MANIFEST_V2)
    payload = build_payload(
        origin,
        native=False,
        legacy={"resolvedRepoSource": {"commitSha": SOURCE_SHA}},
    )
    with refusal("execution_approval_build_provenance_invalid"):
        provenance(payload, origin, "new_consume")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["source"].__setitem__("repoSource", {"commitSha": SOURCE_SHA}),
        lambda payload: payload["source"]["connectedRepository"].__setitem__("dir", "."),
        lambda payload: payload["sourceProvenance"].__setitem__("fileHashes", {}),
        lambda payload: payload["sourceProvenance"]["resolvedRepoSource"].__setitem__("dir", "."),
    ],
)
def test_source_envelopes_refuse_extra_fields(mutate):
    origin = next(row for row in registry().values() if row.manifest_version == MANIFEST_V2)
    payload = build_payload(origin, legacy={"resolvedRepoSource": {"commitSha": SOURCE_SHA}})
    mutate(payload)
    with refusal("execution_approval_build_provenance_invalid"):
        provenance(payload, origin, "new_consume")


def test_retained_v1_receipt_bytes_are_exact_and_wrong_origin_refuses():
    rows = list(registry().values())
    origin = registry()[(MANIFEST_V1, RETAINED_V1_CONTRACT)]
    payload = build_payload(
        origin,
        native=False,
        legacy={"resolvedRepoSource": {"commitSha": SOURCE_SHA}},
    )
    receipt = provenance(payload, origin, "historical_replay")
    expected = (
        b'{"build_id":"11111111-1111-4111-8111-111111111111",'
        b'"build_provenance_contract_version":"open_intelligence_build_provenance_v1",'
        b'"build_resource":"projects/ogilvy-trends-v2/locations/us-central1/builds/'
        b'11111111-1111-4111-8111-111111111111","finished_at":"2026-08-31T12:00:00.000000Z",'
        b'"image_uri":"us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@'
        b'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        b'"location":"us-central1","operation_contract_sha256":"'
        + RETAINED_V1_CONTRACT.encode("ascii")
        + b'","project_id":"ogilvy-trends-v2","resolved_source_sha":"'
        + SOURCE_SHA.encode("ascii")
        + b'","status":"SUCCESS"}'
    )
    assert execution_approval.canonical_build_provenance_bytes(receipt, origin=origin) == expected

    wrong_origin = next(row for row in rows if row.manifest_version == MANIFEST_V2)
    with refusal("execution_approval_build_provenance_invalid"):
        execution_approval.canonical_build_provenance_bytes(receipt, origin=wrong_origin)


def test_normalizer_projects_only_common_and_actual_source_fields():
    origin = next(row for row in registry().values() if row.manifest_version == MANIFEST_V1)
    payload = build_payload(
        origin,
        native=False,
        legacy={"resolvedRepoSource": {"commitSha": SOURCE_SHA}},
    )
    payload.update(createTime="ignored", substitutions={"_UNTRUSTED": "ignored"})
    normalized = execution_approval.normalize_cloud_build_response(deepcopy(payload), origin=origin)
    assert tuple(normalized) == (*COMMON_FIELDS, "sourceProvenance")
    assert normalized["sourceProvenance"] == payload["sourceProvenance"]


def test_receipt_type_and_timestamp_validation_still_refuse():
    origin = next(row for row in registry().values() if row.manifest_version == MANIFEST_V1)
    receipt = execution_approval.BuildProvenanceReceipt(
        build_provenance_contract_version="open_intelligence_build_provenance_v1",
        build_resource=BUILD_RESOURCE,
        project_id="ogilvy-trends-v2",
        location="us-central1",
        build_id=BUILD_ID,
        status="SUCCESS",
        resolved_source_sha=SOURCE_SHA,
        image_uri=f"{origin.image_repository}@{IMAGE_DIGEST}",
        operation_contract_sha256=origin.contract_sha256,
        finished_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
    )
    assert execution_approval.canonical_build_provenance_bytes(receipt, origin=origin)

    malformed = replace(receipt, finished_at=datetime(2026, 8, 31, 12))
    with refusal("execution_approval_build_provenance_invalid"):
        execution_approval.canonical_build_provenance_bytes(malformed, origin=origin)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("projectId", "other-project"),
        lambda payload: payload.__setitem__("status", "FAILURE"),
        lambda payload: payload["source"]["connectedRepository"].__setitem__(
            "revision", SOURCE_SHA[:-1]
        ),
        lambda payload: payload["results"]["images"].clear(),
        lambda payload: payload["results"]["images"].append(
            deepcopy(payload["results"]["images"][0])
        ),
        lambda payload: payload["results"]["images"][0].__setitem__("digest", "sha256:" + "B" * 64),
        lambda payload: payload.__setitem__("finishTime", "2026-08-31T12:00:00Z"),
        lambda payload: payload.__setitem__("extra", "cafe\u0301"),
    ],
)
def test_existing_build_shape_project_image_timestamp_digest_and_nfc_guards_remain_closed(
    mutate,
):
    origin = next(row for row in registry().values() if row.manifest_version == MANIFEST_V2)
    payload = build_payload(origin)
    mutate(payload)
    with refusal("execution_approval_build_provenance_invalid"):
        provenance(payload, origin, "new_consume")
