import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from ops.certification.production_comparison import compare_production, main
from ops.deploy.baseline_scope import certify_protected_scope
from ops.tests.test_foundation_scope import (
    PROJECT,
    allowed_failures,
    approval_payload,
    canonical_digest,
    capture_payload,
    certify,
    dataset_name,
    fixture_bytes,
    json_bytes,
    native_identity_evidence,
    reference_payload,
    table_name,
)

STAGING_JOB = f"projects/{PROJECT}/locations/us-central1/jobs/intelligence-42-daily-staging"
STAGING_SA = "serviceAccount:intelligence-42-scheduler@ogilvy-trends-v2.iam.gserviceaccount.com"
OUTSIDER = "user:outsider@example.invalid"


STAGING_DATASET = dataset_name("trends_v2_staging")
LEDGER_TABLE = table_name("trends_v2_staging_approvals", "open_intelligence_execution_results_v2")


def manifest() -> dict:
    return {
        "contract_version": "42_resource_manifest_v1",
        "resources": [
            {"actions": ["deploy", "read"], "name": f"//run.googleapis.com/{STAGING_JOB}"},
            {"actions": ["read", "write"], "name": f"//bigquery.googleapis.com/{STAGING_DATASET}"},
            {"actions": ["read"], "name": f"//bigquery.googleapis.com/{LEDGER_TABLE}"},
        ],
    }


def delta(manifest_bytes: bytes) -> dict:
    return {
        "contract_version": "42_iam_delta_v1",
        "resource_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "approval": {"state": "approved"},
        "bindings": [
            {
                "resource": f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}",
                "role": "roles/run.invoker",
                "member": STAGING_SA,
                "condition": None,
            },
            {
                "resource": f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}",
                "role": "roles/aiplatform.user",
                "member": STAGING_SA,
                "condition": EXPIRY,
            },
            {
                "resource": f"//bigquery.googleapis.com/{STAGING_DATASET}",
                "role": "roles/bigquery.dataViewer",
                "member": STAGING_SA,
                "condition": None,
            },
        ],
        "retained": [],
        "amendments": [],
    }


EXPIRY = "request.time < timestamp('2026-12-31T23:59:59Z')"
R00_OBSERVED_AT = "2026-09-01T00:00:00+00:00"
AFTER_OBSERVED_AT = "2026-09-25T00:00:00+00:00"


def certify_reference(reference: dict) -> tuple[dict, bytes]:
    """A before certificate over a custom R00 reference; returns it and the reference bytes."""
    reference["content_digest"] = canonical_digest(
        {key: value for key, value in reference.items() if key != "content_digest"}
    )
    capture = deepcopy(reference)
    capture["identity_evidence"] = native_identity_evidence()
    capture["failures"] = allowed_failures()
    capture.pop("content_digest")
    capture["content_digest"] = canonical_digest(capture)
    reference_bytes = json_bytes(reference)
    approval_bytes = json_bytes(approval_payload())
    certificate = certify_protected_scope(
        json_bytes(capture),
        reference_bytes,
        approval_bytes,
        trusted_approval_sha256=hashlib.sha256(approval_bytes).hexdigest(),
        trusted_reference_sha256=hashlib.sha256(reference_bytes).hexdigest(),
    )
    assert certificate["complete_for_approved_scope"] is True
    return certificate, reference_bytes


def default_reference() -> dict:
    """The R00 scope fixture with the production table read in full, as
    production_baseline's tables.get capture always reads it."""
    reference = reference_payload()
    for row in reference["resources"]["tables"]:
        if row["name"] == table_name("permanent", "briefs"):
            row["observed"] = {
                "tableReference": {"projectId": PROJECT, "datasetId": "permanent", "tableId": "briefs"},
                "type": "TABLE",
                "location": "US",
                "creationTime": "1",
                "schema": {"fields": [{"name": "id", "type": "STRING"}]},
            }
    reference["observed_at"] = R00_OBSERVED_AT
    return reference


def staging_reference() -> dict:
    reference = default_reference()
    reference["resources"]["datasets"].append(
        {
            "name": STAGING_DATASET,
            "region": "US",
            "observed": {
                "datasetReference": {"projectId": PROJECT, "datasetId": "trends_v2_staging"},
                "location": "US",
                "access": [{"role": "OWNER", "userByEmail": "owner@example.invalid"}],
            },
        }
    )
    legacy = table_name("trends_v2_staging", "legacy_v1")
    reference["resources"]["tables"].append(
        {"name": legacy, "region": "US", "observed": full_table_observed("legacy_v1")}
    )
    reference["resources"]["iam"].append(
        {"name": legacy, "region": "US", "observed": {"bindings": [{"role": "roles/bigquery.dataViewer", "members": ["group:readers@example.invalid"]}]}}
    )
    return reference


def full_table_observed(table_id: str) -> dict:
    return {
        "tableReference": {"projectId": PROJECT, "datasetId": "trends_v2_staging", "tableId": table_id},
        "type": "TABLE",
        "location": "US",
        "creationTime": "1",
        "schema": {"fields": [{"name": "id", "type": "STRING"}]},
    }


def after_capture(mutator=None, base: dict | None = None) -> dict:
    capture = deepcopy(base if base is not None else default_reference())
    capture["identity_evidence"] = native_identity_evidence()
    capture["failures"] = allowed_failures()
    capture["complete"] = True
    capture["observed_at"] = AFTER_OBSERVED_AT
    if mutator:
        mutator(capture)
    capture.pop("content_digest", None)
    capture["content_digest"] = canonical_digest(capture)
    return capture


