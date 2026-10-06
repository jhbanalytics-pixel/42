"""Production non-change comparison against the R00 before baseline (E03, F09).

The R00 before certificate holds the protected production scope as it stood
before any 42 work. This module takes a fresh read only production capture
from ops/deploy/production_baseline.py, applies the same approved private
cache scope, and compares every protected resource field by field against
that certificate.

Every difference is classified. A change to a resource the staging resource
manifest declares, or an IAM pair the reviewed IAM delta declares, is a
declared staging change, and so is an access entry an approved routine
authorization declares or a dataset the approved delta creates. Anything
else requires an explanation from a person; the tool never explains it.
Capture failures outside the approved limitations are coverage limits and
block a non-change verdict. An after capture that is the R00 source capture,
or that was not observed after the reference, is refused. Nothing here reads
or writes a cloud resource.
"""

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from ops.certification.bigquery_metadata import (
    STATISTICS_FIELDS,
    compare_metadata,
    extract_metadata,
)
from ops.deploy.baseline_scope import (
    CATEGORIES,
    PROJECT,
    REGIONS,
    SHA256_RE,
    _classify_failures,
    _json_object,
    _protected_resources,
    _resource_rows,
    _validate_approval,
    _validate_excluded_dataset_access,
    _validate_native_principal,
    _verify_certificate,
    _verify_content_digest,
)
from ops.deploy.production_baseline import VOLATILE_FIELDS, _canonical_digest, _protected

SCHEMA = "42_production_comparison_v1"
PROJECT_IAM = f"projects/{PROJECT}"
PROJECT_IAM_RESOURCE = f"//cloudresourcemanager.googleapis.com/{PROJECT_IAM}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


DATASET_ROLES = {
    "READER": "roles/bigquery.dataViewer",
    "WRITER": "roles/bigquery.dataEditor",
    "OWNER": "roles/bigquery.dataOwner",
}
# The access BigQuery gives a dataset created without an access list: the
# three project role groups, plus OWNER for the identity that created it.
DEFAULT_DATASET_ACCESS = (
    {"role": "OWNER", "specialGroup": "projectOwners"},
    {"role": "WRITER", "specialGroup": "projectWriters"},
    {"role": "READER", "specialGroup": "projectReaders"},
)
# Only deploy lets 42 change a resource's configuration. write on a dataset
# is a data role (dataEditor) and allows new tables and declared access
# pairs only.
CHANGE_ACTIONS = {"deploy"}
# A scheduler that simply ran moves its next run time and its last attempt
# status; neither is configuration. The schedule, target and state stay
# compared.
SCHEDULER_RUN_STATE = {"scheduleTime", "lastAttemptTime", "status"}
# BigQuery rows keep the production baseline's protected view, which strips
# volatile names at every depth; the metadata backstop in compare_production
# compares what that view drops.
BIGQUERY_CATEGORIES = {"tables", "datasets"}
BIGQUERY = "//bigquery.googleapis.com/"
STANDING_COVERAGE_LIMITS = (
    {
        "limit": "routines_not_captured",
        "detail": "The R00 capture and the production capture read no BigQuery routines, so routine definitions are not compared.",
    },
    {
        "limit": "dataset_access_policy_version_1",
        "detail": "The production capture reads dataset access without accessPolicyVersion 3, so a conditional dataset access entry compares in its version 1 form.",
    },
)


def _declared_resources(manifest: dict) -> dict[tuple[str, str], set[str]]:
    """(category, name) to actions, for every resource the staging manifest declares."""
    declared: dict[tuple[str, str], set[str]] = {}
    for row in manifest.get("resources") or []:
        full = row.get("name") if isinstance(row, dict) else None
        actions = row.get("actions") if isinstance(row, dict) else None
        if not isinstance(full, str) or not isinstance(actions, list):
            continue
        category = None
        name = None
        if full.startswith("//run.googleapis.com/"):
            name = full[len("//run.googleapis.com/"):]
            category = "jobs" if "/jobs/" in name else "services" if "/services/" in name else None
        elif full.startswith("//cloudscheduler.googleapis.com/"):
            name, category = full[len("//cloudscheduler.googleapis.com/"):], "schedulers"
        elif full.startswith("//cloudtasks.googleapis.com/"):
            name, category = full[len("//cloudtasks.googleapis.com/"):], "queues"
        elif full.startswith("//bigquery.googleapis.com/"):
            name = full[len("//bigquery.googleapis.com/"):]
            parts = name.split("/")
            category = "datasets" if len(parts) == 4 else "tables" if "/tables/" in name else None
        if category is not None:
            declared.setdefault((category, name), set()).update(
                action for action in actions if isinstance(action, str)
            )
    return declared


