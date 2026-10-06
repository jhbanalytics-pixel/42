"""Full BigQuery metadata capture and comparison (E03).

A schema listing alone cannot prove table options, so this module works from
full tables.get, datasets.get, routines.get and getIamPolicy responses. Each
table is reduced to a canonical record grouped into the categories the plan
names: columns, modes, partitioning, clustering, location, expiry and
permissions, plus options, lifecycle and routine definitions. Two captures
are compared field by field.

Inputs are either a capture written by this module or a production baseline
capture from ops/deploy/production_baseline.py; both carry the same resource
rows. A table whose record lacks the fields only a full tables.get returns is
reported as options_unproven and never counts as unchanged.

The capture command is a read: it lists and gets datasets, tables, routines
and table policies, and never writes.
"""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from ops.certification.empty_table_list import AnchoredEmptyTableListClient
from ops.deploy.production_baseline import (
    ReadClient,
    _canonical_digest,
    _capture_iam,
    _default_client,
    _failure,
    _list_pages,
    _read,
    _resource_row,
    _validate_schema_fields,
    _validated_principal_evidence,
)

CAPTURE_SCHEMA = "42_bigquery_metadata_capture_v1"
COMPARISON_SCHEMA = "42_bigquery_metadata_comparison_v1"
API = "https://bigquery.googleapis.com/bigquery/v2"
CATEGORIES = (
    "presence",
    "columns",
    "modes",
    "partitioning",
    "clustering",
    "location",
    "expiry",
    "permissions",
    "options",
    "lifecycle",
    "routine_definition",
)
# Fields every tables.get response carries and a schema listing does not.
FULL_TABLE_FIELDS = ("tableReference", "type", "location", "creationTime")
# Storage statistics and transport identity. They change with data, not with
# configuration, and are not compared.
STATISTICS_FIELDS = {
    "etag",
    "id",
    "kind",
    "selfLink",
    "lastModifiedTime",
    "numRows",
    "numBytes",
    "numLongTermBytes",
    "numPhysicalBytes",
    "numTimeTravelPhysicalBytes",
    "numTotalLogicalBytes",
    "numActiveLogicalBytes",
    "numLongTermLogicalBytes",
    "numTotalPhysicalBytes",
    "numActivePhysicalBytes",
    "numLongTermPhysicalBytes",
    "numCurrentPhysicalBytes",
    "numPartitions",
    "streamingBuffer",
}
TABLE_HANDLED = {
    "tableReference",
    "schema",
    "location",
    "timePartitioning",
    "rangePartitioning",
    "requirePartitionFilter",
    "clustering",
    "expirationTime",
    "creationTime",
}
DATASET_HANDLED = {
    "datasetReference",
    "location",
    "access",
    "defaultTableExpirationMs",
    "defaultPartitionExpirationMs",
    "creationTime",
}
ROUTINE_HANDLED = {"routineReference", "definitionBody", "creationTime"}
# Every column key except these is compared, so a new provider field can
# never be dropped silently.
COLUMN_STRUCTURE_KEYS = {"name", "mode", "fields"}
PRIVATE_HASH_KEY = "privateHash"


def _is_private(value) -> bool:
    return isinstance(value, dict) and set(value) == {PRIVATE_HASH_KEY}


def _flatten_columns(fields: list, prefix: str = "") -> list[dict]:
    columns = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        path = f"{prefix}{field.get('name')}"
        column = {"path": path, "mode": field.get("mode") or "NULLABLE"}
        for key, value in field.items():
            if key not in COLUMN_STRUCTURE_KEYS:
                column[key] = deepcopy(value)
        columns.append(column)
        nested = field.get("fields")
        if isinstance(nested, list):
            columns.extend(_flatten_columns(nested, prefix=f"{path}."))
    return columns


def _iam_bindings(observed: dict | None) -> list[dict] | None:
    if not isinstance(observed, dict):
        return None
    bindings = []
    for binding in observed.get("bindings") or []:
        if not isinstance(binding, dict):
            continue
        entry = {
            "role": binding.get("role"),
            "members": sorted(binding.get("members") or []),
        }
        if binding.get("condition") is not None:
            entry["condition"] = deepcopy(binding["condition"])
        bindings.append(entry)
    return sorted(bindings, key=lambda item: json.dumps(item, sort_keys=True))


