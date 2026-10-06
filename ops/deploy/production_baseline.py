import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

REQUIRED_REGION_GROUPS = ("run", "scheduler", "tasks", "bigquery")
VOLATILE_FIELDS = {
    "createTime",
    "updateTime",
    "deleteTime",
    "expireTime",
    "lastModifiedTime",
    "lastAttemptTime",
    "userUpdateTime",
    "purgeTime",
    "observedGeneration",
    "conditions",
    "terminalCondition",
    "latestCreatedExecution",
    "executionCount",
    "reconciling",
    "etag",
    "numRows",
    "numBytes",
    "numLongTermBytes",
    "numTimeTravelPhysicalBytes",
    "streamingBuffer",
}
SENSITIVE_KEYS = {
    "authorization",
    "password",
    "token",
    "access_token",
    "api_key",
    "apikey",
    "client_secret",
}
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CLOUD_TASKS_QUEUES_LIST_RE = re.compile(
    r"^https://cloudtasks\.googleapis\.com/v2/projects/[^/]+/locations/[^/]+/queues$"
)
PRINCIPAL_ACQUISITIONS = {
    "trusted_test_fixture",
    "gcloud_named_account_token_no_impersonation",
}


class ReadClient(Protocol):
    def authenticated_principal_evidence(self) -> dict: ...

    def session_fingerprint(self) -> str: ...

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict: ...


class NativeReadClient:
    def __init__(self, session, principal_evidence: dict, timeout: int = 30):
        self.session = session
        self._principal_evidence = deepcopy(principal_evidence)
        self.timeout = timeout

    def authenticated_principal_evidence(self) -> dict:
        return deepcopy(self._principal_evidence)

    def session_fingerprint(self) -> str:
        authorization = self.session.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise ValueError("session_authorization_missing")
        token = authorization.removeprefix("Bearer ")
        return f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        response = self.session.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("native_response_not_object")
        return payload


def _validate_inventory(inventory: dict) -> tuple[str, dict[str, list[str]]]:
    if not isinstance(inventory, dict):
        raise ValueError("inventory_not_object")
    project = inventory.get("project")
    if not isinstance(project, str) or not project.strip():
        raise ValueError("inventory_missing:project")
    regions = inventory.get("regions")
    if not isinstance(regions, dict):
        raise ValueError("inventory_missing:regions")
    checked: dict[str, list[str]] = {}
    for group in REQUIRED_REGION_GROUPS:
        values = regions.get(group)
        if not isinstance(values, list) or not values:
            raise ValueError(f"inventory_missing:{group}")
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f"inventory_invalid:{group}")
        if len(values) != len(set(values)):
            raise ValueError(f"inventory_duplicate:{group}")
        checked[group] = list(values)
    return project, checked


def _private_hash(value: str, key: bytes) -> dict[str, str]:
    digest = hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"privateHash": f"hmac-sha256:{digest}"}