def run(
    after: dict,
    *,
    certificate: dict | None = None,
    reference_bytes: bytes | None = None,
    delta_value: dict | None = None,
    manifest_value: dict | None = None,
) -> dict:
    _, _, approval_bytes, _, _ = fixture_bytes()
    if certificate is None:
        certificate, reference_bytes = certify_reference(default_reference())
    reference_pin = hashlib.sha256(reference_bytes).hexdigest()
    certificate_bytes = json_bytes(certificate)
    manifest_bytes = json_bytes(manifest_value or manifest())
    delta_bytes = json_bytes(delta_value or delta(manifest_bytes))
    return compare_production(
        before_certificate_bytes=certificate_bytes,
        reference_bytes=reference_bytes,
        approval_bytes=approval_bytes,
        after_capture_bytes=json_bytes(after),
        manifest_bytes=manifest_bytes,
        delta_bytes=delta_bytes,
        trusted_certificate_sha256=hashlib.sha256(certificate_bytes).hexdigest(),
        trusted_reference_sha256=reference_pin,
        trusted_delta_sha256=hashlib.sha256(delta_bytes).hexdigest(),
    )


def test_unchanged_production_scope_is_proven_even_when_capture_is_complete() -> None:
    result = run(after_capture())

    assert result["verdict"] == "production_non_change_proven"
    assert result["changes"] == []
    assert result["after_capture_complete"] is True
    assert result["content_digest"] == canonical_digest(
        {key: value for key, value in result.items() if key != "content_digest"}
    )


def test_production_image_change_requires_explanation_with_field_path() -> None:
    def change(capture):
        capture["resources"]["services"][0]["observed"]["template"]["image"] = "service@sha256:2"

    result = run(after_capture(change))

    assert result["verdict"] == "explanation_or_coverage_required"
    assert result["changes"] == [
        {
            "category": "services",
            "name": f"projects/{PROJECT}/locations/us-central1/services/prod-service",
            "change": "protected_configuration_changed",
            "fields": ["template.image"],
            "classification": "requires_explanation",
        }
    ]


def test_volatile_times_alone_are_not_changes() -> None:
    def touch(capture):
        capture["resources"]["services"][0]["observed"]["updateTime"] = "2026-09-25T00:00:00Z"

    assert run(after_capture(touch))["verdict"] == "production_non_change_proven"


def test_declared_staging_job_and_staging_tables_are_separated_from_production() -> None:
    def add_staging(capture):
        capture["resources"]["jobs"].append(
            {"name": STAGING_JOB, "region": "us-central1", "observed": {"name": STAGING_JOB}}
        )
        capture["resources"]["tables"].append(
            {
                "name": table_name("trends_v2_staging", "answers"),
                "region": "US",
                "observed": {"schema": {"fields": []}},
            }
        )

    result = run(after_capture(add_staging))

    assert result["verdict"] == "production_non_change_proven"
    assert {row["classification"] for row in result["changes"]} == {"declared_staging_resource"}
    assert result["counts"]["declared_staging_resource"] == 2


def test_declared_project_binding_passes_and_undeclared_binding_needs_explanation() -> None:
    def grant(members):
        def mutate(capture):
            project_iam = capture["resources"]["iam"][0]
            project_iam["observed"]["bindings"] = [{"role": "roles/run.invoker", "members": members}]

        return mutate

    declared = run(after_capture(grant([STAGING_SA])))
    undeclared = run(after_capture(grant([STAGING_SA, OUTSIDER])))

    assert declared["verdict"] == "production_non_change_proven"
    assert declared["changes"][0]["classification"] == "declared_iam_delta"
    assert undeclared["verdict"] == "explanation_or_coverage_required"
    outsider = [row for row in undeclared["changes"][0]["bindings"] if row["member"] == OUTSIDER]
    assert outsider[0]["classification"] == "requires_explanation"


def test_removed_production_binding_always_needs_explanation() -> None:
    def remove(capture):
        capture["resources"]["iam"][0]["observed"]["bindings"] = []

    reference = default_reference()
    reference["resources"]["iam"][0]["observed"]["bindings"] = [
        {"role": "roles/run.invoker", "members": [STAGING_SA]}
    ]
    certificate, reference_bytes = certify_reference(reference)

    result = run(
        after_capture(remove, base=reference), certificate=certificate, reference_bytes=reference_bytes
    )

    assert result["changes"][0]["bindings"][0]["change"] == "binding_removed"
    assert result["changes"][0]["classification"] == "requires_explanation"


def test_removed_production_resource_needs_explanation() -> None:
    def drop(capture):
        capture["resources"]["jobs"] = []

    result = run(after_capture(drop))

    assert result["changes"][0]["change"] == "resource_removed"
    assert result["changes"][0]["classification"] == "requires_explanation"


def test_private_hash_only_difference_is_marked() -> None:
    def rehash(capture):
        capture["resources"]["jobs"][0]["observed"]["template"]["env"] = [
            {"name": "TOKEN", "value": {"privateHash": "hmac-sha256:" + "2" * 64}}
        ]

    reference = default_reference()
    reference["resources"]["jobs"][0]["observed"]["template"]["env"] = [
        {"name": "TOKEN", "value": {"privateHash": "hmac-sha256:" + "1" * 64}}
    ]
    certificate, reference_bytes = certify_reference(reference)
    result = run(
        after_capture(rehash, base=reference), certificate=certificate, reference_bytes=reference_bytes
    )

    assert result["changes"][0]["private_value_only"] is True
    assert result["counts"]["private_value_only"] == 1
    assert result["verdict"] == "explanation_or_coverage_required"


def test_unexplained_capture_failure_is_a_coverage_limit() -> None:
    def fail(capture):
        capture["failures"].append(
            {"operation": "services", "resource": "x", "code": "permission_denied", "reason_code": "permission_denied", "http_status": 403}
        )

    result = run(after_capture(fail))

    assert result["verdict"] == "explanation_or_coverage_required"
    assert result["coverage_limits"][0]["resource"] == "x"
    assert len(result["retained_limitations"]) == 18


def test_bigquery_metadata_detail_is_included() -> None:
    def alter(capture):
        for row in capture["resources"]["tables"]:
            if row["name"] == table_name("permanent", "briefs"):
                row["observed"]["schema"]["fields"][0]["mode"] = "REQUIRED"

    result = run(after_capture(alter))
    table_rows = [row for row in result["bigquery_metadata"]["resources"] if row["kind"] == "tables"]

    assert table_rows[0]["name"] == table_name("permanent", "briefs")
    assert result["bigquery_metadata"]["counts"]["by_category"]["modes"] == 1
    assert result["changes"][0]["fields"] == ["schema.fields"]