def _access_entries(access) -> list | None:
    if not isinstance(access, list):
        return None
    return sorted(
        (deepcopy(entry) for entry in access),
        key=lambda item: json.dumps(item, sort_keys=True),
    )


def _remaining(observed: dict, handled: set[str]) -> dict:
    return {
        key: deepcopy(value)
        for key, value in observed.items()
        if key not in handled and key not in STATISTICS_FIELDS
    }


def normalize_table(observed: dict, iam_observed: dict | None) -> dict:
    missing = [field for field in FULL_TABLE_FIELDS if field not in observed]
    schema = observed.get("schema")
    fields = schema.get("fields") if isinstance(schema, dict) else None
    if not isinstance(fields, list):
        missing.append("schema.fields")
    columns = _flatten_columns(fields or [])
    time_partitioning = deepcopy(observed.get("timePartitioning"))
    partition_expiry = None
    if isinstance(time_partitioning, dict):
        partition_expiry = time_partitioning.pop("expirationMs", None)
    clustering = observed.get("clustering")
    options = _remaining(observed, TABLE_HANDLED)
    if isinstance(schema, dict):
        schema_options = {key: deepcopy(value) for key, value in schema.items() if key != "fields"}
        if schema_options:
            options["schema"] = schema_options
    return {
        "options_proven": not missing,
        "missing_full_fields": missing,
        "columns": {
            "order": [column["path"] for column in columns],
            "definitions": {
                column["path"]: {
                    key: value for key, value in column.items() if key not in {"path", "mode"}
                }
                for column in columns
            },
        },
        "modes": {column["path"]: column["mode"] for column in columns},
        "partitioning": {
            "timePartitioning": time_partitioning,
            "rangePartitioning": deepcopy(observed.get("rangePartitioning")),
            "requirePartitionFilter": observed.get("requirePartitionFilter"),
        },
        "clustering": list(clustering.get("fields") or [])
        if isinstance(clustering, dict)
        else None,
        "location": observed.get("location"),
        "expiry": {
            "expirationTime": observed.get("expirationTime"),
            "partitionExpirationMs": partition_expiry,
        },
        "permissions": _iam_bindings(iam_observed),
        "options": options,
        "lifecycle": {"creationTime": observed.get("creationTime")},
    }


def normalize_dataset(observed: dict) -> dict:
    return {
        "options_proven": "location" in observed and isinstance(observed.get("access"), list),
        "location": observed.get("location"),
        "expiry": {
            "defaultTableExpirationMs": observed.get("defaultTableExpirationMs"),
            "defaultPartitionExpirationMs": observed.get("defaultPartitionExpirationMs"),
        },
        "permissions": _access_entries(observed.get("access")),
        "options": _remaining(observed, DATASET_HANDLED),
        "lifecycle": {"creationTime": observed.get("creationTime")},
    }


def normalize_routine(observed: dict) -> dict:
    body = observed.get("definitionBody")
    body_sha256 = (
        hashlib.sha256(body.encode("utf-8")).hexdigest() if isinstance(body, str) else None
    )
    return {
        "options_proven": isinstance(body, str) and "routineReference" in observed,
        "routine_definition": {"definitionBodySha256": body_sha256},
        "options": _remaining(observed, ROUTINE_HANDLED),
        "lifecycle": {"creationTime": observed.get("creationTime")},
    }


def _dataset_of(name: str) -> str:
    parts = name.split("/")
    return parts[3] if len(parts) > 3 and parts[2] == "datasets" else ""


