import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ID = "ogilvy-trends-v2"
PROJECT_NUMBER = "590353929363"
RESOURCE = "projects/" + PROJECT_NUMBER
ENDPOINT = "https://cloudresourcemanager.googleapis.com/v3/" + RESOURCE
READ_URL = ENDPOINT + ":getIamPolicy"
WRITE_URL = ENDPOINT + ":setIamPolicy"
ROLE = "roles/run.viewer"
MEMBER = "serviceAccount:intelligence-42-price-policy@ogilvy-trends-v2.iam.gserviceaccount.com"
POLICY_FIELDS = {"bindings", "etag", "version", "auditConfigs"}
BINDING_FIELDS = {"role", "members", "condition"}


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def checked_policy(policy):
    require(type(policy) is dict and set(policy) <= POLICY_FIELDS, "policy_shape_changed")
    require(type(policy.get("etag")) is str and policy["etag"], "policy_etag_missing")
    require(type(policy.get("version")) is int and policy["version"] == 3, "policy_version_invalid")
    require(type(policy.get("bindings")) is list, "policy_bindings_missing")
    require("auditConfigs" not in policy or type(policy["auditConfigs"]) is list, "policy_audit_shape_invalid")
    for binding in policy["bindings"]:
        require(type(binding) is dict and set(binding) <= BINDING_FIELDS, "binding_shape_changed")
        require(type(binding.get("role")) is str and binding["role"], "binding_role_invalid")
        members = binding.get("members")
        require(type(members) is list and members and all(type(item) is str and item for item in members), "binding_members_invalid")
        require(len(members) == len(set(members)), "binding_duplicate_member")
        if "condition" in binding:
            require(type(binding["condition"]) is dict, "binding_condition_invalid")
    return policy


def checked_project_descriptor(descriptor):
    require(type(descriptor) is dict, "project_descriptor_shape_invalid")
    require(descriptor.get("projectId") == PROJECT_ID, "project_id_mismatch")
    require(descriptor.get("name") == PROJECT_ID, "project_id_mismatch")
    require(str(descriptor.get("projectNumber")) == PROJECT_NUMBER, "project_number_mismatch")
    require(descriptor.get("lifecycleState") == "ACTIVE", "project_not_active")
    return descriptor


def build_plan(preimage, preimage_sha, project_descriptor, project_descriptor_sha):
    before = checked_policy(preimage)
    checked_project_descriptor(project_descriptor)
    require(type(preimage_sha) is str and len(preimage_sha) == 64, "preimage_sha256_invalid")
    require(type(project_descriptor_sha) is str and len(project_descriptor_sha) == 64, "project_descriptor_sha256_invalid")
    require(not any(binding["role"] == ROLE for binding in before["bindings"]), "run_viewer_binding_already_present")
    require(not any(MEMBER in binding["members"] for binding in before["bindings"]), "pricing_member_already_present")
    after = copy.deepcopy(before)
    after["bindings"].append({"role": ROLE, "members": [MEMBER]})
    return {
        "resource": RESOURCE,
        "project_id": PROJECT_ID,
        "project_number": PROJECT_NUMBER,
        "role": ROLE,
        "member": MEMBER,
        "helper_sha256": sha256(Path(__file__).read_bytes()),
        "preimage_sha256": preimage_sha,
        "project_descriptor_sha256": project_descriptor_sha,
        "preimage_etag": before["etag"],
        "set_request": {"policy": after, "updateMask": "bindings,etag"},
    }


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def save(root, name, value):
    with (root / name).open("xb") as out:
        out.write(json_bytes(value))


def checked_json(path, expected_sha, reason):
    require(type(expected_sha) is str and len(expected_sha) == 64 and all(ch in "0123456789abcdef" for ch in expected_sha), "sha256_invalid")
    raw = path.read_bytes()
    require(sha256(raw) == expected_sha, reason)
    value = json.loads(raw.decode("utf-8"))
    require(type(value) is dict, "input_not_object")
    return value