@pytest.mark.parametrize(
    "target",
    [
        "certificate_pin",
        "reference_pin",
        "approval",
        "capture_digest",
        "manifest_binding",
        "wrong_region",
        "delta_pin",
        "reference_binding",
        "fixture_principal",
        "wrong_project",
    ],
)
def test_tampered_inputs_are_refused(target: str) -> None:
    _, reference_bytes, approval_bytes, _, reference_pin = fixture_bytes()
    certificate_bytes = json_bytes(certify())
    manifest_bytes = json_bytes(manifest())
    delta_bytes = json_bytes(delta(manifest_bytes))
    after = after_capture()
    certificate_pin = hashlib.sha256(certificate_bytes).hexdigest()
    delta_pin = hashlib.sha256(delta_bytes).hexdigest()
    expected = {
        "certificate_pin": "certificate_sha256_mismatch",
        "reference_pin": "reference_sha256_mismatch",
        "approval": "certificate_approval_binding_mismatch",
        "capture_digest": "capture_content_digest_mismatch",
        "manifest_binding": "delta_manifest_binding_mismatch",
        "wrong_region": "capture_regions_mismatch",
        "delta_pin": "delta_sha256_mismatch",
        "reference_binding": "certificate_reference_binding_mismatch",
        "fixture_principal": "native_principal_required",
        "wrong_project": "capture_project_mismatch",
    }[target]
    if target == "certificate_pin":
        certificate_pin = "0" * 64
    if target == "reference_pin":
        reference_pin = "0" * 64
    if target == "approval":
        approval_bytes = approval_bytes + b" "
    if target == "capture_digest":
        after["resources"]["jobs"] = []
    if target == "manifest_binding":
        manifest_bytes = manifest_bytes + b" "
    if target == "delta_pin":
        delta_pin = hashlib.sha256(b"another delta").hexdigest()
    if target == "reference_binding":
        tampered = certify()
        tampered["reference_sha256"] = "9" * 64
        tampered.pop("content_digest")
        tampered["content_digest"] = canonical_digest(tampered)
        certificate_bytes = json_bytes(tampered)
        certificate_pin = hashlib.sha256(certificate_bytes).hexdigest()
    if target == "fixture_principal":
        after["identity_evidence"]["acquisition"] = "trusted_test_fixture"
        after.pop("content_digest")
        after["content_digest"] = canonical_digest(after)
    if target == "wrong_project":
        after["project"] = "another-project"
        after.pop("content_digest")
        after["content_digest"] = canonical_digest(after)
    if target == "wrong_region":
        after["regions"] = {**after["regions"], "run": ["europe-west1"]}
        after.pop("content_digest")
        after["content_digest"] = canonical_digest(after)

    with pytest.raises(ValueError, match=expected):
        compare_production(
            before_certificate_bytes=certificate_bytes,
            reference_bytes=reference_bytes,
            approval_bytes=approval_bytes,
            after_capture_bytes=json_bytes(after),
            manifest_bytes=manifest_bytes,
            delta_bytes=delta_bytes,
            trusted_certificate_sha256=certificate_pin,
            trusted_reference_sha256=reference_pin,
            trusted_delta_sha256=delta_pin,
        )


def test_incomplete_before_certificate_is_refused() -> None:
    capture = capture_payload()
    capture["resources"]["jobs"] = []
    certificate = certify(capture=capture)
    assert certificate["complete_for_approved_scope"] is False

    with pytest.raises(ValueError, match="certificate_incomplete"):
        run(after_capture(), certificate=certificate, reference_bytes=fixture_bytes()[1])


def test_cli_writes_new_receipt_and_exit_code(tmp_path: Path, capsys) -> None:
    _, _, approval_bytes, _, _ = fixture_bytes()
    certificate, reference_bytes = certify_reference(default_reference())
    reference_pin = hashlib.sha256(reference_bytes).hexdigest()
    certificate_bytes = json_bytes(certificate)
    manifest_bytes = json_bytes(manifest())
    files = {
        "certificate": certificate_bytes,
        "reference": reference_bytes,
        "approval": approval_bytes,
        "after": json_bytes(after_capture()),
        "manifest": manifest_bytes,
        "delta": json_bytes(delta(manifest_bytes)),
    }
    for name, data in files.items():
        (tmp_path / f"{name}.json").write_bytes(data)
    argv = [
        "--before-certificate", str(tmp_path / "certificate.json"),
        "--reference", str(tmp_path / "reference.json"),
        "--approval", str(tmp_path / "approval.json"),
        "--after-capture", str(tmp_path / "after.json"),
        "--manifest", str(tmp_path / "manifest.json"),
        "--delta", str(tmp_path / "delta.json"),
        "--trusted-certificate-sha256", hashlib.sha256(certificate_bytes).hexdigest(),
        "--trusted-reference-sha256", reference_pin,
        "--trusted-delta-sha256", hashlib.sha256(files["delta"]).hexdigest(),
        "--output", str(tmp_path / "out" / "comparison.json"),
    ]

    assert main(argv) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["verdict"] == "production_non_change_proven"
    with pytest.raises(FileExistsError):
        main(argv)
    assert deepcopy(json.loads((tmp_path / "out" / "comparison.json").read_text()))["verdict"] == "production_non_change_proven"


def grant_project(bindings):
    def mutate(capture):
        capture["resources"]["iam"][0]["observed"]["bindings"] = bindings

    return mutate


def test_declared_conditional_grant_must_keep_its_condition() -> None:
    with_condition = run(
        after_capture(
            grant_project(
                [{"role": "roles/aiplatform.user", "members": [STAGING_SA], "condition": {"expression": EXPIRY, "title": "expiry"}}]
            )
        )
    )
    without_condition = run(
        after_capture(grant_project([{"role": "roles/aiplatform.user", "members": [STAGING_SA]}]))
    )

    assert with_condition["verdict"] == "production_non_change_proven"
    assert without_condition["verdict"] == "explanation_or_coverage_required"
    assert without_condition["changes"][0]["bindings"][0]["classification"] == "requires_explanation"