def extract_metadata(capture: dict, datasets: set[str] | None = None) -> dict:
    """Canonical metadata for every dataset, table and routine in a capture."""
    resources = capture.get("resources")
    if not isinstance(resources, dict):
        raise ValueError("capture_resources_missing")

    def wanted(name: str) -> bool:
        return datasets is None or _dataset_of(name) in datasets

    iam = {
        row["name"]: row.get("observed")
        for row in resources.get("iam") or []
        if isinstance(row, dict) and "/tables/" in str(row.get("name"))
    }
    result = {"datasets": {}, "tables": {}, "routines": {}}
    for row in resources.get("datasets") or []:
        if wanted(row["name"]):
            result["datasets"][row["name"]] = normalize_dataset(row.get("observed") or {})
    for row in resources.get("tables") or []:
        if wanted(row["name"]):
            result["tables"][row["name"]] = normalize_table(
                row.get("observed") or {}, iam.get(row["name"])
            )
    for row in resources.get("routines") or []:
        if wanted(row["name"]):
            result["routines"][row["name"]] = normalize_routine(row.get("observed") or {})
    return result


def _contains_private(value) -> bool:
    if _is_private(value):
        return True
    if isinstance(value, dict):
        return any(_contains_private(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_private(item) for item in value)
    return False


def _field_diffs(category: str, before, after, path: str = "") -> list[dict]:
    if before == after:
        return []
    if (
        isinstance(before, dict)
        and isinstance(after, dict)
        and not _is_private(before)
        and not _is_private(after)
    ):
        diffs = []
        for key in sorted(before.keys() | after.keys()):
            child = f"{path}.{key}" if path else key
            diffs.extend(_field_diffs(category, before.get(key), after.get(key), child))
        return diffs
    diff = {
        "category": category,
        "field": path or category,
        "before": deepcopy(before),
        "after": deepcopy(after),
    }
    if _contains_private(before) or _contains_private(after):
        diff["private_value"] = True
    return [diff]


def _resource_categories(kind: str) -> tuple[str, ...]:
    if kind == "tables":
        return (
            "columns",
            "modes",
            "partitioning",
            "clustering",
            "location",
            "expiry",
            "permissions",
            "options",
            "lifecycle",
        )
    if kind == "datasets":
        return ("location", "expiry", "permissions", "options", "lifecycle")
    return ("routine_definition", "options", "lifecycle")


def compare_metadata(before: dict, after: dict) -> dict:
    rows = []
    for kind in ("datasets", "tables", "routines"):
        before_rows = before.get(kind, {})
        after_rows = after.get(kind, {})
        for name in sorted(before_rows.keys() | after_rows.keys()):
            if name not in before_rows:
                rows.append({"kind": kind, "name": name, "status": "added", "diffs": []})
                continue
            if name not in after_rows:
                rows.append({"kind": kind, "name": name, "status": "removed", "diffs": []})
                continue
            old, new = before_rows[name], after_rows[name]
            diffs = []
            for category in _resource_categories(kind):
                diffs.extend(_field_diffs(category, old.get(category), new.get(category)))
            if not (old["options_proven"] and new["options_proven"]):
                status = "options_unproven"
            elif diffs:
                status = "changed"
            else:
                status = "unchanged"
            row = {"kind": kind, "name": name, "status": status, "diffs": diffs}
            if status == "options_unproven":
                row["missing_full_fields"] = {
                    "before": old.get("missing_full_fields", []),
                    "after": new.get("missing_full_fields", []),
                }
            rows.append(row)
    by_category = {category: 0 for category in CATEGORIES}
    by_status = {"unchanged": 0, "changed": 0, "added": 0, "removed": 0, "options_unproven": 0}
    for row in rows:
        by_status[row["status"]] += 1
        if row["status"] in {"added", "removed"}:
            by_category["presence"] += 1
        for diff in row["diffs"]:
            by_category[diff["category"]] += 1
    return {
        "resources": rows,
        "counts": {"by_status": by_status, "by_category": by_category},
        "private_value_diffs": sum(
            1 for row in rows for diff in row["diffs"] if diff.get("private_value")
        ),
    }


def _load_capture(data: bytes, label: str) -> dict:
    try:
        capture = json.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label}_invalid_json") from error
    if not isinstance(capture, dict):
        raise ValueError(f"{label}_not_object")
    reported = capture.get("content_digest")
    material = {key: value for key, value in capture.items() if key != "content_digest"}
    if reported != _canonical_digest(material):
        raise ValueError(f"{label}_content_digest_mismatch")
    return capture