def _condition_expression(condition) -> str | None:
    if condition is None:
        return None
    if isinstance(condition, str):
        return condition
    if isinstance(condition, dict) and isinstance(condition.get("expression"), str):
        return condition["expression"]
    return json.dumps(condition, sort_keys=True)


def _declared_pairs(delta: dict) -> set[tuple[str, str, str, str | None]]:
    """IAM pairs an approved delta or approved amendment grants, with their
    condition. Retained rows record bindings that existed before 42, so they
    never declare an addition."""
    rows = []
    approved = (delta.get("approval") or {}).get("state") == "approved"
    if approved:
        rows.extend(delta.get("bindings") or [])
    for amendment in delta.get("amendments") or []:
        if (amendment.get("approval") or {}).get("state") == "approved":
            rows.extend(amendment.get("bindings") or [])
    pairs = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        resource, role, member = row.get("resource"), row.get("role"), row.get("member")
        if all(isinstance(value, str) for value in (resource, role, member)):
            pairs.add((resource, role, member, _condition_expression(row.get("condition"))))
    # An approved routine authorization declares the one access entry that
    # authorizes that routine on that dataset, with its role (none for a
    # function).
    if approved:
        for row in delta.get("routine_authorizations") or []:
            if not isinstance(row, dict):
                continue
            dataset, routine, role = row.get("dataset"), row.get("routine"), row.get("role")
            if (
                isinstance(dataset, str)
                and isinstance(routine, str)
                and (role is None or isinstance(role, str))
            ):
                pairs.add((dataset, role, f"routine:{routine}", None))
    return pairs


def _created_datasets(delta: dict) -> set[str]:
    """Datasets an approved delta lists under create, by capture name."""
    if (delta.get("approval") or {}).get("state") != "approved":
        return set()
    created = set()
    for row in delta.get("create") or []:
        resource = row.get("resource") if isinstance(row, dict) else None
        if isinstance(resource, str) and resource.startswith(BIGQUERY):
            name = resource[len(BIGQUERY):]
            if _dataset_parent(name) == name:
                created.add(name)
    return created


def _dataset_parent(name: str) -> str | None:
    parts = name.split("/")
    if len(parts) >= 4 and parts[0] == "projects" and parts[2] == "datasets":
        return "/".join(parts[:4])
    return None


def _field_paths(before, after, path: str = "") -> list[str]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        paths = []
        for key in sorted(before.keys() | after.keys()):
            child = f"{path}.{key}" if path else key
            paths.extend(_field_paths(before.get(key), after.get(key), child))
        return paths
    return [path or "(root)"]


def _private_only(before, after) -> bool:
    """True when every differing leaf is a private hash on both sides."""

    def walk(old, new) -> bool:
        if old == new:
            return True
        if isinstance(old, dict) and isinstance(new, dict):
            if set(old) == {"privateHash"} and set(new) == {"privateHash"}:
                return True
            return all(walk(old.get(key), new.get(key)) for key in old.keys() | new.keys())
        if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
            return all(walk(a, b) for a, b in zip(old, new, strict=True))
        return False

    return walk(before, after)


def _full_condition(condition) -> str | None:
    """The whole condition, title and description included, so a renamed
    condition shows as a removed and an added binding."""
    if condition is None:
        return None
    return json.dumps(condition, sort_keys=True)


def _binding_pairs(observed: dict | None) -> set[tuple[str, str, str | None]]:
    pairs = set()
    for binding in (observed or {}).get("bindings") or []:
        if not isinstance(binding, dict):
            continue
        condition = _full_condition(binding.get("condition"))
        for member in binding.get("members") or []:
            pairs.add((binding.get("role"), member, condition))
    return pairs