def test_unapproved_delta_or_amendment_declares_nothing() -> None:
    manifest_bytes = json_bytes(manifest())
    unapproved = delta(manifest_bytes)
    unapproved["approval"] = {"state": "proposed"}
    amended = delta(manifest_bytes)
    amended["amendments"] = [
        {
            "amendment": "x",
            "approval": {"state": "proposed"},
            "bindings": [
                {"resource": f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}", "role": "roles/owner", "member": OUTSIDER, "condition": None}
            ],
        }
    ]

    base = run(after_capture(grant_project([{"role": "roles/run.invoker", "members": [STAGING_SA]}])), delta_value=unapproved)
    owner = run(after_capture(grant_project([{"role": "roles/owner", "members": [OUTSIDER]}])), delta_value=amended)

    assert base["verdict"] == "explanation_or_coverage_required"
    assert owner["verdict"] == "explanation_or_coverage_required"


def test_resources_in_a_declared_dataset_that_existed_at_r00_are_never_cleared() -> None:
    reference = staging_reference()
    certificate, reference_bytes = certify_reference(reference)
    legacy = table_name("trends_v2_staging", "legacy_v1")

    def remove_table(capture):
        capture["resources"]["tables"] = [row for row in capture["resources"]["tables"] if row["name"] != legacy]

    def open_table_iam(capture):
        for row in capture["resources"]["iam"]:
            if row["name"] == legacy:
                row["observed"]["bindings"].append({"role": "roles/bigquery.dataOwner", "members": ["allUsers"]})

    def open_dataset(capture):
        for row in capture["resources"]["datasets"]:
            if row["name"] == STAGING_DATASET:
                row["observed"]["access"].append({"role": "OWNER", "specialGroup": "allAuthenticatedUsers"})

    for mutator in (remove_table, open_table_iam, open_dataset):
        result = run(
            after_capture(mutator, base=reference),
            certificate=certificate,
            reference_bytes=reference_bytes,
        )
        assert result["verdict"] == "explanation_or_coverage_required", mutator.__name__
        assert {row["classification"] for row in result["changes"]} == {"requires_explanation"}


def test_declared_dataset_access_grant_and_new_table_are_declared() -> None:
    reference = staging_reference()
    certificate, reference_bytes = certify_reference(reference)
    email = STAGING_SA.split(":", 1)[1]

    def grant_and_add(capture):
        for row in capture["resources"]["datasets"]:
            if row["name"] == STAGING_DATASET:
                row["observed"]["access"].append({"role": "READER", "userByEmail": email})
        capture["resources"]["tables"].append(
            {"name": table_name("trends_v2_staging", "answers"), "region": "US", "observed": full_table_observed("answers")}
        )

    result = run(after_capture(grant_and_add, base=reference), certificate=certificate, reference_bytes=reference_bytes)

    assert result["verdict"] == "production_non_change_proven"
    assert {row["classification"] for row in result["changes"]} == {"declared_staging_resource"}


def test_read_only_declared_table_change_needs_explanation() -> None:
    reference = staging_reference()
    reference["resources"]["tables"].append(
        {"name": LEDGER_TABLE, "region": "US", "observed": full_table_observed("ledger")}
    )
    certificate, reference_bytes = certify_reference(reference)

    def alter(capture):
        for row in capture["resources"]["tables"]:
            if row["name"] == LEDGER_TABLE:
                row["observed"]["expirationTime"] = "1"

    result = run(after_capture(alter, base=reference), certificate=certificate, reference_bytes=reference_bytes)

    assert result["changes"][0]["classification"] == "requires_explanation"


def test_scheduler_job_sharing_a_declared_run_job_path_needs_explanation() -> None:
    def add_scheduler(capture):
        capture["resources"]["schedulers"].append(
            {"name": STAGING_JOB, "region": "us-central1", "observed": {"name": STAGING_JOB, "schedule": "* * * * *"}}
        )

    result = run(after_capture(add_scheduler))

    assert result["changes"][0]["category"] == "schedulers"
    assert result["changes"][0]["classification"] == "requires_explanation"


def test_storage_statistics_growth_is_not_a_change() -> None:
    def grow(capture):
        for row in capture["resources"]["tables"]:
            row["observed"].update(numPhysicalBytes="999", numTotalLogicalBytes="999", numPartitions="9")

    result = run(after_capture(grow))

    assert [row for row in result["changes"] if row["category"] == "tables"] == []


def test_schema_only_table_blocks_the_proven_verdict_and_limits_are_listed() -> None:
    reference = staging_reference()
    reference["resources"]["tables"].append(
        {"name": table_name("trends_v2_staging", "schema_only"), "region": "US", "observed": {"schema": {"fields": []}}}
    )
    certificate, reference_bytes = certify_reference(reference)

    result = run(after_capture(base=reference), certificate=certificate, reference_bytes=reference_bytes)

    assert result["changes"] == []
    assert result["counts"]["tables_options_unproven"] >= 1
    assert result["verdict"] == "explanation_or_coverage_required"
    assert [limit["limit"] for limit in result["standing_coverage_limits"]] == [
        "routines_not_captured",
        "dataset_access_policy_version_1",
    ]


def test_missing_excluded_dataset_access_is_a_coverage_limit() -> None:
    def drop_access(capture):
        for row in capture["resources"]["datasets"]:
            if row["name"].endswith("_approved_cache_00"):
                row["observed"].pop("access")

    result = run(after_capture(drop_access))

    assert result["coverage_limits"][0]["code"] == "dataset_access_missing"
    assert result["verdict"] == "explanation_or_coverage_required"


def test_transport_normalizations_are_carried_into_the_result() -> None:
    normalization = {"operation": "list_tables", "normalization": "omitted_empty_bigquery_table_list"}

    def normalize(capture):
        capture["transport_normalizations"] = [normalization]

    result = run(after_capture(normalize))

    assert result["after_transport_normalizations"] == [normalization]


