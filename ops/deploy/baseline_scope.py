import hashlib
import json
import re
from copy import deepcopy

from ops.deploy.production_baseline import _canonical_digest, compare_baselines

PROJECT = "ogilvy-trends-v2"
REGIONS = {
    "run": ["us-central1"],
    "scheduler": ["us-central1"],
    "tasks": ["us-central1"],
    "bigquery": ["US"],
}
CATEGORIES = ("jobs", "services", "schedulers", "queues", "datasets", "tables", "iam")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DATASET_RE = re.compile(rf"^projects/{PROJECT}/datasets/[^/]+$")
TABLE_RE = re.compile(rf"^projects/{PROJECT}/datasets/[^/]+/tables/[^/]+$")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_object(value: bytes, label: str) -> dict:
    try:
        parsed = json.loads(value.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label}_invalid_json") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{label}_not_object")
    return parsed


def _verify_content_digest(value: dict, label: str) -> str:
    reported = value.get("content_digest")
    material = {key: item for key, item in value.items() if key != "content_digest"}
    if reported != _canonical_digest(material):
        raise ValueError(f"{label}_content_digest_mismatch")
    return reported


def _dataset_name(dataset_id: str) -> str:
    return f"projects/{PROJECT}/datasets/{dataset_id}"


def _table_name(dataset_id: str, table_id: str) -> str:
    return f"{_dataset_name(dataset_id)}/tables/{table_id}"


def _validate_approval(approval: dict) -> dict:
    if approval.get("schema_version") != "42_r00_scope_approval_v1":
        raise ValueError("approval_schema_mismatch")
    if approval.get("decision") != "approved":
        raise ValueError("approval_decision_mismatch")
    delta_sha256 = approval.get("delta_sha256")
    if not isinstance(delta_sha256, str) or not SHA256_RE.fullmatch(delta_sha256):
        raise ValueError("approval_delta_mismatch")
    if approval.get("project") != PROJECT:
        raise ValueError("approval_project_mismatch")
    excluded = approval.get("excluded_table_namespaces")
    if (
        not isinstance(excluded, list)
        or len(excluded) != 14
        or len(excluded) != len(set(excluded))
        or any(not isinstance(name, str) or not DATASET_RE.fullmatch(name) for name in excluded)
    ):
        raise ValueError("approval_excluded_scope_mismatch")
    unsupported = approval.get("unsupported_table_iam_resources")
    if (
        not isinstance(unsupported, list)
        or len(unsupported) != 5
        or len(unsupported) != len(set(unsupported))
        or any(
            not isinstance(name, str)
            or not TABLE_RE.fullmatch(name)
            or _table_parent(name) not in set(excluded)
            for name in unsupported
        )
    ):
        raise ValueError("approval_unsupported_scope_mismatch")
    if approval.get("retain_dataset_metadata_and_iam") is not True:
        raise ValueError("approval_dataset_retention_missing")
    if approval.get("exclude_future_namespaces") is not False:
        raise ValueError("approval_future_scope_invalid")
    user_decision = approval.get("user_decision")
    decision_evidence_sha256 = (
        user_decision.get("evidence_sha256")
        if isinstance(user_decision, dict)
        else None
    )
    if not isinstance(decision_evidence_sha256, str) or not SHA256_RE.fullmatch(
        decision_evidence_sha256
    ):
        raise ValueError("approval_user_decision_mismatch")
    return {
        "delta_sha256": delta_sha256,
        "excluded_datasets": tuple(excluded),
        "unsupported_table_iam": tuple(unsupported),
        "decision_evidence_sha256": decision_evidence_sha256,
    }


def _validate_native_principal(capture: dict) -> dict:
    evidence = capture.get("identity_evidence")
    if not isinstance(evidence, dict):
        raise ValueError("native_principal_missing")
    material = {
        "principal": evidence.get("principal"),
        "credential_fingerprint": evidence.get("credential_fingerprint"),
        "acquisition": evidence.get("acquisition"),
        "no_fallback": evidence.get("no_fallback"),
    }
    if evidence.get("acquisition") != "gcloud_named_account_token_no_impersonation":
        raise ValueError("native_principal_required")
    if (
        not isinstance(material["principal"], str)
        or material["principal"] != capture.get("identity")
        or not isinstance(material["credential_fingerprint"], str)
        or not FINGERPRINT_RE.fullmatch(material["credential_fingerprint"])
        or material["no_fallback"] is not True
        or evidence.get("evidence_digest") != _canonical_digest(material)
    ):
        raise ValueError("native_principal_invalid")
    return {**material, "evidence_digest": evidence["evidence_digest"]}