def _access_pairs(observed: dict | None) -> set[tuple[str, str, str | None]]:
    """Dataset access entries as IAM pairs. An entry with no IAM member form
    (special group, view, routine, dataset) keeps its full JSON as the member,
    so it can never match a declared pair."""
    pairs = set()
    for entry in (observed or {}).get("access") or []:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        role = DATASET_ROLES.get(role, role)
        condition = _full_condition(entry.get("condition"))
        if isinstance(entry.get("iamMember"), str) and not entry["iamMember"].startswith("routine:"):
            member = entry["iamMember"]
        elif isinstance(entry.get("userByEmail"), str):
            email = entry["userByEmail"]
            kind = "serviceAccount" if email.endswith(".gserviceaccount.com") else "user"
            member = f"{kind}:{email}"
        elif isinstance(entry.get("groupByEmail"), str):
            member = f"group:{entry['groupByEmail']}"
        elif isinstance(entry.get("domain"), str):
            member = f"domain:{entry['domain']}"
        elif (routine := _routine_reference(entry)) is not None:
            member = f"routine:{routine}"
        else:
            member = "entry:" + json.dumps(
                {key: value for key, value in entry.items() if key != "role"}, sort_keys=True
            )
        pairs.add((role, member, condition))
    return pairs


def _routine_reference(entry: dict) -> str | None:
    """The full routine name of an authorized routine entry, or None when the
    entry carries anything besides its role and one exact routine reference."""
    reference = entry.get("routine")
    if set(entry) - {"role"} != {"routine"} or not isinstance(reference, dict):
        return None
    keys = ("projectId", "datasetId", "routineId")
    if set(reference) != set(keys) or not all(isinstance(reference[key], str) for key in keys):
        return None
    project, dataset, routine = (reference[key] for key in keys)
    return f"{BIGQUERY}projects/{project}/datasets/{dataset}/routines/{routine}"


def _iam_resource(name: str) -> str:
    if name == PROJECT_IAM:
        return PROJECT_IAM_RESOURCE
    if "/datasets/" in name:
        return f"//bigquery.googleapis.com/{name}"
    if "/queues/" in name:
        return f"//cloudtasks.googleapis.com/{name}"
    return f"//run.googleapis.com/{name}"


def _classify_pairs(resource: str, old: set, new: set, declared_pairs) -> list[dict]:
    rows = []
    for change, pairs in (("binding_added", new - old), ("binding_removed", old - new)):
        for role, member, condition in sorted(pairs, key=lambda item: json.dumps(item)):
            expression = _condition_expression(json.loads(condition)) if condition else None
            declared = (resource, role, member, expression) in declared_pairs
            rows.append(
                {
                    "change": change,
                    "role": role,
                    "member": member,
                    "condition": condition,
                    "classification": "declared_iam_delta"
                    if declared and change == "binding_added"
                    else "requires_explanation",
                }
            )
    return rows


def _all_declared(bindings: list[dict]) -> bool:
    return bool(bindings) and all(
        item["classification"] == "declared_iam_delta" for item in bindings
    )


def _strip_statistics(category: str, observed):
    if category != "tables" or not isinstance(observed, dict):
        return observed
    return {key: value for key, value in observed.items() if key not in STATISTICS_FIELDS}


def _comparison_view(category: str, observed):
    """The compared form of one resource. Outside BigQuery, volatile fields
    are stripped only at the resource top level, never inside labels,
    annotations, headers or any other map whose keys a person chooses."""
    if category in BIGQUERY_CATEGORIES:
        return _strip_statistics(category, _protected(observed))
    if not isinstance(observed, dict):
        return observed
    volatile = VOLATILE_FIELDS | (SCHEDULER_RUN_STATE if category == "schedulers" else set())
    return {key: deepcopy(value) for key, value in observed.items() if key not in volatile}


def _other_fields_changed(old_observed, new_observed, allowed: str) -> bool:
    """True when anything outside the one allowed top level field differs."""
    old = old_observed if isinstance(old_observed, dict) else {}
    new = new_observed if isinstance(new_observed, dict) else {}
    return any(
        old.get(key) != new.get(key) for key in old.keys() | new.keys() if key != allowed
    )