def test_declared_pair_does_not_clear_other_policy_changes() -> None:
    def audit(capture):
        policy = capture["resources"]["iam"][0]["observed"]
        policy["bindings"] = [{"role": "roles/run.invoker", "members": [STAGING_SA]}]
        policy["auditConfigs"] = [{"service": "allServices", "auditLogConfigs": [{"logType": "DATA_READ"}]}]

    result = run(after_capture(audit))

    assert result["changes"][0]["classification"] == "requires_explanation"
    assert result["verdict"] == "explanation_or_coverage_required"


def test_renamed_condition_on_an_existing_binding_needs_explanation() -> None:
    reference = default_reference()
    reference["resources"]["iam"][0]["observed"]["bindings"] = [
        {"role": "roles/aiplatform.user", "members": [STAGING_SA], "condition": {"expression": EXPIRY, "title": "until"}}
    ]
    certificate, reference_bytes = certify_reference(reference)

    def rename(capture):
        capture["resources"]["iam"][0]["observed"]["bindings"] = [
            {"role": "roles/aiplatform.user", "members": [STAGING_SA], "condition": {"expression": EXPIRY, "title": "renamed"}},
            {"role": "roles/run.invoker", "members": [STAGING_SA]},
        ]

    result = run(after_capture(rename, base=reference), certificate=certificate, reference_bytes=reference_bytes)

    assert result["changes"][0]["classification"] == "requires_explanation"


def test_dataset_configuration_change_needs_explanation_even_when_writable() -> None:
    reference = staging_reference()
    certificate, reference_bytes = certify_reference(reference)

    def reconfigure(capture):
        for row in capture["resources"]["datasets"]:
            if row["name"] == STAGING_DATASET:
                row["observed"]["defaultTableExpirationMs"] = "3600000"
                row["observed"]["labels"] = {"x": "y"}

    result = run(after_capture(reconfigure, base=reference), certificate=certificate, reference_bytes=reference_bytes)

    assert result["changes"][0]["classification"] == "requires_explanation"


def test_retained_rows_never_declare_an_addition() -> None:
    manifest_bytes = json_bytes(manifest())
    value = delta(manifest_bytes)
    value["retained"] = [
        {"resource": f"//cloudresourcemanager.googleapis.com/projects/{PROJECT}", "role": "roles/owner", "member": OUTSIDER, "condition": None}
    ]

    result = run(after_capture(grant_project([{"role": "roles/owner", "members": [OUTSIDER]}])), delta_value=value)

    assert result["verdict"] == "explanation_or_coverage_required"


def test_new_table_with_its_empty_policy_is_declared() -> None:
    reference = staging_reference()
    certificate, reference_bytes = certify_reference(reference)
    answers = table_name("trends_v2_staging", "answers")

    def add(capture):
        capture["resources"]["tables"].append({"name": answers, "region": "US", "observed": full_table_observed("answers")})
        capture["resources"]["iam"].append({"name": answers, "region": "US", "observed": {"bindings": []}})

    result = run(after_capture(add, base=reference), certificate=certificate, reference_bytes=reference_bytes)

    assert result["verdict"] == "production_non_change_proven"


def test_removed_deployable_resource_needs_explanation() -> None:
    reference = default_reference()
    reference["resources"]["jobs"].append({"name": STAGING_JOB, "region": "us-central1", "observed": {"name": STAGING_JOB}})
    certificate, reference_bytes = certify_reference(reference)

    def drop(capture):
        capture["resources"]["jobs"] = [row for row in capture["resources"]["jobs"] if row["name"] != STAGING_JOB]

    result = run(after_capture(drop, base=reference), certificate=certificate, reference_bytes=reference_bytes)

    assert result["changes"][0]["classification"] == "requires_explanation"


def test_pre_r00_table_under_a_writable_dataset_cannot_change_silently() -> None:
    reference = staging_reference()
    certificate, reference_bytes = certify_reference(reference)
    legacy = table_name("trends_v2_staging", "legacy_v1")

    def alter(capture):
        for row in capture["resources"]["tables"]:
            if row["name"] == legacy:
                row["observed"]["expirationTime"] = "1"

    result = run(after_capture(alter, base=reference), certificate=certificate, reference_bytes=reference_bytes)

    assert result["changes"][0]["classification"] == "requires_explanation"


def test_user_access_entry_maps_to_a_user_member() -> None:
    reference = staging_reference()
    certificate, reference_bytes = certify_reference(reference)
    manifest_bytes = json_bytes(manifest())
    value = delta(manifest_bytes)
    value["bindings"].append(
        {"resource": f"//bigquery.googleapis.com/{STAGING_DATASET}", "role": "roles/bigquery.dataViewer", "member": "user:analyst@example.invalid", "condition": None}
    )

    def grant(capture):
        for row in capture["resources"]["datasets"]:
            if row["name"] == STAGING_DATASET:
                row["observed"]["access"].append({"role": "READER", "userByEmail": "analyst@example.invalid"})

    result = run(
        after_capture(grant, base=reference), certificate=certificate, reference_bytes=reference_bytes, delta_value=value
    )

    assert result["verdict"] == "production_non_change_proven"


def test_metadata_change_the_protected_view_strips_still_needs_explanation() -> None:
    def relabel(capture):
        for row in capture["resources"]["tables"]:
            if row["name"] == table_name("permanent", "briefs"):
                row["observed"]["labels"] = {"etag": "changed"}

    reference_labels = default_reference()
    for row in reference_labels["resources"]["tables"]:
        if row["name"] == table_name("permanent", "briefs"):
            row["observed"]["labels"] = {"etag": "original"}
    certificate, reference_bytes = certify_reference(reference_labels)

    result = run(after_capture(relabel, base=reference_labels), certificate=certificate, reference_bytes=reference_bytes)

    assert result["verdict"] == "explanation_or_coverage_required"
    assert result["changes"][0]["change"] == "metadata_only_change"