def _resource_rows(value: dict, label: str) -> dict[str, list[dict]]:
    resources = value.get("resources")
    if not isinstance(resources, dict):
        raise ValueError(f"{label}_resources_missing")
    result = {}
    for category in CATEGORIES:
        rows = resources.get(category)
        if not isinstance(rows, list) or any(
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not row["name"]
            for row in rows
        ):
            raise ValueError(f"{label}_{category}_invalid")
        names = [row["name"] for row in rows]
        if len(names) != len(set(names)):
            raise ValueError(f"{label}_{category}_duplicate")
        result[category] = deepcopy(rows)
    return result


def _table_parent(name: str) -> str | None:
    if "/tables/" not in name:
        return None
    return name.rsplit("/tables/", 1)[0]


def _protected_resources(
    resources: dict[str, list[dict]], excluded: set[str]
) -> dict[str, list[dict]]:
    protected = {}
    for category, rows in resources.items():
        if category in {"tables", "iam"}:
            rows = [row for row in rows if _table_parent(row["name"]) not in excluded]
        protected[category] = sorted(deepcopy(rows), key=lambda row: row["name"])
    return protected


def _name_inventory(resources: dict[str, list[dict]]) -> dict[str, list[str]]:
    return {
        category: sorted(row["name"] for row in resources[category])
        for category in CATEGORIES
    }


def _inventory_mismatch(expected: dict, observed: dict) -> dict:
    mismatch = {}
    for category in CATEGORIES:
        expected_names = set(expected[category])
        observed_names = set(observed[category])
        missing = sorted(expected_names - observed_names)
        added = sorted(observed_names - expected_names)
        if missing or added:
            mismatch[category] = {"missing": missing, "added": added}
    return mismatch


def _limitation_key(failure: dict) -> tuple:
    return (
        failure.get("operation"),
        failure.get("resource"),
        failure.get("reason_code"),
        failure.get("http_status"),
    )


def _table_list_dataset(resource: object) -> str | None:
    prefix = (
        "https://bigquery.googleapis.com/bigquery/v2/"
        f"projects/{PROJECT}/datasets/"
    )
    suffix = "/tables"
    if (
        not isinstance(resource, str)
        or not resource.startswith(prefix)
        or not resource.endswith(suffix)
    ):
        return None
    dataset_id = resource[len(prefix) : -len(suffix)]
    if not dataset_id or "/" in dataset_id:
        return None
    return _dataset_name(dataset_id)


def _classify_failures(
    capture: dict, excluded: set[str]
) -> tuple[list[dict], list[dict]]:
    failures = capture.get("failures")
    if not isinstance(failures, list) or any(
        not isinstance(failure, dict) for failure in failures
    ):
        raise ValueError("capture_failures_invalid")
    retained = []
    unexplained = []
    seen = set()
    for failure in failures:
        key = _limitation_key(failure)
        table_parent = _table_parent(failure.get("resource", ""))
        permitted_list = (
            failure.get("operation") == "list_tables"
            and _table_list_dataset(failure.get("resource")) in excluded
        )
        permitted_iam = (
            failure.get("operation") == "iam"
            and table_parent in excluded
            and failure.get("reason_code") == "invalid_request"
            and failure.get("http_status") == 400
        )
        if (permitted_list or permitted_iam) and key not in seen:
            retained.append(deepcopy(failure))
            seen.add(key)
        else:
            unexplained.append(deepcopy(failure))
    return retained, unexplained


def _validate_excluded_dataset_access(
    resources: dict[str, list[dict]], excluded: set[str], unexplained: list[dict]
) -> None:
    datasets = {row["name"]: row for row in resources["datasets"]}
    for name in sorted(excluded):
        row = datasets.get(name)
        observed = row.get("observed") if row else None
        if not isinstance(observed, dict) or not isinstance(observed.get("access"), list):
            unexplained.append(
                {
                    "operation": "scope_validation",
                    "resource": name,
                    "code": "dataset_access_missing",
                }
            )