def _classify(
    category: str,
    name: str,
    change: str,
    old_observed,
    new_observed,
    declared: dict,
    declared_pairs,
    created: set[str],
    creator: str | None = None,
) -> dict:
    """Classification of one changed resource. Removal is never declared. A
    permission change is declared only pair by pair against the approved
    delta, and only when nothing else in the policy moved. A configuration
    change is declared only on a resource the manifest lets 42 deploy. A
    dataset the manifest lets 42 write may gain new tables and declared
    access pairs, nothing else. A dataset the approved delta creates may be
    added when every access entry it carries is a declared pair or the
    default access BigQuery gives a new dataset (the three project role
    groups, and OWNER for the capturing identity, which ran the create).
    Its other settings are accepted as part of the approved create."""
    result: dict = {}
    parent = _dataset_parent(name)
    parent_actions = declared.get(("datasets", parent), set()) if parent and "/tables/" in name else set()
    if category == "iam":
        bindings = _classify_pairs(
            _iam_resource(name),
            _binding_pairs(old_observed),
            _binding_pairs(new_observed),
            declared_pairs,
        )
        result["bindings"] = bindings
        new_table_policy = (
            change == "resource_added"
            and not bindings
            and not _other_fields_changed({}, new_observed, "bindings")
            and "write" in parent_actions
        )
        declared_row = new_table_policy or (
            change != "resource_removed"
            and _all_declared(bindings)
            and not _other_fields_changed(old_observed, new_observed, "bindings")
        )
        result["classification"] = "declared_iam_delta" if declared_row else "requires_explanation"
        return result
    if change == "resource_removed":
        result["classification"] = "requires_explanation"
        return result
    actions = declared.get((category, name), set())
    if (
        category == "datasets"
        and change == "resource_added"
        and name in created
        and actions & {"write", "deploy"}
    ):
        defaults = [*DEFAULT_DATASET_ACCESS]
        if creator is not None:
            defaults.append({"role": "OWNER", "userByEmail": creator})
        access = [entry for entry in (new_observed or {}).get("access") or [] if entry not in defaults]
        bindings = _classify_pairs(
            f"{BIGQUERY}{name}", set(), _access_pairs({"access": access}), declared_pairs
        )
        result["bindings"] = bindings
        result["classification"] = (
            "declared_staging_resource"
            if all(item["classification"] == "declared_iam_delta" for item in bindings)
            else "requires_explanation"
        )
        return result
    if category == "datasets" and change == "protected_configuration_changed" and actions & {"write", "deploy"}:
        bindings = _classify_pairs(
            f"//bigquery.googleapis.com/{name}",
            _access_pairs(old_observed),
            _access_pairs(new_observed),
            declared_pairs,
        )
        result["bindings"] = bindings
        declared_row = _all_declared(bindings) and (
            bool(actions & CHANGE_ACTIONS)
            or not _other_fields_changed(old_observed, new_observed, "access")
        )
        result["classification"] = (
            "declared_staging_resource" if declared_row else "requires_explanation"
        )
        return result
    may_change = bool(actions & CHANGE_ACTIONS)
    may_add_table = category == "tables" and change == "resource_added" and "write" in parent_actions
    result["classification"] = (
        "declared_staging_resource" if may_change or may_add_table else "requires_explanation"
    )
    return result


def _metadata_backstop(
    metadata: dict,
    changes: list[dict],
    declared: dict,
    declared_pairs,
    before_resources: dict,
    after_resources: dict,
) -> list[dict]:
    """A row requiring explanation for every metadata difference no
    classified row accounts for, for example a field the protected view
    strips. A row accounts for a resource only in its own category: a
    declared policy row covers a table's permissions and nothing else, and a
    dataset declared for its access pairs alone covers only access entries
    that are declared pairs in the unstripped capture."""
    by_key = {(row["category"], row["name"]): row for row in changes}
    rows = []
    for item in metadata["resources"]:
        kind, name = item["kind"], item["name"]
        if item["status"] not in {"changed", "added", "removed"}:
            continue
        own = by_key.get((kind, name))
        fields = sorted({diff["field"] for diff in item["diffs"]})
        if own is not None and (
            own["classification"] == "requires_explanation"
            or own["change"] != "protected_configuration_changed"
            or declared.get((kind, name), set()) & CHANGE_ACTIONS
        ):
            continue
        permissions = any(diff["category"] == "permissions" for diff in item["diffs"])
        uncovered = sorted(
            {diff["field"] for diff in item["diffs"] if diff["category"] != "permissions"}
        )
        if permissions:
            if own is not None and kind == "datasets":
                covered = _all_declared(
                    _classify_pairs(
                        f"{BIGQUERY}{name}",
                        _access_pairs(_observed(before_resources, kind, name)),
                        _access_pairs(_observed(after_resources, kind, name)),
                        declared_pairs,
                    )
                )
            else:
                covered = kind == "tables" and ("iam", name) in by_key
            if not covered:
                uncovered = fields
        if uncovered or item["status"] != "changed":
            rows.append(
                {
                    "category": kind,
                    "name": name,
                    "change": "metadata_only_change",
                    "fields": uncovered,
                    "classification": "requires_explanation",
                }
            )
    return rows