def compare_captures(
    before_bytes: bytes,
    after_bytes: bytes,
    *,
    datasets: set[str] | None = None,
) -> dict:
    before = _load_capture(before_bytes, "before")
    after = _load_capture(after_bytes, "after")
    if before.get("project") != after.get("project"):
        raise ValueError("capture_project_mismatch")
    if datasets is not None:
        covered = {
            _dataset_of(row.get("name", ""))
            for capture in (before, after)
            for row in (capture.get("resources") or {}).get("datasets") or []
            if isinstance(row, dict)
        }
        unknown = sorted(set(datasets) - covered)
        if not datasets or unknown:
            raise ValueError("dataset_filter_not_captured:" + ",".join(unknown))
    comparison = compare_metadata(
        extract_metadata(before, datasets), extract_metadata(after, datasets)
    )
    limits = []
    for label, capture in (("before", before), ("after", after)):
        for failure in capture.get("failures") or []:
            limits.append({"capture": label, **deepcopy(failure)})
    key_fingerprints = {
        label: capture.get("private_hash_key_fingerprint")
        for label, capture in (("before", before), ("after", after))
    }
    private_values_comparable = (
        key_fingerprints["before"] is not None
        and key_fingerprints["before"] == key_fingerprints["after"]
    )
    counts = comparison["counts"]["by_status"]
    # An empty scope proves nothing, so it can never read as no_change.
    no_change = (
        bool(comparison["resources"])
        and not limits
        and counts["changed"] == 0
        and counts["added"] == 0
        and counts["removed"] == 0
        and counts["options_unproven"] == 0
    )
    result = {
        "schema_version": COMPARISON_SCHEMA,
        "project": before.get("project"),
        "datasets": sorted(datasets) if datasets is not None else "all",
        "before": {
            "sha256": hashlib.sha256(before_bytes).hexdigest(),
            "content_digest": before["content_digest"],
            "complete": before.get("complete"),
            "observed_at": before.get("observed_at"),
        },
        "after": {
            "sha256": hashlib.sha256(after_bytes).hexdigest(),
            "content_digest": after["content_digest"],
            "complete": after.get("complete"),
            "observed_at": after.get("observed_at"),
        },
        "private_values_comparable": private_values_comparable,
        "coverage_limits": limits,
        "verdict": "no_change" if no_change else "differences_or_limits",
        **comparison,
    }
    result["content_digest"] = _canonical_digest(result)
    return result


def _key_fingerprint(key: bytes) -> str:
    digest = hmac.new(key, b"42_bigquery_metadata_key_fingerprint", hashlib.sha256)
    return f"hmac-sha256:{digest.hexdigest()}"