def certify_protected_scope(
    capture_bytes: bytes,
    reference_bytes: bytes,
    approval_bytes: bytes,
    *,
    trusted_approval_sha256: str,
    trusted_reference_sha256: str,
) -> dict:
    if not SHA256_RE.fullmatch(trusted_approval_sha256):
        raise ValueError("trusted_approval_sha256_invalid")
    if not SHA256_RE.fullmatch(trusted_reference_sha256):
        raise ValueError("trusted_reference_sha256_invalid")
    approval_sha256 = _sha256(approval_bytes)
    reference_sha256 = _sha256(reference_bytes)
    if approval_sha256 != trusted_approval_sha256:
        raise ValueError("approval_sha256_mismatch")
    if reference_sha256 != trusted_reference_sha256:
        raise ValueError("reference_sha256_mismatch")

    approval = _json_object(approval_bytes, "approval")
    reference = _json_object(reference_bytes, "reference")
    capture = _json_object(capture_bytes, "capture")
    approved_scope = _validate_approval(approval)
    excluded = set(approved_scope["excluded_datasets"])
    reference_content_digest = _verify_content_digest(reference, "reference")
    capture_content_digest = _verify_content_digest(capture, "capture")
    if capture.get("complete") is not False:
        raise ValueError("capture_complete_flag_changed")
    if reference.get("project") != PROJECT:
        raise ValueError("reference_project_mismatch")
    if capture.get("project") != PROJECT:
        raise ValueError("capture_project_mismatch")
    if reference.get("regions") != REGIONS:
        raise ValueError("reference_regions_mismatch")
    if capture.get("regions") != REGIONS:
        raise ValueError("capture_regions_mismatch")
    principal = _validate_native_principal(capture)

    reference_resources = _protected_resources(
        _resource_rows(reference, "reference"), excluded
    )
    capture_all_resources = _resource_rows(capture, "capture")
    capture_resources = _protected_resources(capture_all_resources, excluded)
    expected_inventory = _name_inventory(reference_resources)
    observed_inventory = _name_inventory(capture_resources)
    inventory_mismatch = _inventory_mismatch(expected_inventory, observed_inventory)
    retained, unexplained = _classify_failures(capture, excluded)
    _validate_excluded_dataset_access(capture_all_resources, excluded, unexplained)
    inventory_digest = _canonical_digest(expected_inventory)
    transport_normalizations = capture.get("transport_normalizations", [])
    if not isinstance(transport_normalizations, list) or any(
        not isinstance(normalization, dict)
        for normalization in transport_normalizations
    ):
        raise ValueError("capture_transport_normalizations_invalid")

    certificate = {
        "schema_version": "42_r00_protected_scope_certificate_v1",
        "project": PROJECT,
        "regions": deepcopy(capture["regions"]),
        "source_capture_sha256": _sha256(capture_bytes),
        "source_capture_content_digest": capture_content_digest,
        "source_capture_complete": capture["complete"],
        "source_principal_evidence": principal,
        "source_transport_normalizations": deepcopy(transport_normalizations),
        "reference_sha256": reference_sha256,
        "reference_content_digest": reference_content_digest,
        "approval_sha256": approval_sha256,
        "delta_sha256": approved_scope["delta_sha256"],
        "decision_evidence_sha256": approved_scope["decision_evidence_sha256"],
        "protected_inventory_digest": inventory_digest,
        "complete_for_approved_scope": not inventory_mismatch and not unexplained,
        "protected_resources": capture_resources,
        "retained_limitations": retained,
        "unexplained_failures": unexplained,
        "inventory_mismatch": inventory_mismatch,
    }
    certificate["content_digest"] = _canonical_digest(certificate)
    return certificate


def _verify_certificate(certificate: dict) -> None:
    if not isinstance(certificate, dict):
        raise ValueError("certificate_not_object")
    _verify_content_digest(certificate, "certificate")
    if certificate.get("schema_version") != "42_r00_protected_scope_certificate_v1":
        raise ValueError("certificate_schema_mismatch")
    if certificate.get("complete_for_approved_scope") is not True:
        raise ValueError("certificate_incomplete")
    resources = _resource_rows(
        {"resources": certificate.get("protected_resources")}, "certificate"
    )
    if certificate.get("protected_inventory_digest") != _canonical_digest(
        _name_inventory(resources)
    ):
        raise ValueError("certificate_inventory_digest_mismatch")


def compare_protected_scopes(before_certificate: dict, after_certificate: dict) -> list[dict]:
    _verify_certificate(before_certificate)
    _verify_certificate(after_certificate)
    compatibility = (
        "project",
        "regions",
        "approval_sha256",
        "delta_sha256",
        "reference_sha256",
        "protected_inventory_digest",
        "decision_evidence_sha256",
    )
    if any(
        before_certificate.get(field) != after_certificate.get(field)
        for field in compatibility
    ):
        raise ValueError("certificate_scope_mismatch")
    return compare_baselines(
        {"complete": True, "resources": before_certificate["protected_resources"]},
        {"complete": True, "resources": after_certificate["protected_resources"]},
    )