def _observed(resources: dict, category: str, name: str):
    for row in resources.get(category) or []:
        if row["name"] == name:
            return row.get("observed")
    return None


def _observed_time(value: dict, label: str) -> datetime:
    observed_at = value.get("observed_at")
    try:
        moment = datetime.fromisoformat(observed_at) if isinstance(observed_at, str) else None
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise ValueError(f"{label}_observed_at_invalid")
    return moment


def compare_production(
    *,
    before_certificate_bytes: bytes,
    reference_bytes: bytes,
    approval_bytes: bytes,
    after_capture_bytes: bytes,
    manifest_bytes: bytes,
    delta_bytes: bytes,
    trusted_certificate_sha256: str,
    trusted_reference_sha256: str,
    trusted_delta_sha256: str,
) -> dict:
    for label, value in (
        ("certificate", trusted_certificate_sha256),
        ("reference", trusted_reference_sha256),
        ("delta", trusted_delta_sha256),
    ):
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise ValueError(f"trusted_{label}_sha256_invalid")
    if _sha256(before_certificate_bytes) != trusted_certificate_sha256:
        raise ValueError("certificate_sha256_mismatch")
    if _sha256(reference_bytes) != trusted_reference_sha256:
        raise ValueError("reference_sha256_mismatch")
    if _sha256(delta_bytes) != trusted_delta_sha256:
        raise ValueError("delta_sha256_mismatch")
    certificate = _json_object(before_certificate_bytes, "certificate")
    _verify_certificate(certificate)
    if certificate.get("reference_sha256") != trusted_reference_sha256:
        raise ValueError("certificate_reference_binding_mismatch")
    if certificate.get("approval_sha256") != _sha256(approval_bytes):
        raise ValueError("certificate_approval_binding_mismatch")
    approval = _json_object(approval_bytes, "approval")
    excluded = set(_validate_approval(approval)["excluded_datasets"])

    capture = _json_object(after_capture_bytes, "capture")
    capture_digest = _verify_content_digest(capture, "capture")
    if capture.get("project") != PROJECT:
        raise ValueError("capture_project_mismatch")
    if capture.get("regions") != REGIONS:
        raise ValueError("capture_regions_mismatch")
    principal = _validate_native_principal(capture)
    after_all = _resource_rows(capture, "capture")
    after_resources = _protected_resources(after_all, excluded)
    retained, unexplained = _classify_failures(capture, excluded)
    _validate_excluded_dataset_access(after_all, excluded, unexplained)
    before_resources = _resource_rows(
        {"resources": certificate["protected_resources"]}, "certificate"
    )
    normalizations = capture.get("transport_normalizations", [])
    if not isinstance(normalizations, list):
        raise ValueError("capture_transport_normalizations_invalid")

    manifest = _json_object(manifest_bytes, "manifest")
    delta = _json_object(delta_bytes, "delta")
    if delta.get("resource_manifest_sha256") != _sha256(manifest_bytes):
        raise ValueError("delta_manifest_binding_mismatch")
    declared = _declared_resources(manifest)
    declared_pairs = _declared_pairs(delta)
    created = _created_datasets(delta)
    if (
        _sha256(after_capture_bytes) == certificate.get("source_capture_sha256")
        or capture_digest == certificate.get("source_capture_content_digest")
    ):
        raise ValueError("after_capture_is_source_capture")
    reference = _json_object(reference_bytes, "reference")
    if _observed_time(capture, "capture") <= _observed_time(reference, "reference"):
        raise ValueError("after_capture_not_later_than_reference")

    changes = []
    for category in CATEGORIES:
        before_rows = {row["name"]: row for row in before_resources[category]}
        after_rows = {row["name"]: row for row in after_resources[category]}
        for name in sorted(before_rows.keys() | after_rows.keys()):
            old = before_rows.get(name)
            new = after_rows.get(name)
            old_observed = _comparison_view(category, old.get("observed")) if old else None
            new_observed = _comparison_view(category, new.get("observed")) if new else None
            if old is not None and new is not None and old_observed == new_observed:
                continue
            change = (
                "resource_added"
                if old is None
                else "resource_removed"
                if new is None
                else "protected_configuration_changed"
            )
            row = {"category": category, "name": name, "change": change}
            if change == "protected_configuration_changed":
                row["fields"] = _field_paths(old_observed, new_observed)
                if _private_only(old_observed, new_observed):
                    row["private_value_only"] = True
            row.update(
                _classify(
                    category,
                    name,
                    change,
                    old_observed,
                    new_observed,
                    declared,
                    declared_pairs,
                    created,
                    principal["principal"],
                )
            )
            changes.append(row)

    metadata = compare_metadata(
        extract_metadata({"resources": before_resources}),
        extract_metadata({"resources": after_resources}),
    )
    changes.extend(
        _metadata_backstop(
            metadata, changes, declared, declared_pairs, before_resources, after_resources
        )
    )
    requires = [row for row in changes if row["classification"] == "requires_explanation"]
    unproven = metadata["counts"]["by_status"]["options_unproven"]
    counts = {
        "declared_staging_resource": sum(
            row["classification"] == "declared_staging_resource" for row in changes
        ),
        "declared_iam_delta": sum(row["classification"] == "declared_iam_delta" for row in changes),
        "requires_explanation": len(requires),
        "private_value_only": sum(bool(row.get("private_value_only")) for row in requires),
        "tables_options_unproven": unproven,
    }
    proven = not requires and not unexplained and not unproven
    result = {
        "schema_version": SCHEMA,
        "project": PROJECT,
        "before_certificate_sha256": trusted_certificate_sha256,
        "before_certificate_content_digest": certificate["content_digest"],
        "reference_sha256": trusted_reference_sha256,
        "approval_sha256": certificate["approval_sha256"],
        "after_capture_sha256": _sha256(after_capture_bytes),
        "after_capture_content_digest": capture_digest,
        "after_capture_complete": capture.get("complete"),
        "after_observed_at": capture.get("observed_at"),
        "after_principal_evidence": principal,
        "after_transport_normalizations": deepcopy(normalizations),
        "manifest_sha256": _sha256(manifest_bytes),
        "delta_sha256": trusted_delta_sha256,
        "counts": counts,
        "changes": changes,
        "bigquery_metadata": metadata,
        "retained_limitations": retained,
        "coverage_limits": unexplained,
        "standing_coverage_limits": [dict(limit) for limit in STANDING_COVERAGE_LIMITS],
        "verdict": "production_non_change_proven"
        if proven
        else "explanation_or_coverage_required",
        "note": (
            "A requires_explanation row is not a finding of change by 42 and not "
            "a clearance: a person explains it. private_value_only rows differ only "
            "in keyed private hashes, which also differ when the capture used "
            "another hash key. A coverage limit is an unread field, not a pass; "
            "standing coverage limits apply to every run of this comparison."
        ),
    }
    result["content_digest"] = _canonical_digest(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="production_comparison")
    parser.add_argument("--before-certificate", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--after-capture", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--delta", type=Path, required=True)
    parser.add_argument("--trusted-certificate-sha256", required=True)
    parser.add_argument("--trusted-reference-sha256", required=True)
    parser.add_argument("--trusted-delta-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(f"output_exists:{args.output}")
    result = compare_production(
        before_certificate_bytes=args.before_certificate.read_bytes(),
        reference_bytes=args.reference.read_bytes(),
        approval_bytes=args.approval.read_bytes(),
        after_capture_bytes=args.after_capture.read_bytes(),
        manifest_bytes=args.manifest.read_bytes(),
        delta_bytes=args.delta.read_bytes(),
        trusted_certificate_sha256=args.trusted_certificate_sha256,
        trusted_reference_sha256=args.trusted_reference_sha256,
        trusted_delta_sha256=args.trusted_delta_sha256,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(deepcopy(result), indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "verdict": result["verdict"],
                "counts": result["counts"],
                "coverage_limits": len(result["coverage_limits"]),
                "content_digest": result["content_digest"],
            }
        )
    )
    return 0 if result["verdict"] == "production_non_change_proven" else 3


if __name__ == "__main__":
    sys.exit(main())