PROD_SERVICE = f"projects/{PROJECT}/locations/us-central1/services/prod-service"


def rows_named(capture, category, name):
    return [row for row in capture["resources"][category] if row["name"] == name]


def test_declared_access_pair_does_not_clear_a_dataset_settings_change() -> None:
    reference = staging_reference()
    certificate, reference_bytes = certify_reference(reference)

    def grant_and_reconfigure(capture):
        observed = rows_named(capture, "datasets", STAGING_DATASET)[0]["observed"]
        observed["access"].append({"role": "READER", "userByEmail": STAGING_SA.split(":", 1)[1]})
        observed["defaultTableExpirationMs"] = "3600000"

    result = run(after_capture(grant_and_reconfigure, base=reference), certificate=certificate, reference_bytes=reference_bytes)

    assert result["verdict"] == "explanation_or_coverage_required"
    assert {row["classification"] for row in result["changes"]} == {"requires_explanation"}


def test_new_table_policy_outside_a_writable_dataset_needs_explanation() -> None:
    sneaky = table_name("permanent", "sneaky")

    def add(capture):
        capture["resources"]["tables"].append({"name": sneaky, "region": "US", "observed": full_table_observed("sneaky")})
        capture["resources"]["iam"].append({"name": sneaky, "region": "US", "observed": {"bindings": []}})

    result = run(after_capture(add))

    policy = [row for row in result["changes"] if row["category"] == "iam"]
    assert policy[0]["classification"] == "requires_explanation"


def test_new_policy_with_no_bindings_is_not_declared_by_an_empty_pair_list() -> None:
    def add(capture):
        capture["resources"]["iam"].append({"name": PROD_SERVICE, "region": "us-central1", "observed": {"bindings": []}})

    result = run(after_capture(add))

    assert result["changes"][0]["category"] == "iam"
    assert result["changes"][0]["classification"] == "requires_explanation"
    assert result["verdict"] == "explanation_or_coverage_required"


LEGACY_TABLE = table_name("trends_v2_staging", "legacy_v1")
PROD_SCHEDULER = f"projects/{PROJECT}/locations/us-central1/jobs/prod-scheduler"
SOURCES_DATASET = dataset_name("intelligence_42_sources_staging")


def delta_with(pairs=(), **extra) -> dict:
    value = delta(json_bytes(manifest()))
    for resource, role, member in pairs:
        value["bindings"].append({"resource": resource, "role": role, "member": member, "condition": None})
    value.update(extra)
    return value


def certified(mutate_reference):
    reference = staging_reference()
    mutate_reference(reference)
    return reference, *certify_reference(reference)


def test_declared_table_policy_pair_does_not_hide_a_label_change_on_that_table() -> None:
    def labels(value):
        def mutate(capture):
            rows_named(capture, "tables", LEGACY_TABLE)[0]["observed"]["labels"] = {"etag": value}

        return mutate

    reference, certificate, reference_bytes = certified(labels("original"))
    value = delta_with([(f"//bigquery.googleapis.com/{LEGACY_TABLE}", "roles/bigquery.dataViewer", STAGING_SA)])

    def relabel_and_grant(capture):
        labels("changed")(capture)
        rows_named(capture, "iam", LEGACY_TABLE)[0]["observed"]["bindings"].append(
            {"role": "roles/bigquery.dataViewer", "members": [STAGING_SA]}
        )

    def grant_only(capture):
        rows_named(capture, "iam", LEGACY_TABLE)[0]["observed"]["bindings"].append(
            {"role": "roles/bigquery.dataViewer", "members": [STAGING_SA]}
        )

    result = run(after_capture(relabel_and_grant, base=reference), certificate=certificate, reference_bytes=reference_bytes, delta_value=value)
    granted = run(after_capture(grant_only, base=reference), certificate=certificate, reference_bytes=reference_bytes, delta_value=value)

    assert result["verdict"] == "explanation_or_coverage_required"
    backstop = [row for row in result["changes"] if row["change"] == "metadata_only_change"]
    assert backstop[0]["category"] == "tables"
    assert backstop[0]["fields"] == ["labels.etag"]
    assert granted["verdict"] == "production_non_change_proven"


def test_declared_dataset_access_pair_does_not_hide_a_volatile_named_label_change() -> None:
    def labels(value):
        def mutate(capture):
            rows_named(capture, "datasets", STAGING_DATASET)[0]["observed"]["labels"] = {"reconciling": value}

        return mutate

    reference, certificate, reference_bytes = certified(labels("no"))

    def relabel_and_grant(capture):
        labels("yes")(capture)
        rows_named(capture, "datasets", STAGING_DATASET)[0]["observed"]["access"].append(
            {"role": "READER", "userByEmail": STAGING_SA.split(":", 1)[1]}
        )

    result = run(after_capture(relabel_and_grant, base=reference), certificate=certificate, reference_bytes=reference_bytes)

    assert result["verdict"] == "explanation_or_coverage_required"
    backstop = [row for row in result["changes"] if row["change"] == "metadata_only_change"]
    assert backstop[0]["category"] == "datasets"
    assert backstop[0]["fields"] == ["labels.reconciling"]


@pytest.mark.parametrize(
    ("category", "place"),
    [
        ("services", lambda observed, value: observed.update(labels={"etag": value})),
        ("services", lambda observed, value: observed["template"].update(annotations={"conditions": value})),
        (
            "schedulers",
            lambda observed, value: observed.update(
                httpTarget={"uri": "https://example.invalid/run", "headers": {"etag": value}}
            ),
        ),
    ],
    ids=["service_label", "service_template_annotation", "scheduler_header"],
)
def test_volatile_named_keys_inside_user_keyed_maps_are_compared(category, place) -> None:
    def build(value):
        def mutate(capture):
            if not capture["resources"]["schedulers"]:
                capture["resources"]["schedulers"].append(
                    {"name": PROD_SCHEDULER, "region": "us-central1", "observed": {"name": PROD_SCHEDULER, "schedule": "0 * * * *", "state": "ENABLED"}}
                )
            place(capture["resources"][category][0]["observed"], value)

        return mutate

    reference = default_reference()
    build("a")(reference)
    certificate, reference_bytes = certify_reference(reference)

    result = run(after_capture(build("b"), base=reference), certificate=certificate, reference_bytes=reference_bytes)

    assert result["verdict"] == "explanation_or_coverage_required"
    assert result["changes"][0]["category"] == category
    assert result["changes"][0]["classification"] == "requires_explanation"


