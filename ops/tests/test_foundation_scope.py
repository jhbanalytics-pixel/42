import hashlib
import json
from copy import deepcopy

import pytest
from ops.deploy.baseline_scope import (
    certify_protected_scope,
    compare_protected_scopes,
)

PROJECT = "ogilvy-trends-v2"
DELTA_SHA256 = "d" * 64
DECISION_EVIDENCE_SHA256 = "e" * 64
EXCLUDED_DATASETS = tuple(f"_approved_cache_{index:02d}" for index in range(14))
ANON_TABLES = tuple(f"anon_old_{index}" for index in range(5))
REGIONS = {
    "run": ["us-central1"],
    "scheduler": ["us-central1"],
    "tasks": ["us-central1"],
    "bigquery": ["US"],
}


def canonical_digest(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def json_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def dataset_name(dataset_id: str) -> str:
    return f"projects/{PROJECT}/datasets/{dataset_id}"


def table_name(dataset_id: str, table_id: str) -> str:
    return f"{dataset_name(dataset_id)}/tables/{table_id}"


def row(name: str, observed: dict) -> dict:
    return {"name": name, "region": "US", "observed": observed}


def reference_payload() -> dict:
    resources = {
        "jobs": [
            row(
                f"projects/{PROJECT}/locations/us-central1/jobs/prod-job",
                {"name": "prod-job", "template": {"image": "job@sha256:1"}},
            )
        ],
        "services": [
            row(
                f"projects/{PROJECT}/locations/us-central1/services/prod-service",
                {"name": "prod-service", "template": {"image": "service@sha256:1"}},
            )
        ],
        "schedulers": [],
        "queues": [],
        "datasets": [
            row(
                dataset_name(dataset_id),
                {
                    "datasetReference": {
                        "projectId": PROJECT,
                        "datasetId": dataset_id,
                    },
                    "location": "US",
                    "access": [{"role": "OWNER", "userByEmail": "owner@example.invalid"}],
                },
            )
            for dataset_id in (*EXCLUDED_DATASETS, "permanent")
        ],
        "tables": [
            row(
                table_name("permanent", "briefs"),
                {"schema": {"fields": [{"name": "id", "type": "STRING"}]}},
            ),
            *[
                row(
                    table_name(EXCLUDED_DATASETS[2], table_id),
                    {"schema": {"fields": [{"name": "value", "type": "STRING"}]}},
                )
                for table_id in ANON_TABLES
            ],
        ],
        "iam": [
            row(f"projects/{PROJECT}", {"etag": "project-etag", "bindings": []}),
            row(
                table_name("permanent", "briefs"),
                {"etag": "table-etag", "bindings": []},
            ),
        ],
    }
    payload = {
        "schema_version": 1,
        "project": PROJECT,
        "regions": deepcopy(REGIONS),
        "identity": "reader@example.invalid",
        "complete": False,
        "resources": resources,
        "failures": [],
    }
    payload["content_digest"] = canonical_digest(payload)
    return payload


def approval_payload() -> dict:
    excluded = [dataset_name(dataset_id) for dataset_id in EXCLUDED_DATASETS]
    unsupported = [
        table_name(EXCLUDED_DATASETS[2], table_id) for table_id in ANON_TABLES
    ]
    return {
        "schema_version": "42_r00_scope_approval_v1",
        "decision": "approved",
        "delta_sha256": DELTA_SHA256,
        "project": PROJECT,
        "excluded_table_namespaces": excluded,
        "unsupported_table_iam_resources": unsupported,
        "retain_dataset_metadata_and_iam": True,
        "exclude_future_namespaces": False,
        "user_decision": {
            "thread_id": "synthetic-private-thread",
            "turn_id": "synthetic-private-turn",
            "message_id": "synthetic-private-message",
            "text": "Synthetic approved decision",
            "evidence_sha256": DECISION_EVIDENCE_SHA256,
        },
        "recorded_at": "2026-09-12T13:52:06.471086+00:00",
    }


def native_identity_evidence() -> dict:
    material = {
        "principal": "reader@example.invalid",
        "credential_fingerprint": "sha256:"
        + hashlib.sha256(b"native-session").hexdigest(),
        "acquisition": "gcloud_named_account_token_no_impersonation",
        "no_fallback": True,
    }
    return {**material, "evidence_digest": canonical_digest(material)}


def allowed_failures() -> list[dict]:
    list_failures = [
        {
            "operation": "list_tables",
            "resource": "https://bigquery.googleapis.com/bigquery/v2/"
            f"projects/{PROJECT}/datasets/{dataset_id}/tables",
            "code": "permission_denied",
            "reason_code": "permission_denied",
            "exception_category": "http",
            "http_status": 403,
        }
        for dataset_id in EXCLUDED_DATASETS
        if dataset_id != EXCLUDED_DATASETS[2]
    ]
    iam_failures = [
        {
            "operation": "iam",
            "resource": table_name(EXCLUDED_DATASETS[2], table_id),
            "code": "invalid_request",
            "reason_code": "invalid_request",
            "exception_category": "http",
            "http_status": 400,
        }
        for table_id in ANON_TABLES
    ]
    return [*list_failures, *iam_failures]


def capture_payload() -> dict:
    payload = deepcopy(reference_payload())
    payload["identity_evidence"] = native_identity_evidence()
    payload["failures"] = allowed_failures()
    payload["content_digest"] = canonical_digest(
        {key: value for key, value in payload.items() if key != "content_digest"}
    )
    return payload


def fixture_bytes() -> tuple[bytes, bytes, bytes, str, str]:
    capture_bytes = json_bytes(capture_payload())
    reference_bytes = json_bytes(reference_payload())
    approval_bytes = json_bytes(approval_payload())
    return (
        capture_bytes,
        reference_bytes,
        approval_bytes,
        hashlib.sha256(approval_bytes).hexdigest(),
        hashlib.sha256(reference_bytes).hexdigest(),
    )


def certify(capture: dict | None = None) -> dict:
    capture_bytes, reference_bytes, approval_bytes, approval_pin, reference_pin = (
        fixture_bytes()
    )
    if capture is not None:
        capture["content_digest"] = canonical_digest(
            {key: value for key, value in capture.items() if key != "content_digest"}
        )
        capture_bytes = json_bytes(capture)
    return certify_protected_scope(
        capture_bytes,
        reference_bytes,
        approval_bytes,
        trusted_approval_sha256=approval_pin,
        trusted_reference_sha256=reference_pin,
    )


def test_only_approved_private_table_limits_can_certify_protected_scope() -> None:
    certificate = certify()

    assert certificate["complete_for_approved_scope"] is True
    assert certificate["unexplained_failures"] == []
    assert len(certificate["retained_limitations"]) == 18
    assert len(certificate["protected_resources"]["datasets"]) == 15
    assert [row["name"] for row in certificate["protected_resources"]["tables"]] == [
        table_name("permanent", "briefs")
    ]


def test_disappeared_private_cache_tables_and_failures_remain_certifiable() -> None:
    capture = capture_payload()
    excluded = {dataset_name(dataset_id) for dataset_id in EXCLUDED_DATASETS}
    capture["resources"]["tables"] = [
        item
        for item in capture["resources"]["tables"]
        if item["name"].rsplit("/tables/", 1)[0] not in excluded
    ]
    capture["failures"] = []

    certificate = certify(capture=capture)

    assert certificate["complete_for_approved_scope"] is True
    assert certificate["retained_limitations"] == []


def test_newly_visible_table_inside_approved_namespace_is_temporal() -> None:
    capture = capture_payload()
    visible = table_name(EXCLUDED_DATASETS[0], "new_visible_table")
    capture["resources"]["tables"].append(
        row(visible, {"schema": {"fields": [{"name": "id", "type": "STRING"}]}})
    )
    capture["failures"] = []

    certificate = certify(capture=capture)

    assert certificate["complete_for_approved_scope"] is True
    assert all(
        item["name"] != visible for item in certificate["protected_resources"]["tables"]
    )


def test_changed_anonymous_id_inside_approved_namespace_is_temporal() -> None:
    capture = capture_payload()
    parent = dataset_name(EXCLUDED_DATASETS[2])
    capture["resources"]["tables"] = [
        item
        for item in capture["resources"]["tables"]
        if item["name"].rsplit("/tables/", 1)[0] != parent
    ]
    changed = table_name(EXCLUDED_DATASETS[2], "anon_new_identity")
    capture["resources"]["tables"].append(
        row(changed, {"schema": {"fields": [{"name": "value", "type": "STRING"}]}})
    )
    capture["failures"] = [
        {
            "operation": "iam",
            "resource": changed,
            "code": "invalid_request",
            "reason_code": "invalid_request",
            "exception_category": "http",
            "http_status": 400,
        }
    ]

    certificate = certify(capture=capture)

    assert certificate["complete_for_approved_scope"] is True
    assert certificate["retained_limitations"][0]["resource"] == changed


def test_generic_cache_iam_error_is_not_relabelled_as_unsupported() -> None:
    capture = capture_payload()
    capture["failures"] = [
        {
            "operation": "iam",
            "resource": table_name(EXCLUDED_DATASETS[2], "anon_unknown"),
            "code": "provider_error",
            "reason_code": "provider_error",
            "exception_category": "http",
            "http_status": 500,
        }
    ]

    certificate = certify(capture=capture)

    assert certificate["complete_for_approved_scope"] is False
    assert certificate["retained_limitations"] == []
    assert certificate["unexplained_failures"][0]["reason_code"] == "provider_error"


def test_duplicate_permitted_failure_still_blocks_certification() -> None:
    capture = capture_payload()
    capture["failures"].append(deepcopy(capture["failures"][0]))

    certificate = certify(capture=capture)

    assert certificate["complete_for_approved_scope"] is False
    assert certificate["unexplained_failures"] == [capture["failures"][-1]]


def test_inputs_are_not_mutated() -> None:
    values = fixture_bytes()
    originals = tuple(bytes(value) if isinstance(value, bytes) else value for value in values)

    certify_protected_scope(
        values[0],
        values[1],
        values[2],
        trusted_approval_sha256=values[3],
        trusted_reference_sha256=values[4],
    )

    assert values == originals


def test_source_transport_normalization_evidence_is_preserved() -> None:
    capture = capture_payload()
    capture["transport_normalizations"] = [
        {
            "collection": "queues",
            "field": "queues",
            "method": "GET",
            "reason": "provider_omitted_empty_terminal_collection",
            "resource": "https://cloudtasks.googleapis.com/v2/"
            f"projects/{PROJECT}/locations/us-central1/queues",
            "raw_response_digest": "sha256:" + "a" * 64,
        }
    ]

    certificate = certify(capture=capture)

    assert certificate["source_transport_normalizations"] == capture[
        "transport_normalizations"
    ]


@pytest.mark.parametrize("target", ["approval", "reference"])
def test_tampered_trust_anchor_is_refused(target: str) -> None:
    capture_bytes, reference_bytes, approval_bytes, approval_pin, reference_pin = (
        fixture_bytes()
    )
    if target == "approval":
        approval_bytes += b" "
    else:
        reference_bytes += b" "

    with pytest.raises(ValueError, match=f"{target}_sha256_mismatch"):
        certify_protected_scope(
            capture_bytes,
            reference_bytes,
            approval_bytes,
            trusted_approval_sha256=approval_pin,
            trusted_reference_sha256=reference_pin,
        )


def test_tampered_capture_content_digest_is_refused() -> None:
    capture_bytes, reference_bytes, approval_bytes, approval_pin, reference_pin = (
        fixture_bytes()
    )
    capture = json.loads(capture_bytes)
    capture["project"] = "tampered-project"

    with pytest.raises(ValueError, match="capture_content_digest_mismatch"):
        certify_protected_scope(
            json_bytes(capture),
            reference_bytes,
            approval_bytes,
            trusted_approval_sha256=approval_pin,
            trusted_reference_sha256=reference_pin,
        )


def test_certificate_retains_only_bounded_decision_evidence() -> None:
    certificate = certify()
    encoded = json.dumps(certificate, sort_keys=True)

    assert certificate["decision_evidence_sha256"] == DECISION_EVIDENCE_SHA256
    assert "user_decision" not in certificate
    assert "synthetic-private-thread" not in encoded
    assert "synthetic-private-turn" not in encoded
    assert "synthetic-private-message" not in encoded
    assert "Synthetic approved decision" not in encoded


def test_fixture_principal_cannot_certify_native_scope() -> None:
    capture = capture_payload()
    capture["identity_evidence"]["acquisition"] = "trusted_test_fixture"
    material = {
        key: capture["identity_evidence"][key]
        for key in (
            "principal",
            "credential_fingerprint",
            "acquisition",
            "no_fallback",
        )
    }
    capture["identity_evidence"]["evidence_digest"] = canonical_digest(material)

    with pytest.raises(ValueError, match="native_principal_required"):
        certify(capture=capture)


def test_unlisted_hidden_dataset_blocks_certification() -> None:
    capture = capture_payload()
    capture["resources"]["datasets"].append(
        row(
            dataset_name("_future_hidden_namespace"),
            {
                "datasetReference": {
                    "projectId": PROJECT,
                    "datasetId": "_future_hidden_namespace",
                },
                "location": "US",
                "access": [],
            },
        )
    )

    certificate = certify(capture=capture)

    assert certificate["complete_for_approved_scope"] is False
    assert certificate["inventory_mismatch"]["datasets"]["added"] == [
        dataset_name("_future_hidden_namespace")
    ]


def test_denied_permanent_table_iam_blocks_certification() -> None:
    capture = capture_payload()
    capture["failures"].append(
        {
            "operation": "iam",
            "resource": table_name("permanent", "briefs"),
            "code": "permission_denied",
            "reason_code": "permission_denied",
            "exception_category": "http",
            "http_status": 403,
        }
    )

    certificate = certify(capture=capture)

    assert certificate["complete_for_approved_scope"] is False
    assert certificate["unexplained_failures"][-1]["resource"] == table_name(
        "permanent", "briefs"
    )


def test_missing_excluded_dataset_acl_blocks_certification() -> None:
    capture = capture_payload()
    target = dataset_name(EXCLUDED_DATASETS[0])
    dataset = next(row for row in capture["resources"]["datasets"] if row["name"] == target)
    dataset["observed"].pop("access")

    certificate = certify(capture=capture)

    assert certificate["complete_for_approved_scope"] is False
    assert certificate["unexplained_failures"][-1] == {
        "operation": "scope_validation",
        "resource": target,
        "code": "dataset_access_missing",
    }


def test_missing_protected_resource_blocks_certification() -> None:
    capture = capture_payload()
    capture["resources"]["services"] = []

    certificate = certify(capture=capture)

    assert certificate["complete_for_approved_scope"] is False
    assert certificate["inventory_mismatch"]["services"]["missing"] == [
        f"projects/{PROJECT}/locations/us-central1/services/prod-service"
    ]


def test_changed_project_or_regions_is_refused() -> None:
    for field in ("project", "regions"):
        capture = capture_payload()
        capture[field] = "other" if field == "project" else {**REGIONS, "run": ["other"]}
        with pytest.raises(ValueError, match=f"capture_{field}_mismatch"):
            certify(capture=capture)


def test_reference_regions_must_match_declared_scope() -> None:
    capture_bytes, reference_bytes, approval_bytes, approval_pin, _ = fixture_bytes()
    reference = json.loads(reference_bytes)
    reference["regions"]["run"] = ["other"]
    reference["content_digest"] = canonical_digest(
        {key: value for key, value in reference.items() if key != "content_digest"}
    )
    reference_bytes = json_bytes(reference)

    with pytest.raises(ValueError, match="reference_regions_mismatch"):
        certify_protected_scope(
            capture_bytes,
            reference_bytes,
            approval_bytes,
            trusted_approval_sha256=approval_pin,
            trusted_reference_sha256=hashlib.sha256(reference_bytes).hexdigest(),
        )


def test_same_scope_image_drift_is_reported_by_protected_comparator() -> None:
    before = certify()
    after_capture = capture_payload()
    after_capture["resources"]["services"][0]["observed"]["template"]["image"] = (
        "service@sha256:changed"
    )
    after = certify(capture=after_capture)

    assert compare_protected_scopes(before, after) == [
        {
            "category": "services",
            "name": f"projects/{PROJECT}/locations/us-central1/services/prod-service",
            "change": "protected_configuration_changed",
        }
    ]


def test_changed_certificate_or_inventory_binding_is_refused() -> None:
    before = certify()
    tampered = deepcopy(before)
    tampered["protected_inventory_digest"] = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="certificate_content_digest_mismatch"):
        compare_protected_scopes(before, tampered)

    after = certify()
    after["approval_sha256"] = "sha256:" + "1" * 64
    after["content_digest"] = canonical_digest(
        {key: value for key, value in after.items() if key != "content_digest"}
    )
    with pytest.raises(ValueError, match="certificate_scope_mismatch"):
        compare_protected_scopes(before, after)