def plan(root, preimage, preimage_sha, project_descriptor, project_descriptor_sha):
    root = Path(root)
    require(root.is_dir(), "root_missing")
    before = checked_json(preimage, preimage_sha, "preimage_changed")
    descriptor = checked_json(project_descriptor, project_descriptor_sha, "project_descriptor_changed")
    result = build_plan(before, preimage_sha, descriptor, project_descriptor_sha)
    save(root, "plan.json", result)
    return {
        "plan_sha256": sha256((root / "plan.json").read_bytes()),
        "resource": RESOURCE,
        "role": ROLE,
        "member": MEMBER,
        "preimage_etag": before["etag"],
    }


APPROVAL_CONTRACT = "42_pricing_project_viewer_approval_v1"
APPROVAL_LINE = "pricing_project_viewer_approval_manifest_sha256="


def check_write_clock(now):
    require(now.tzinfo is not None and now.utcoffset().total_seconds() == 0, "clock_not_utc")
    require(now.date().isoformat() == "2026-09-28" and now.hour < 20, "write_clock_gate_refused")


def check_go(root, brief, brief_sha, approval_manifest, preimage_sha, plan_sha, project_descriptor_sha, clock):
    brief_raw = brief.read_bytes()
    require(sha256(brief_raw) == brief_sha, "brief_changed_since_go")
    go = json.loads((root / "go.json").read_text(encoding="utf-8"))
    require(type(go) is dict and go.get("brief_sha256") == brief_sha, "go_brief_mismatch")
    manifest_raw = approval_manifest.read_bytes()
    manifest_sha = sha256(manifest_raw)
    manifest = json.loads(manifest_raw.decode("utf-8"))
    require(type(manifest) is dict and set(manifest) == {"contract", "preimage_sha256", "plan_sha256", "helper_sha256"}, "approval_manifest_shape_invalid")
    require(manifest["contract"] == APPROVAL_CONTRACT, "approval_manifest_contract_invalid")
    require(go.get("approval_manifest_sha256") == manifest_sha, "approval_manifest_not_bound_to_go")
    declarations = [line for line in brief_raw.decode("utf-8").splitlines() if APPROVAL_LINE in line]
    require(declarations == [APPROVAL_LINE + manifest_sha], "approval_manifest_not_pinned_in_brief")
    require(
        manifest["preimage_sha256"] == preimage_sha
        and manifest["plan_sha256"] == plan_sha
        and manifest["helper_sha256"] == sha256(Path(__file__).read_bytes()),
        "approval_manifest_input_mismatch",
    )
    now = clock()
    received = datetime.fromisoformat(go["go_received_at"].replace("Z", "+00:00"))
    check_write_clock(now)
    require(received.tzinfo is not None and received <= now, "go_time_invalid")
    return now


def checked_inputs(root, preimage, preimage_sha, project_descriptor, project_descriptor_sha, plan_sha, approval_manifest, brief, brief_sha, clock):
    now = check_go(root, brief, brief_sha, approval_manifest, preimage_sha, plan_sha, project_descriptor_sha, clock)
    before = checked_json(preimage, preimage_sha, "preimage_changed")
    descriptor = checked_json(project_descriptor, project_descriptor_sha, "project_descriptor_changed")
    planned = checked_json(root / "plan.json", plan_sha, "plan_changed")
    require(planned == build_plan(before, preimage_sha, descriptor, project_descriptor_sha), "plan_no_longer_matches_preimage")
    return now, before, planned


def policy_content(policy):
    value = copy.deepcopy(checked_policy(policy))
    value.pop("etag")
    value["bindings"] = sorted(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
        for binding in value["bindings"]
    )
    return value


def response_status(response, url):
    require(type(response) is dict and "status" in response, "transport_response_invalid")
    status = response["status"]
    require(type(status) is int, "transport_response_invalid")
    require(not 300 <= status < 400, "redirect_refused")
    require(response.get("url") in (None, url), "redirect_refused")
    return status