def scheduler_reference() -> tuple[dict, dict, bytes]:
    reference = default_reference()
    reference["resources"]["schedulers"].append(
        {
            "name": PROD_SCHEDULER,
            "region": "us-central1",
            "observed": {
                "name": PROD_SCHEDULER,
                "schedule": "0 * * * *",
                "state": "ENABLED",
                "scheduleTime": "2026-09-01T01:00:00Z",
                "lastAttemptTime": "2026-09-01T00:00:00Z",
                "status": {},
            },
        }
    )
    return reference, *certify_reference(reference)


def test_production_scheduler_that_ran_is_not_a_change_but_its_configuration_is() -> None:
    reference, certificate, reference_bytes = scheduler_reference()

    def ran(capture):
        observed = capture["resources"]["schedulers"][0]["observed"]
        observed.update(
            scheduleTime="2026-09-25T01:00:00Z",
            lastAttemptTime="2026-09-25T00:00:00Z",
            status={"code": 2, "message": "failed"},
        )

    def paused(capture):
        ran(capture)
        capture["resources"]["schedulers"][0]["observed"]["state"] = "PAUSED"

    def rescheduled(capture):
        capture["resources"]["schedulers"][0]["observed"]["schedule"] = "* * * * *"

    results = {
        mutator.__name__: run(after_capture(mutator, base=reference), certificate=certificate, reference_bytes=reference_bytes)
        for mutator in (ran, paused, rescheduled)
    }

    assert results["ran"]["verdict"] == "production_non_change_proven"
    assert results["paused"]["changes"][0]["fields"] == ["state"]
    assert results["paused"]["verdict"] == "explanation_or_coverage_required"
    assert results["rescheduled"]["verdict"] == "explanation_or_coverage_required"


def sources_inputs(create: bool) -> tuple[dict, dict]:
    value = manifest()
    value["resources"].append({"actions": ["read", "write"], "name": f"//bigquery.googleapis.com/{SOURCES_DATASET}"})
    declared = delta(json_bytes(value))
    declared["bindings"].append(
        {"resource": f"//bigquery.googleapis.com/{SOURCES_DATASET}", "role": "roles/bigquery.dataViewer", "member": STAGING_SA, "condition": None}
    )
    if create:
        declared["create"] = [
            {
                "evidence": ["r03-native-dataset-intelligence_42_sources_staging.json"],
                "existence": "absent_by_native_discovery",
                "note": "Source dataset.",
                "resource": f"//bigquery.googleapis.com/{SOURCES_DATASET}",
            }
        ]
    return value, declared


def add_sources(*access):
    def mutate(capture):
        capture["resources"]["datasets"].append(
            {
                "name": SOURCES_DATASET,
                "region": "US",
                "observed": {
                    "datasetReference": {"projectId": PROJECT, "datasetId": "intelligence_42_sources_staging"},
                    "location": "US",
                    "access": list(access),
                },
            }
        )

    return mutate


def test_dataset_the_approved_delta_creates_is_declared_when_its_access_is_declared() -> None:
    email = STAGING_SA.split(":", 1)[1]
    reader = {"role": "READER", "userByEmail": email}
    outsider = {"role": "OWNER", "userByEmail": "outsider@example.invalid"}
    value, created = sources_inputs(create=True)
    _, not_created = sources_inputs(create=False)
    unapproved = deepcopy(created)
    unapproved["approval"] = {"state": "proposed"}

    declared = run(after_capture(add_sources(reader)), manifest_value=value, delta_value=created)
    undeclared_entry = run(after_capture(add_sources(reader, outsider)), manifest_value=value, delta_value=created)
    not_listed = run(after_capture(add_sources(reader)), manifest_value=value, delta_value=not_created)
    not_approved = run(after_capture(add_sources(reader)), manifest_value=value, delta_value=unapproved)

    assert declared["verdict"] == "production_non_change_proven"
    assert declared["changes"][0]["classification"] == "declared_staging_resource"
    assert undeclared_entry["verdict"] == "explanation_or_coverage_required"
    assert not_listed["verdict"] == "explanation_or_coverage_required"
    assert not_approved["verdict"] == "explanation_or_coverage_required"


def test_approved_routine_authorization_entry_is_declared() -> None:
    reference = staging_reference()
    certificate, reference_bytes = certify_reference(reference)
    dataset = f"//bigquery.googleapis.com/{STAGING_DATASET}"
    authorizations = [
        {"dataset": dataset, "role": "roles/bigquery.routineDataEditor", "routine": f"{dataset}/routines/sp_record"},
        {"dataset": dataset, "role": None, "routine": f"{dataset}/routines/fn_check", "routine_type": "SCALAR_FUNCTION"},
    ]
    value = delta_with(routine_authorizations=authorizations)

    def entry(routine_id, role="roles/bigquery.routineDataEditor", project=PROJECT):
        item = {"routine": {"projectId": project, "datasetId": "trends_v2_staging", "routineId": routine_id}}
        if role is not None:
            item["role"] = role
        return item

    def authorize(*entries):
        def mutate(capture):
            rows_named(capture, "datasets", STAGING_DATASET)[0]["observed"]["access"].extend(entries)

        return mutate

    def verdict(*entries, delta_value=value):
        return run(
            after_capture(authorize(*entries), base=reference),
            certificate=certificate,
            reference_bytes=reference_bytes,
            delta_value=delta_value,
        )["verdict"]

    assert verdict(entry("sp_record"), entry("fn_check", role=None)) == "production_non_change_proven"
    assert verdict(entry("sp_other")) == "explanation_or_coverage_required"
    assert verdict(entry("sp_record", role="roles/bigquery.dataOwner")) == "explanation_or_coverage_required"
    assert verdict(entry("sp_record", project="another-project")) == "explanation_or_coverage_required"
    unapproved = deepcopy(value)
    unapproved["approval"] = {"state": "proposed"}
    assert verdict(entry("sp_record"), delta_value=unapproved) == "explanation_or_coverage_required"