def _sanitize(value, key: bytes, *, in_secret_reference: bool = False):
    if isinstance(value, list):
        return [_sanitize(item, key, in_secret_reference=in_secret_reference) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for field, item in value.items():
        lowered = field.casefold()
        if field == "env" and isinstance(item, list):
            clean_env = []
            for entry in item:
                if isinstance(entry, dict) and "value" in entry:
                    entry = deepcopy(entry)
                    entry["value"] = _private_hash(str(entry["value"]), key)
                clean_env.append(_sanitize(entry, key))
            result[field] = clean_env
        elif lowered == "headers" and isinstance(item, dict):
            result[field] = {
                header: _private_hash(str(header_value), key)
                for header, header_value in item.items()
            }
        elif lowered == "body" and isinstance(item, (str, bytes)):
            body = item.decode("utf-8", errors="strict") if isinstance(item, bytes) else item
            result[field] = _private_hash(body, key)
        elif lowered in SENSITIVE_KEYS and not in_secret_reference and isinstance(item, str):
            result[field] = _private_hash(item, key)
        else:
            is_reference = in_secret_reference or lowered in {
                "secretkeyref",
                "secretkeyselector",
                "secretref",
            }
            result[field] = _sanitize(item, key, in_secret_reference=is_reference)
    return result


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _principal_evidence(
    principal: str,
    credential_fingerprint: str,
    acquisition: str,
) -> dict:
    material = {
        "principal": principal,
        "credential_fingerprint": credential_fingerprint,
        "acquisition": acquisition,
        "no_fallback": True,
    }
    return {**material, "evidence_digest": _canonical_digest(material)}


def _validated_principal_evidence(client: ReadClient, expected_identity: str) -> dict:
    try:
        evidence = client.authenticated_principal_evidence()
    except Exception as error:
        raise ValueError("principal_evidence_unavailable") from error
    if not isinstance(evidence, dict):
        raise ValueError("principal_evidence_missing")
    material = {
        "principal": evidence.get("principal"),
        "credential_fingerprint": evidence.get("credential_fingerprint"),
        "acquisition": evidence.get("acquisition"),
        "no_fallback": evidence.get("no_fallback"),
    }
    if (
        not all(isinstance(material[field], str) and material[field] for field in ("principal", "credential_fingerprint", "acquisition"))
        or material["no_fallback"] is not True
        or not FINGERPRINT_RE.fullmatch(material["credential_fingerprint"])
        or material["acquisition"] not in PRINCIPAL_ACQUISITIONS
        or evidence.get("evidence_digest") != _canonical_digest(material)
    ):
        raise ValueError("principal_evidence_invalid")
    if material["principal"].casefold() != expected_identity.casefold():
        raise ValueError("principal_mismatch")
    try:
        current_fingerprint = client.session_fingerprint()
    except Exception as error:
        raise ValueError("session_binding_unavailable") from error
    if current_fingerprint != material["credential_fingerprint"]:
        raise ValueError("session_binding_mismatch")
    return {**material, "evidence_digest": evidence["evidence_digest"]}


def _http_status(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(error, "status_code", None)
    return status if isinstance(status, int) and 100 <= status <= 599 else None


def _exception_reason(error: Exception) -> tuple[str, str, int | None]:
    status = _http_status(error)
    if status is not None:
        reasons = {
            400: "invalid_request",
            401: "unauthenticated",
            403: "permission_denied",
            404: "not_found",
            409: "conflict",
            429: "rate_limited",
        }
        return reasons.get(status, "provider_error" if status >= 500 else "http_error"), "http", status
    if isinstance(error, PermissionError):
        return "permission_denied", "permission", None
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout", "timeout", None
    if isinstance(error, (ConnectionError, OSError)):
        return "transport_error", "transport", None
    if isinstance(error, ValueError):
        return "invalid_response", "validation", None
    return "client_error", "client", None


def _failure(
    operation: str,
    resource: str,
    error: Exception | None = None,
    code: str = "read_failed",
    *,
    field: str | None = None,
    item_index: int | None = None,
) -> dict:
    result = {"operation": operation, "resource": resource}
    if error is not None:
        reason, category, status = _exception_reason(error)
        result.update(
            {
                "code": reason,
                "reason_code": reason,
                "exception_category": category,
            }
        )
        if status is not None:
            result["http_status"] = status
    else:
        result.update({"code": code, "reason_code": code, "exception_category": "validation"})
    if field is not None:
        result["field"] = field
    if item_index is not None:
        result["item_index"] = item_index
    return result


def _list_pages(
    client: ReadClient,
    url: str,
    collection: str,
    operation: str,
    failures: list[dict],
    *,
    base_params: dict | None = None,
    transport_normalizations: list[dict] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    token = ""
    seen: set[str] = set()
    while True:
        params = dict(base_params or {})
        if token:
            params["pageToken"] = token
        try:
            payload = client.request_json("GET", url, params=params)
        except Exception as error:
            failures.append(_failure(operation, url, error))
            return rows
        if collection not in payload:
            if (
                operation == "list_queues"
                and collection == "queues"
                and token
                and rows
                and payload == {}
                and CLOUD_TASKS_QUEUES_LIST_RE.fullmatch(url)
                and transport_normalizations is not None
            ):
                transport_normalizations.append(
                    {
                        "operation": operation,
                        "resource": url,
                        "collection": collection,
                        "normalization": "omitted_empty_terminal_collection",
                        "prior_page_token_digest": _canonical_digest(token),
                        "original_response_digest": _canonical_digest(payload),
                    }
                )
                return rows
            failures.append(_failure(operation, url, code="collection_missing", field=collection))
            return rows
        page = payload[collection]
        if not isinstance(page, list):
            failures.append(_failure(operation, url, code="collection_not_list", field=collection))
            return rows
        for item_index, item in enumerate(page):
            if isinstance(item, dict):
                rows.append(item)
            else:
                failures.append(
                    _failure(
                        operation,
                        url,
                        code="collection_item_invalid",
                        field=collection,
                        item_index=item_index,
                    )
                )
        unreachable = payload.get("unreachable") or []
        if unreachable:
            failures.append(_failure(operation, url, code="unreachable_scope"))
        next_token = payload.get("nextPageToken") or ""
        if not next_token:
            return rows
        if not isinstance(next_token, str) or next_token in seen:
            failures.append(_failure(operation, url, code="pagination_cycle"))
            return rows
        seen.add(next_token)
        token = next_token


def _read(
    client: ReadClient,
    method: str,
    url: str,
    operation: str,
    resource: str,
    failures: list[dict],
    *,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict | None:
    try:
        payload = client.request_json(
            method,
            url,
            params=params,
            json_body=json_body,
        )
    except Exception as error:
        failures.append(_failure(operation, resource, error))
        return None
    if not isinstance(payload, dict):
        failures.append(_failure(operation, resource, code="response_not_object"))
        return None
    return payload


def _resource_row(name: str, region: str, observed: dict, hash_key: bytes) -> dict:
    status_observed = {}
    if "observedGeneration" in observed:
        status_observed["observedGeneration"] = observed["observedGeneration"]
    conditions = observed.get("conditions") or []
    ready = next(
        (
            condition
            for condition in conditions
            if isinstance(condition, dict) and condition.get("type") == "Ready"
        ),
        None,
    )
    if ready is None:
        terminal = observed.get("terminalCondition")
        if isinstance(terminal, dict) and terminal.get("type") == "Ready":
            ready = terminal
    if ready is not None:
        status_observed["ready"] = _sanitize(ready, hash_key)
    return {
        "name": name,
        "region": region,
        "field_coverage": sorted(observed),
        "status_observed": status_observed,
        "observed": _sanitize(observed, hash_key),
    }


def _required_field_failure(
    failures: list[dict], operation: str, resource: str, field: str
) -> None:
    failures.append(
        _failure(operation, resource, code="required_field_missing", field=field)
    )


def _detail_identity_matches(
    detail: dict, expected_name: str, operation: str, failures: list[dict]
) -> bool:
    if detail.get("name") != expected_name:
        failures.append(
            _failure(
                operation,
                expected_name,
                code="resource_identity_mismatch",
                field="name",
            )
        )
        return False
    return True


def _validate_run_template(
    detail: dict, category: str, name: str, failures: list[dict]
) -> None:
    template = detail.get("template")
    if not isinstance(template, dict):
        _required_field_failure(failures, category, name, "template")
        return
    container_parent = template if category == "services" else template.get("template")
    if not isinstance(container_parent, dict):
        _required_field_failure(failures, category, name, "template.template")
        return
    containers = container_parent.get("containers")
    if not isinstance(containers, list) or not containers:
        _required_field_failure(failures, category, name, "containers")
        return
    for container in containers:
        if not isinstance(container, dict) or not isinstance(container.get("image"), str) or not container["image"]:
            _required_field_failure(failures, category, name, "image")


def _malformed_field(
    failures: list[dict], operation: str, resource: str, field_path: str
) -> None:
    failures.append(
        _failure(operation, resource, code="field_malformed", field=field_path)
    )


def _validate_iam_bindings(
    bindings: list, resource: str, failures: list[dict]
) -> bool:
    valid = True
    for index, binding in enumerate(bindings):
        path = f"bindings[{index}]"
        if not isinstance(binding, dict):
            _malformed_field(failures, "iam", resource, path)
            valid = False
            continue
        role = binding.get("role")
        if not isinstance(role, str) or not role:
            _malformed_field(failures, "iam", resource, f"{path}.role")
            valid = False
        members = binding.get("members")
        if not isinstance(members, list):
            _malformed_field(failures, "iam", resource, f"{path}.members")
            valid = False
        else:
            for member_index, member in enumerate(members):
                if not isinstance(member, str) or not member:
                    _malformed_field(
                        failures,
                        "iam",
                        resource,
                        f"{path}.members[{member_index}]",
                    )
                    valid = False
        if "condition" not in binding:
            continue
        condition = binding["condition"]
        condition_path = f"{path}.condition"
        if not isinstance(condition, dict):
            _malformed_field(failures, "iam", resource, condition_path)
            valid = False
            continue
        expression = condition.get("expression")
        if not isinstance(expression, str) or not expression:
            _malformed_field(
                failures,
                "iam",
                resource,
                f"{condition_path}.expression",
            )
            valid = False
        for optional_field in ("title", "description", "location"):
            if optional_field in condition and not isinstance(
                condition[optional_field], str
            ):
                _malformed_field(
                    failures,
                    "iam",
                    resource,
                    f"{condition_path}.{optional_field}",
                )
                valid = False
    return valid


def _validate_schema_fields(
    fields: list,
    resource: str,
    failures: list[dict],
    path: str = "schema.fields",
) -> bool:
    valid = True
    for index, field in enumerate(fields):
        field_path = f"{path}[{index}]"
        if not isinstance(field, dict):
            _malformed_field(failures, "tables", resource, field_path)
            valid = False
            continue
        for required, required_value in (
            ("name", field.get("name")),
            ("type", field.get("type")),
        ):
            if not isinstance(required_value, str) or not required_value:
                _malformed_field(
                    failures,
                    "tables",
                    resource,
                    f"{field_path}.{required}",
                )
                valid = False
        if "mode" in field and not isinstance(field["mode"], str):
            _malformed_field(failures, "tables", resource, f"{field_path}.mode")
            valid = False
        if "fields" not in field:
            continue
        nested = field["fields"]
        if not isinstance(nested, list):
            _malformed_field(failures, "tables", resource, f"{field_path}.fields")
            valid = False
            continue
        if not _validate_schema_fields(
            nested,
            resource,
            failures,
            path=f"{field_path}.fields",
        ):
            valid = False
    return valid


def _capture_iam(
    client: ReadClient,
    url: str,
    name: str,
    failures: list[dict],
    hash_key: bytes,
    *,
    method: str = "GET",
) -> dict | None:
    payload = _read(
        client,
        method,
        url,
        "iam",
        name,
        failures,
        params={"options.requestedPolicyVersion": "3"} if method == "GET" else None,
        json_body={"options": {"requestedPolicyVersion": 3}}
        if method == "POST"
        else None,
    )
    if payload is None:
        return None
    raw_field_coverage = sorted(payload)
    normalizations = []
    if "bindings" not in payload:
        if not isinstance(payload.get("etag"), str) or not payload["etag"]:
            failures.append(
                _failure("iam", name, code="required_field_missing", field="bindings")
            )
            return None
        raw_digest = _canonical_digest(payload)
        payload = deepcopy(payload)
        payload["bindings"] = []
        normalizations.append(
            {
                "field": "bindings",
                "method": method,
                "resource": name,
                "reason": "provider_omitted_empty_repeated_field",
                "raw_response_digest": raw_digest,
            }
        )
    elif not isinstance(payload["bindings"], list):
        failures.append(_failure("iam", name, code="field_malformed", field="bindings"))
        return None
    elif not payload["bindings"] and (
        not isinstance(payload.get("etag"), str) or not payload["etag"]
    ):
        failures.append(_failure("iam", name, code="policy_anchor_missing", field="etag"))
        return None
    if not _validate_iam_bindings(payload["bindings"], name, failures):
        return None
    row = {
        "name": name,
        "field_coverage": raw_field_coverage,
        "observed": _sanitize(payload, hash_key),
    }
    if normalizations:
        row["transport_normalizations"] = normalizations
    return row


def capture_baseline(
    inventory: dict,
    client: ReadClient,
    *,
    identity: str,
    private_hash_key: bytes,
    observed_at: str | None = None,
) -> dict:
    project, regions = _validate_inventory(inventory)
    if not identity.strip():
        raise ValueError("identity_missing")
    if not private_hash_key:
        raise ValueError("private_hash_key_missing")
    identity_evidence = _validated_principal_evidence(client, identity)
    failures: list[dict] = []
    transport_normalizations: list[dict] = []
    resources: dict[str, list[dict]] = {
        "jobs": [],
        "services": [],
        "schedulers": [],
        "queues": [],
        "datasets": [],
        "tables": [],
        "iam": [],
    }

    project_name = f"projects/{project}"
    project_iam = _capture_iam(
        client,
        f"https://cloudresourcemanager.googleapis.com/v1/{project_name}:getIamPolicy",
        project_name,
        failures,
        private_hash_key,
        method="POST",
    )
    if project_iam:
        resources["iam"].append(project_iam)

    for region in regions["run"]:
        parent = f"projects/{project}/locations/{region}"
        for category in ("jobs", "services"):
            list_url = f"https://run.googleapis.com/v2/{parent}/{category}"
            summaries = _list_pages(client, list_url, category, f"list_{category}", failures)
            for summary in summaries:
                name = summary.get("name")
                if not isinstance(name, str) or not name:
                    _required_field_failure(
                        failures, f"list_{category}", list_url, "name"
                    )
                    continue
                detail = _read(client, "GET", f"https://run.googleapis.com/v2/{name}", category, name, failures)
                if detail is None:
                    continue
                if not _detail_identity_matches(detail, name, category, failures):
                    continue
                _validate_run_template(detail, category, name, failures)
                resources[category].append(_resource_row(name, region, detail, private_hash_key))
                iam = _capture_iam(
                    client,
                    f"https://run.googleapis.com/v2/{name}:getIamPolicy",
                    name,
                    failures,
                    private_hash_key,
                )
                if iam:
                    resources["iam"].append(iam)

    for region in regions["scheduler"]:
        parent = f"projects/{project}/locations/{region}"
        list_url = f"https://cloudscheduler.googleapis.com/v1/{parent}/jobs"
        summaries = _list_pages(client, list_url, "jobs", "list_schedulers", failures)
        for summary in summaries:
            name = summary.get("name")
            if not isinstance(name, str) or not name:
                _required_field_failure(failures, "list_schedulers", list_url, "name")
                continue
            detail = _read(client, "GET", f"https://cloudscheduler.googleapis.com/v1/{name}", "schedulers", name, failures)
            if detail is None:
                continue
            if not _detail_identity_matches(detail, name, "schedulers", failures):
                continue
            for field in ("schedule", "state"):
                if field not in detail:
                    _required_field_failure(failures, "schedulers", name, field)
            if not any(field in detail for field in ("httpTarget", "pubsubTarget", "appEngineHttpTarget")):
                _required_field_failure(failures, "schedulers", name, "target")
            resources["schedulers"].append(_resource_row(name, region, detail, private_hash_key))
    for region in regions["tasks"]:
        parent = f"projects/{project}/locations/{region}"
        list_url = f"https://cloudtasks.googleapis.com/v2/{parent}/queues"
        summaries = _list_pages(
            client,
            list_url,
            "queues",
            "list_queues",
            failures,
            transport_normalizations=transport_normalizations,
        )
        for summary in summaries:
            name = summary.get("name")
            if not isinstance(name, str) or not name:
                _required_field_failure(failures, "list_queues", list_url, "name")
                continue
            detail = _read(client, "GET", f"https://cloudtasks.googleapis.com/v2/{name}", "queues", name, failures)
            if detail is None:
                continue
            if not _detail_identity_matches(detail, name, "queues", failures):
                continue
            for field in ("rateLimits", "retryConfig"):
                if not isinstance(detail.get(field), dict):
                    _required_field_failure(failures, "queues", name, field)
            resources["queues"].append(_resource_row(name, region, detail, private_hash_key))
            iam = _capture_iam(
                client,
                f"https://cloudtasks.googleapis.com/v2/{name}:getIamPolicy",
                name,
                failures,
                private_hash_key,
                method="POST",
            )
            if iam:
                resources["iam"].append(iam)

    dataset_url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/datasets"
    dataset_summaries = _list_pages(
        client,
        dataset_url,
        "datasets",
        "list_datasets",
        failures,
        base_params={"all": "true"},
    )
    wanted_locations = set(regions["bigquery"])
    for summary in dataset_summaries:
        reference = summary.get("datasetReference") or {}
        dataset_id = reference.get("datasetId")
        if not isinstance(dataset_id, str) or not dataset_id:
            _required_field_failure(
                failures, "list_datasets", dataset_url, "datasetReference.datasetId"
            )
            continue
        name = f"projects/{project}/datasets/{dataset_id}"
        detail_url = f"{dataset_url}/{dataset_id}"
        detail = _read(
            client,
            "GET",
            detail_url,
            "datasets",
            name,
            failures,
            params={"datasetView": "FULL"},
        )
        if detail is None:
            continue
        detail_reference = detail.get("datasetReference")
        if not isinstance(detail_reference, dict) or (
            detail_reference.get("projectId") != project
            or detail_reference.get("datasetId") != dataset_id
        ):
            failures.append(
                _failure(
                    "datasets",
                    name,
                    code="resource_identity_mismatch",
                    field="datasetReference",
                )
            )
            continue
        location = detail.get("location")
        if not isinstance(location, str) or not location:
            _required_field_failure(failures, "datasets", name, "location")
            continue
        if location not in wanted_locations:
            continue
        if not isinstance(detail.get("access"), list):
            _required_field_failure(failures, "datasets", name, "access")
        resources["datasets"].append(_resource_row(name, location, detail, private_hash_key))
        table_list_url = f"{detail_url}/tables"
        table_summaries = _list_pages(client, table_list_url, "tables", "list_tables", failures)
        for table_summary in table_summaries:
            table_reference = table_summary.get("tableReference") or {}
            table_id = table_reference.get("tableId")
            if not isinstance(table_id, str) or not table_id:
                _required_field_failure(
                    failures, "list_tables", table_list_url, "tableReference.tableId"
                )
                continue
            table_name = f"{name}/tables/{table_id}"
            table_detail = _read(client, "GET", f"{table_list_url}/{table_id}", "tables", table_name, failures)
            if table_detail is None:
                continue
            detail_reference = table_detail.get("tableReference")
            if not isinstance(detail_reference, dict) or (
                detail_reference.get("projectId") != project
                or detail_reference.get("datasetId") != dataset_id
                or detail_reference.get("tableId") != table_id
            ):
                failures.append(
                    _failure(
                        "tables",
                        table_name,
                        code="resource_identity_mismatch",
                        field="tableReference",
                    )
                )
                continue
            schema = table_detail.get("schema")
            if not isinstance(schema, dict) or not isinstance(schema.get("fields"), list):
                _required_field_failure(failures, "tables", table_name, "schema")
            else:
                _validate_schema_fields(schema["fields"], table_name, failures)
            for field in ("timePartitioning", "rangePartitioning", "clustering", "requirePartitionFilter"):
                table_detail.setdefault(field, None)
            resources["tables"].append(_resource_row(table_name, location, table_detail, private_hash_key))
            table_iam = _capture_iam(
                client,
                f"https://bigquery.googleapis.com/bigquery/v2/{table_name}:getIamPolicy",
                table_name,
                failures,
                private_hash_key,
                method="POST",
            )
            if table_iam:
                resources["iam"].append(table_iam)

    for rows in resources.values():
        rows.sort(key=lambda row: row["name"])
    coverage = {
        "jobs": {"regions": regions["run"], "count": len(resources["jobs"])},
        "services": {"regions": regions["run"], "count": len(resources["services"])},
        "schedulers": {"regions": regions["scheduler"], "count": len(resources["schedulers"])},
        "queues": {"regions": regions["tasks"], "count": len(resources["queues"])},
        "datasets": {"regions": regions["bigquery"], "count": len(resources["datasets"])},
        "tables": {"regions": regions["bigquery"], "count": len(resources["tables"])},
        "iam": {"count": len(resources["iam"])},
    }
    result = {
        "schema_version": 1,
        "project": project,
        "regions": regions,
        "identity": identity,
        "identity_evidence": identity_evidence,
        "observed_at": observed_at or datetime.now(UTC).isoformat(),
        "complete": not failures,
        "coverage": coverage,
        "iam_capabilities": {
            "project": "resource_manager_policy",
            "jobs": "cloud_run_per_resource_policy",
            "services": "cloud_run_per_resource_policy",
            "schedulers": "project_policy_only_provider_has_no_job_iam_method",
            "queues": "cloud_tasks_per_resource_policy",
            "datasets": "full_dataset_access_metadata",
            "tables": "bigquery_per_resource_policy",
        },
        "resources": resources,
        "failures": failures,
    }
    if transport_normalizations:
        result["transport_normalizations"] = transport_normalizations
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result["content_digest"] = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    return result


def write_capture(output: Path, capture: dict) -> None:
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"output_exists:{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(capture, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _protected(value):
    if isinstance(value, list):
        return [_protected(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _protected(item)
            for key, item in value.items()
            if key not in VOLATILE_FIELDS
        }
    return value


def compare_baselines(before: dict, after: dict) -> list[dict]:
    if not before.get("complete") or not after.get("complete"):
        raise ValueError("baseline_incomplete")
    changes: list[dict] = []
    categories = ("jobs", "services", "schedulers", "queues", "datasets", "tables", "iam")
    for category in categories:
        before_rows = {row["name"]: row for row in before.get("resources", {}).get(category, [])}
        after_rows = {row["name"]: row for row in after.get("resources", {}).get(category, [])}
        for name in sorted(before_rows.keys() | after_rows.keys()):
            if name not in before_rows:
                change = "resource_added"
            elif name not in after_rows:
                change = "resource_removed"
            elif _protected(before_rows[name].get("observed")) != _protected(after_rows[name].get("observed")):
                change = "protected_configuration_changed"
            else:
                continue
            changes.append({"category": category, "name": name, "change": change})
    return changes


def _default_client(identity: str) -> NativeReadClient:
    executable = shutil.which("gcloud")
    if not executable:
        raise ValueError("gcloud_missing")
    if os.environ.get("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"):
        raise ValueError("impersonation_not_permitted")
    impersonation = subprocess.run(
        [
            executable,
            "config",
            "get-value",
            "auth/impersonate_service_account",
            "--quiet",
        ],
        check=False,
        capture_output=True,
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if impersonation.returncode:
        raise ValueError("impersonation_check_failed")
    configured_impersonation = impersonation.stdout.decode(
        "utf-8", errors="strict"
    ).strip()
    if configured_impersonation and configured_impersonation != "(unset)":
        raise ValueError("impersonation_not_permitted")
    completed = subprocess.run(
        [executable, "auth", "print-access-token", f"--account={identity}", "--quiet"],
        check=False,
        capture_output=True,
        timeout=40,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if completed.returncode:
        raise ValueError("operator_authentication_failed")
    token = completed.stdout.decode("utf-8", errors="strict").strip()
    if not token:
        raise ValueError("operator_token_empty")
    import requests

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    fingerprint = f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"
    evidence = _principal_evidence(
        identity,
        fingerprint,
        "gcloud_named_account_token_no_impersonation",
    )
    return NativeReadClient(session, evidence)


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[str], ReadClient] = _default_client,
) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--inventory", type=Path, required=True)
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.add_argument("--identity", required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(f"output_exists:{args.output}")
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    hash_key = os.environ.get("R00_BASELINE_HASH_KEY", "").encode("utf-8")
    if not hash_key:
        raise ValueError("private_hash_key_missing:R00_BASELINE_HASH_KEY")
    result = capture_baseline(
        inventory,
        client_factory(args.identity),
        identity=args.identity,
        private_hash_key=hash_key,
    )
    write_capture(args.output, result)
    print(json.dumps({"output": str(args.output), "complete": result["complete"], "content_digest": result["content_digest"]}))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