def response_payload(response):
    body = bytes(response.get("body") or b"")
    require(bool(body), "response_not_json")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise RuntimeError("response_not_json") from None
    require(type(value) is dict, "response_not_json")
    return value


def get_policy(adapter, token):
    request = {
        "method": "POST",
        "url": READ_URL,
        "headers": {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        "body": json.dumps({"options": {"requestedPolicyVersion": 3}}, separators=(",", ":")).encode("utf-8"),
    }
    response = adapter.transport(request)
    status = response_status(response, READ_URL)
    require(status == 200, "project_iam_read_refused")
    return checked_policy(response_payload(response))


def reconcile_lost_response(root, adapter, expected, before_etag, reason):
    try:
        observed = get_policy(adapter, adapter.token())
        result = {
            "reason": reason,
            "expected_policy_observed": policy_content(observed) == policy_content(expected),
            "etag_advanced": observed["etag"] != before_etag,
            "observed_etag": observed["etag"],
        }
    except Exception as error:
        result = {"reason": reason, "readback_failed": type(error).__name__}
    save(root, "lost-response-reconciliation.json", result)
    raise RuntimeError("write_response_lost_stop")


def target_member_granted(policy):
    return any(
        binding["role"] == ROLE
        and "condition" not in binding
        and MEMBER in binding["members"]
        for binding in checked_policy(policy)["bindings"]
    )


def no_op_result(before, observed):
    return {
        "verified": True,
        "no_op": True,
        "resource": RESOURCE,
        "role": ROLE,
        "member": MEMBER,
        "before_etag": before["etag"],
        "after_etag": observed["etag"],
        "added_members": [],
        "removed_members": [],
    }


def native_adapter_from_core():
    core_root = Path.cwd().resolve()
    adapter_path = core_root / "ops/deploy/runtime_native_adapter.py"
    runner_path = core_root / "ops/runners/managed_runtime.py"
    require(adapter_path.is_file() and runner_path.is_file(), "core_repo_root_invalid")
    if str(core_root) not in sys.path:
        sys.path.insert(0, str(core_root))
    from ops.runners import managed_runtime

    require(getattr(managed_runtime, "PROJECT", None) == PROJECT_ID, "core_repo_project_mismatch")
    from ops.deploy import runtime_native_adapter as native

    require(Path(native.__file__).resolve() == adapter_path.resolve(), "native_adapter_outside_core_root")
    return native.NativeAdapter("owner-gcloud")


def apply(root, preimage, preimage_sha, project_descriptor, project_descriptor_sha, plan_sha, approval_manifest, brief, brief_sha, adapter=None, clock=None):
    root = Path(root)
    clock = clock or (lambda: datetime.now(timezone.utc))
    checked_inputs(root, preimage, preimage_sha, project_descriptor, project_descriptor_sha, plan_sha, approval_manifest, brief, brief_sha, clock)
    if adapter is None:
        adapter = native_adapter_from_core()
    live = get_policy(adapter, adapter.token())
    _, before, planned = checked_inputs(root, preimage, preimage_sha, project_descriptor, project_descriptor_sha, plan_sha, approval_manifest, brief, brief_sha, clock)
    if target_member_granted(live):
        return no_op_result(before, live)
    require(not any(MEMBER in binding["members"] for binding in live["bindings"]), "pricing_member_present_elsewhere")
    require(live["etag"] == before["etag"] and policy_content(live) == policy_content(before), "project_iam_changed_since_review")
    set_token = adapter.token()
    fresh = get_policy(adapter, set_token)
    if target_member_granted(fresh):
        return no_op_result(before, fresh)
    require(not any(MEMBER in binding["members"] for binding in fresh["bindings"]), "pricing_member_present_elsewhere")
    _, before, planned = checked_inputs(root, preimage, preimage_sha, project_descriptor, project_descriptor_sha, plan_sha, approval_manifest, brief, brief_sha, clock)
    require(fresh["etag"] == before["etag"] and policy_content(fresh) == policy_content(before), "project_iam_changed_before_send")
    expected = copy.deepcopy(fresh)
    expected["bindings"].append({"role": ROLE, "members": [MEMBER]})
    planned_policy = planned["set_request"]["policy"]
    require(
        expected["etag"] == planned_policy["etag"]
        and policy_content(expected) == policy_content(planned_policy),
        "plan_no_longer_matches_preimage",
    )
    require(policy_content(expected) != policy_content(live), "project_iam_add_missing")
    set_request = {"policy": expected, "updateMask": "bindings,etag"}
    body = json.dumps(set_request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = {
        "method": "POST",
        "url": WRITE_URL,
        "headers": {
            "Authorization": "Bearer " + set_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        "body": body,
    }
    checked_inputs(root, preimage, preimage_sha, project_descriptor, project_descriptor_sha, plan_sha, approval_manifest, brief, brief_sha, clock)
    go_raw = (root / "go.json").read_bytes()
    go = json.loads(go_raw.decode("utf-8"))
    require(
        sha256(preimage.read_bytes()) == preimage_sha
        and sha256(project_descriptor.read_bytes()) == project_descriptor_sha
        and sha256((root / "plan.json").read_bytes()) == plan_sha
        and sha256(brief.read_bytes()) == brief_sha
        and type(go) is dict
        and sha256(approval_manifest.read_bytes()) == go.get("approval_manifest_sha256")
        and go.get("brief_sha256") == brief_sha
        and sha256(Path(__file__).read_bytes()) == planned["helper_sha256"],
        "acting_input_changed_before_transport",
    )
    check_write_clock(clock())
    try:
        response = adapter.transport(request)
    except Exception as error:
        reconcile_lost_response(root, adapter, expected, live["etag"], type(error).__name__)
    try:
        status = response_status(response, WRITE_URL)
    except Exception as error:
        reconcile_lost_response(root, adapter, expected, live["etag"], str(error))
    if status >= 500:
        reconcile_lost_response(root, adapter, expected, live["etag"], "http_" + str(status))
    require(status == 200, "project_iam_set_refused_no_retry")
    try:
        answer = checked_policy(response_payload(response))
        require(policy_content(answer) == policy_content(expected), "project_iam_set_reply_mismatch")
        require(answer["etag"] != live["etag"], "project_iam_set_etag_unchanged")
    except Exception as error:
        reconcile_lost_response(root, adapter, expected, live["etag"], str(error))
    after = get_policy(adapter, adapter.token())
    require(policy_content(after) == policy_content(expected), "project_iam_readback_changed")
    require(after["etag"] == answer["etag"] and after["etag"] != live["etag"], "project_iam_readback_etag_mismatch")
    result = {
        "verified": True,
        "resource": RESOURCE,
        "role": ROLE,
        "member": MEMBER,
        "before_etag": live["etag"],
        "after_etag": after["etag"],
        "added_members": [MEMBER],
        "removed_members": [],
    }
    save(root, "verified.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--preimage", type=Path, required=True)
    parser.add_argument("--preimage-sha256", required=True)
    parser.add_argument("--project-descriptor", type=Path, required=True)
    parser.add_argument("--project-descriptor-sha256", required=True)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--approval-manifest", type=Path)
    parser.add_argument("--brief", type=Path)
    parser.add_argument("--brief-sha256")
    args = parser.parse_args()
    if args.action == "plan":
        result = plan(args.root, args.preimage, args.preimage_sha256, args.project_descriptor, args.project_descriptor_sha256)
    else:
        require(all((args.plan_sha256, args.approval_manifest, args.brief, args.brief_sha256)), "apply_pins_missing")
        result = apply(
            args.root,
            args.preimage,
            args.preimage_sha256,
            args.project_descriptor,
            args.project_descriptor_sha256,
            args.plan_sha256,
            args.approval_manifest,
            args.brief,
            args.brief_sha256,
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