def test_stale_after_capture_is_refused() -> None:
    certificate, reference_bytes = certify_reference(default_reference())

    def earlier(capture):
        capture["observed_at"] = R00_OBSERVED_AT

    def missing(capture):
        capture.pop("observed_at")

    for mutator, error in ((earlier, "after_capture_not_later_than_reference"), (missing, "capture_observed_at_invalid")):
        with pytest.raises(ValueError, match=error):
            run(after_capture(mutator), certificate=certificate, reference_bytes=reference_bytes)


def test_after_capture_that_is_the_source_capture_is_refused() -> None:
    reference = default_reference()
    certificate, reference_bytes = certify_reference(reference)
    source = deepcopy(reference)
    source["identity_evidence"] = native_identity_evidence()
    source["failures"] = allowed_failures()
    source.pop("content_digest")
    source["content_digest"] = canonical_digest(source)
    assert hashlib.sha256(json_bytes(source)).hexdigest() == certificate["source_capture_sha256"]

    with pytest.raises(ValueError, match="after_capture_is_source_capture"):
        run(source, certificate=certificate, reference_bytes=reference_bytes)


def test_source_capture_saved_again_in_another_byte_form_is_refused() -> None:
    reference = default_reference()
    certificate, reference_bytes = certify_reference(reference)
    source = deepcopy(reference)
    source["identity_evidence"] = native_identity_evidence()
    source["failures"] = allowed_failures()
    source.pop("content_digest")
    source["content_digest"] = canonical_digest(source)
    certificate_bytes = json_bytes(certificate)
    manifest_bytes = json_bytes(manifest())
    delta_bytes = json_bytes(delta(manifest_bytes))
    _, _, approval_bytes, _, _ = fixture_bytes()
    compact = json.dumps(source, separators=(",", ":")).encode()
    unsorted_crlf = json.dumps(dict(reversed(list(source.items()))), indent=2).replace("\n", "\r\n").encode()
    for after_bytes in (compact, unsorted_crlf):
        assert hashlib.sha256(after_bytes).hexdigest() != certificate["source_capture_sha256"]
        with pytest.raises(ValueError, match="after_capture_is_source_capture"):
            compare_production(
                before_certificate_bytes=certificate_bytes,
                reference_bytes=reference_bytes,
                approval_bytes=approval_bytes,
                after_capture_bytes=after_bytes,
                manifest_bytes=manifest_bytes,
                delta_bytes=delta_bytes,
                trusted_certificate_sha256=hashlib.sha256(certificate_bytes).hexdigest(),
                trusted_reference_sha256=hashlib.sha256(reference_bytes).hexdigest(),
                trusted_delta_sha256=hashlib.sha256(delta_bytes).hexdigest(),
            )


def test_created_dataset_with_bigquery_default_access_is_declared() -> None:
    email = STAGING_SA.split(":", 1)[1]
    reader = {"role": "READER", "userByEmail": email}
    defaults = [
        {"role": "OWNER", "specialGroup": "projectOwners"},
        {"role": "WRITER", "specialGroup": "projectWriters"},
        {"role": "READER", "specialGroup": "projectReaders"},
    ]
    creator = {"role": "OWNER", "userByEmail": native_identity_evidence()["principal"]}
    value, created = sources_inputs(create=True)

    def verdict(*access, delta_value=created):
        return run(after_capture(add_sources(*access)), manifest_value=value, delta_value=delta_value)["verdict"]

    assert verdict(reader, *defaults, creator) == "production_non_change_proven"
    assert verdict(reader, *defaults) == "production_non_change_proven"
    assert verdict(reader, {"role": "OWNER", "specialGroup": "allAuthenticatedUsers"}) == "explanation_or_coverage_required"
    assert verdict(reader, {"role": "OWNER", "specialGroup": "projectReaders"}) == "explanation_or_coverage_required"
    assert verdict(reader, {"role": "OWNER", "userByEmail": "outsider@example.invalid"}) == "explanation_or_coverage_required"
    _, not_created = sources_inputs(create=False)
    assert verdict(reader, *defaults, delta_value=not_created) == "explanation_or_coverage_required"


def test_default_access_entries_are_not_declared_on_an_existing_dataset() -> None:
    reference = staging_reference()
    certificate, reference_bytes = certify_reference(reference)

    def grant(capture):
        rows_named(capture, "datasets", STAGING_DATASET)[0]["observed"]["access"].append(
            {"role": "OWNER", "specialGroup": "projectOwners"}
        )

    result = run(after_capture(grant, base=reference), certificate=certificate, reference_bytes=reference_bytes)
    assert result["verdict"] == "explanation_or_coverage_required"


def test_routine_shaped_iam_member_is_not_a_routine_authorization() -> None:
    reference = staging_reference()
    certificate, reference_bytes = certify_reference(reference)
    dataset = f"//bigquery.googleapis.com/{STAGING_DATASET}"
    value = delta_with(
        routine_authorizations=[
            {"dataset": dataset, "role": "roles/bigquery.routineDataEditor", "routine": f"{dataset}/routines/sp_record"}
        ]
    )
    forged = f"routine://bigquery.googleapis.com/projects/{PROJECT}/datasets/trends_v2_staging/routines/sp_record"

    def forge(capture):
        access = rows_named(capture, "datasets", STAGING_DATASET)[0]["observed"]["access"]
        access.append({"role": "roles/bigquery.routineDataEditor", "iamMember": forged})

    result = run(
        after_capture(forge, base=reference),
        certificate=certificate,
        reference_bytes=reference_bytes,
        delta_value=value,
    )
    assert result["verdict"] == "explanation_or_coverage_required"