def capture_datasets(
    project: str,
    dataset_ids: list[str],
    client: ReadClient,
    *,
    identity: str,
    private_hash_key: bytes,
    observed_at: str | None = None,
) -> dict:
    if not project.strip() or not dataset_ids:
        raise ValueError("capture_scope_missing")
    if len(dataset_ids) != len(set(dataset_ids)):
        raise ValueError("capture_scope_duplicate")
    if not identity.strip():
        raise ValueError("identity_missing")
    if not private_hash_key:
        raise ValueError("private_hash_key_missing")
    identity_evidence = _validated_principal_evidence(client, identity)
    failures: list[dict] = []
    normalizations: list[dict] = []
    # An anchored empty table list is recorded into the same list, in read
    # order, alongside the routine list normalizations below.
    client = AnchoredEmptyTableListClient(client, normalizations)
    resources: dict[str, list[dict]] = {"datasets": [], "tables": [], "routines": [], "iam": []}
    for dataset_id in dataset_ids:
        name = f"projects/{project}/datasets/{dataset_id}"
        detail_url = f"{API}/{name}"
        detail = _read(
            client,
            "GET",
            detail_url,
            "datasets",
            name,
            failures,
            params={"datasetView": "FULL", "accessPolicyVersion": "3"},
        )
        if detail is None:
            continue
        reference = detail.get("datasetReference")
        if not isinstance(reference, dict) or (
            reference.get("projectId") != project or reference.get("datasetId") != dataset_id
        ):
            failures.append(
                _failure("datasets", name, code="resource_identity_mismatch", field="datasetReference")
            )
            continue
        location = detail.get("location") if isinstance(detail.get("location"), str) else ""
        if not location:
            failures.append(_failure("datasets", name, code="required_field_missing", field="location"))
        if not isinstance(detail.get("access"), list):
            failures.append(_failure("datasets", name, code="required_field_missing", field="access"))
        resources["datasets"].append(_resource_row(name, location, detail, private_hash_key))
        tables_url = f"{detail_url}/tables"
        for summary in _list_pages(
            client,
            tables_url,
            "tables",
            "list_tables",
            failures,
            transport_normalizations=normalizations,
        ):
            table_id = (summary.get("tableReference") or {}).get("tableId")
            if not isinstance(table_id, str) or not table_id:
                failures.append(
                    _failure("list_tables", tables_url, code="required_field_missing", field="tableReference.tableId")
                )
                continue
            table_name = f"{name}/tables/{table_id}"
            table = _read(client, "GET", f"{tables_url}/{table_id}", "tables", table_name, failures)
            if table is None:
                continue
            table_reference = table.get("tableReference")
            if not isinstance(table_reference, dict) or (
                table_reference.get("projectId") != project
                or table_reference.get("datasetId") != dataset_id
                or table_reference.get("tableId") != table_id
            ):
                failures.append(
                    _failure("tables", table_name, code="resource_identity_mismatch", field="tableReference")
                )
                continue
            for field in FULL_TABLE_FIELDS:
                if field not in table:
                    failures.append(
                        _failure("tables", table_name, code="required_field_missing", field=field)
                    )
            schema = table.get("schema")
            if table.get("type") in {"TABLE", "MATERIALIZED_VIEW", "VIEW", "EXTERNAL", "SNAPSHOT"}:
                if not isinstance(schema, dict) or not isinstance(schema.get("fields"), list):
                    failures.append(
                        _failure("tables", table_name, code="required_field_missing", field="schema")
                    )
                else:
                    _validate_schema_fields(schema["fields"], table_name, failures)
            resources["tables"].append(_resource_row(table_name, location, table, private_hash_key))
            policy = _capture_iam(
                client,
                f"{API}/{table_name}:getIamPolicy",
                table_name,
                failures,
                private_hash_key,
                method="POST",
            )
            if policy:
                resources["iam"].append(policy)
        routines_url = f"{detail_url}/routines"
        routines = _list_routines(client, routines_url, failures, normalizations)
        for summary in routines:
            routine_id = (summary.get("routineReference") or {}).get("routineId")
            if not isinstance(routine_id, str) or not routine_id:
                failures.append(
                    _failure("list_routines", routines_url, code="required_field_missing", field="routineReference.routineId")
                )
                continue
            routine_name = f"{name}/routines/{routine_id}"
            routine = _read(client, "GET", f"{routines_url}/{routine_id}", "routines", routine_name, failures)
            if routine is None:
                continue
            routine_reference = routine.get("routineReference")
            if not isinstance(routine_reference, dict) or (
                routine_reference.get("projectId") != project
                or routine_reference.get("datasetId") != dataset_id
                or routine_reference.get("routineId") != routine_id
            ):
                failures.append(
                    _failure("routines", routine_name, code="resource_identity_mismatch", field="routineReference")
                )
                continue
            if not isinstance(routine.get("definitionBody"), str):
                failures.append(
                    _failure("routines", routine_name, code="required_field_missing", field="definitionBody")
                )
            resources["routines"].append(_resource_row(routine_name, location, routine, private_hash_key))
    for rows in resources.values():
        rows.sort(key=lambda row: row["name"])
    result = {
        "schema_version": CAPTURE_SCHEMA,
        "project": project,
        "datasets": list(dataset_ids),
        "identity": identity,
        "identity_evidence": identity_evidence,
        "observed_at": observed_at or datetime.now(UTC).isoformat(),
        "private_hash_key_fingerprint": _key_fingerprint(private_hash_key),
        "complete": not failures,
        "coverage": {kind: len(rows) for kind, rows in resources.items()},
        "resources": resources,
        "failures": failures,
    }
    if normalizations:
        result["transport_normalizations"] = normalizations
    result["content_digest"] = _canonical_digest(result)
    return result


def _list_routines(
    client: ReadClient, url: str, failures: list[dict], normalizations: list[dict]
) -> list[dict]:
    # routines.list answers an empty dataset with an empty object and nothing
    # to anchor it, so an empty first page is kept as a recorded
    # normalization rather than silently accepted.
    rows: list[dict] = []
    token = ""
    seen: set[str] = set()
    while True:
        params = {"pageToken": token} if token else {}
        try:
            page = client.request_json("GET", url, params=params)
        except Exception as error:
            failures.append(_failure("list_routines", url, error))
            return rows
        if not isinstance(page, dict):
            failures.append(_failure("list_routines", url, code="response_not_object"))
            return rows
        if page == {} and not token:
            normalizations.append(
                {
                    "operation": "list_routines",
                    "resource": url,
                    "collection": "routines",
                    "normalization": "empty_routine_list_response",
                    "original_response_digest": _canonical_digest(page),
                }
            )
            return rows
        if not isinstance(page.get("routines"), list):
            failures.append(_failure("list_routines", url, code="collection_missing", field="routines"))
            return rows
        for index, item in enumerate(page["routines"]):
            if isinstance(item, dict):
                rows.append(item)
            else:
                failures.append(
                    _failure("list_routines", url, code="collection_item_invalid", field="routines", item_index=index)
                )
        next_token = page.get("nextPageToken") or ""
        if not next_token:
            return rows
        if not isinstance(next_token, str) or next_token in seen:
            failures.append(_failure("list_routines", url, code="pagination_cycle"))
            return rows
        seen.add(next_token)
        token = next_token


def write_json(path: Path, value: dict) -> None:
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"output_exists:{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[str], ReadClient] = _default_client,
) -> int:
    parser = argparse.ArgumentParser(prog="bigquery_metadata")
    commands = parser.add_subparsers(dest="command", required=True)
    capture_parser = commands.add_parser("capture", help="read only capture of named datasets")
    capture_parser.add_argument("--project", required=True)
    capture_parser.add_argument("--dataset", action="append", required=True)
    capture_parser.add_argument("--identity", required=True)
    capture_parser.add_argument("--output", type=Path, required=True)
    compare_parser = commands.add_parser("compare", help="field level comparison of two captures")
    compare_parser.add_argument("--before", type=Path, required=True)
    compare_parser.add_argument("--after", type=Path, required=True)
    compare_parser.add_argument("--dataset", action="append")
    compare_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(f"output_exists:{args.output}")
    if args.command == "capture":
        # A fresh key per capture keeps private values out of the file. Two
        # captures only compare private values when they share a key, which
        # the fingerprint records.
        key = os.environ.get("BIGQUERY_METADATA_HASH_KEY", "").encode("utf-8") or secrets.token_bytes(32)
        result = capture_datasets(
            args.project,
            args.dataset,
            client_factory(args.identity),
            identity=args.identity,
            private_hash_key=key,
        )
        write_json(args.output, result)
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "complete": result["complete"],
                    "coverage": result["coverage"],
                    "failures": len(result["failures"]),
                    "content_digest": result["content_digest"],
                }
            )
        )
        return 0 if result["complete"] else 2
    result = compare_captures(
        args.before.read_bytes(),
        args.after.read_bytes(),
        datasets=set(args.dataset) if args.dataset else None,
    )
    write_json(args.output, result)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "verdict": result["verdict"],
                "by_status": result["counts"]["by_status"],
                "by_category": result["counts"]["by_category"],
                "coverage_limits": len(result["coverage_limits"]),
                "content_digest": result["content_digest"],
            }
        )
    )
    return 0 if result["verdict"] == "no_change" else 3


if __name__ == "__main__":
    sys.exit(main())
